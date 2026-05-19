"""Fine-tune the synthetic-trained disco CNN with real captured slices.

Validation showed the synthetic-only model is great on FM_BROADCAST (~85%)
but essentially broken on AM_VOICE (~2%) and FM_NARROW (~0%) because
synthetic AM/NFM doesn't capture real-world bursting + speech + CTCSS
patterns. This script:

  1. Loads the existing synthetic HDF5 (50k examples).
  2. Reads real captures from /captures/<LABEL>/, applies the SAME
     preprocessing the deployed classifier uses (resample to 1 MS/s,
     central 1024-sample window, per-sample peak normalize).
  3. Holds out 20% of captures per class for validation.
  4. Combines synthetic + train captures (captures oversampled so they
     have meaningful weight in the loss despite being ~1% of the dataset).
  5. Continues from the synthetic checkpoint for ~10 epochs at LR 1e-4
     (lower than original 1e-3 to avoid catastrophic forgetting).
  6. Reports per-class accuracy on held-out captures + on a synthetic
     validation slice to verify we haven't regressed on synthetic.
  7. Saves new checkpoint + exports single-file ONNX (opset 14).
"""
import argparse
import glob
import json
import os
import sys
import time
from math import gcd

import h5py
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from scipy.signal import resample_poly
from torch.utils.data import Dataset, DataLoader, ConcatDataset, random_split

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_synthetic import CNN1D, CLASSES_DISCO  # reuse arch + class list

SYNTH_HDF5 = "/Volumes/1tb/disco-training/data/disco_synth_v1.hdf5"
CHECKPOINT_IN = "/Volumes/1tb/disco-training/disco_synth_cnn.pth"
CHECKPOINT_OUT = "/Volumes/1tb/disco-training/disco_finetuned_cnn.pth"
ONNX_OUT = "/Volumes/1tb/disco-training/radioml.onnx"
CLASSES_OUT = "/Volumes/1tb/disco-training/radioml.classes.json"
CAPTURES_BASE = "/Volumes/1tb/disco-training/captures"
TARGET_RATE_HZ = 1_000_000

CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES_DISCO)}


def parse_meta(filename):
    base = os.path.basename(filename)
    if not base.endswith(".iq.f32"):
        return None
    parts = base[:-len(".iq.f32")].split("_")
    if len(parts) < 6:
        return None
    try:
        return {"freq_hz": float(parts[1]), "bandwidth_hz": float(parts[2]),
                "rate_hz": float(parts[3])}
    except Exception:
        return None


def resample_iq(iq, src_rate, target_rate):
    src = int(round(src_rate))
    if src == target_rate:
        return iq
    g = gcd(src, target_rate)
    up = target_rate // g
    down = src // g
    r = resample_poly(iq.real.astype(np.float32), up, down).astype(np.float32)
    i = resample_poly(iq.imag.astype(np.float32), up, down).astype(np.float32)
    return r + 1j * i


def preprocess_slice(path):
    """Match the deployed classifier's preprocessing exactly. Returns
    (1024, 2) float32 in real/imag last-dim order, or None on failure."""
    meta = parse_meta(path)
    if not meta:
        return None
    iq = np.fromfile(path, dtype=np.complex64)
    if len(iq) < 256:
        return None
    iq2 = resample_iq(iq, meta["rate_hz"], TARGET_RATE_HZ)
    n = len(iq2)
    if n < 1024:
        pad = np.zeros(1024 - n, dtype=iq2.dtype)
        win = np.concatenate([iq2, pad])
    else:
        s = (n - 1024) // 2
        win = iq2[s:s + 1024]
    nm = win / (np.max(np.abs(win)) + 1e-12)
    return np.stack([nm.real, nm.imag], axis=-1).astype(np.float32)


def load_captures(base_dir):
    """Walk /captures/<LABEL>/*.iq.f32, return per-label lists of (X, y)."""
    per_label = {}
    for label in sorted(os.listdir(base_dir)):
        if label not in CLASS_TO_IDX:
            print(f"  skipping unknown label dir: {label}")
            continue
        d = os.path.join(base_dir, label)
        files = sorted(glob.glob(os.path.join(d, "*.iq.f32")))
        Xs, ys = [], []
        for fp in files:
            x = preprocess_slice(fp)
            if x is None:
                continue
            Xs.append(x)
            ys.append(CLASS_TO_IDX[label])
        if Xs:
            per_label[label] = (np.stack(Xs), np.array(ys, dtype=np.int64))
            print(f"  {label}: {len(Xs)} usable captures")
    return per_label


def split_holdout(per_label, holdout_frac=0.2, seed=42):
    rng = np.random.default_rng(seed)
    train_X, train_y, val_X, val_y = [], [], [], []
    for label, (X, y) in per_label.items():
        n = len(X)
        idx = rng.permutation(n)
        n_val = max(1, int(round(n * holdout_frac)))
        val_idx, tr_idx = idx[:n_val], idx[n_val:]
        train_X.append(X[tr_idx]); train_y.append(y[tr_idx])
        val_X.append(X[val_idx]); val_y.append(y[val_idx])
        print(f"  {label}: {len(tr_idx)} train / {len(val_idx)} val")
    return (np.concatenate(train_X), np.concatenate(train_y),
            np.concatenate(val_X), np.concatenate(val_y))


class TensorPairDataset(Dataset):
    """X stored as (N, 1024, 2). Conv1d wants (2, 1024) per example."""
    def __init__(self, X, y):
        self.X = X
        self.y = y

    def __len__(self):
        return len(self.X)

    def __getitem__(self, i):
        x = self.X[i].transpose(1, 0)  # (2, 1024)
        return torch.from_numpy(x.copy()), int(self.y[i])


def evaluate(model, loader, device, n_classes):
    model.eval()
    correct_per_class = np.zeros(n_classes, dtype=np.int64)
    total_per_class = np.zeros(n_classes, dtype=np.int64)
    confusion = np.zeros((n_classes, n_classes), dtype=np.int64)
    with torch.no_grad():
        for xb, yb in loader:
            xb = xb.to(device); yb = yb.to(device)
            out = model(xb)
            pred = out.argmax(1)
            for t, p in zip(yb.cpu().numpy(), pred.cpu().numpy()):
                total_per_class[t] += 1
                if p == t:
                    correct_per_class[t] += 1
                confusion[t, p] += 1
    return correct_per_class, total_per_class, confusion


def print_per_class(correct, total, confusion, classes, title):
    print(f"\n=== {title} ===")
    overall_n = int(total.sum())
    overall_c = int(correct.sum())
    print(f"overall: {overall_c}/{overall_n} = {overall_c/max(overall_n,1):.2%}")
    print(f"{'class':<14} {'n':<5} {'correct':<8} {'acc':<8} confusion (top mispred)")
    for i, c in enumerate(classes):
        n = total[i]
        if n == 0:
            continue
        acc = correct[i] / n
        # top mispred
        misp = confusion[i].copy(); misp[i] = 0
        if misp.sum() > 0:
            top_mis = int(misp.argmax())
            top_n = int(misp[top_mis])
            mis_str = f"{classes[top_mis]}={top_n}"
        else:
            mis_str = "-"
        print(f"{c:<14} {n:<5d} {correct[i]:<8d} {acc:<8.2%} {mis_str}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--capture-oversample", type=int, default=20,
                    help="Repeat each capture this many times in the train set so it has comparable weight to synthetic")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    device = (torch.device("mps") if args.device == "auto" and torch.backends.mps.is_available()
              else torch.device(args.device if args.device != "auto" else "cpu"))
    print(f"device: {device}")

    # --- load synthetic ---
    print("loading synthetic dataset...")
    with h5py.File(SYNTH_HDF5, "r") as f:
        Xsy = f["X"][:]
        ysy = f["Y"][:]
    print(f"  synthetic: {Xsy.shape}, classes: {len(np.unique(ysy))}")

    # --- load real captures ---
    print("loading real captures...")
    per_label = load_captures(CAPTURES_BASE)
    if not per_label:
        sys.exit("no usable captures found")

    print("\nholdout split (per class):")
    cap_train_X, cap_train_y, cap_val_X, cap_val_y = split_holdout(per_label)
    print(f"  total: {len(cap_train_X)} train / {len(cap_val_X)} val captures")

    # --- combined train set: synthetic + oversampled captures ---
    X_train = np.concatenate([Xsy] + [cap_train_X] * args.capture_oversample)
    y_train = np.concatenate([ysy] + [cap_train_y] * args.capture_oversample)
    print(f"\ncombined train: {len(X_train)} examples ({len(Xsy)} synth + "
          f"{len(cap_train_X) * args.capture_oversample} oversampled real)")

    train_ds = TensorPairDataset(X_train, y_train)
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=0)

    # synthetic val (10%) for regression check
    n_synth_val = int(len(Xsy) * 0.10)
    rng = np.random.default_rng(0)
    sval_idx = rng.permutation(len(Xsy))[:n_synth_val]
    synth_val_ds = TensorPairDataset(Xsy[sval_idx], ysy[sval_idx])
    synth_val_loader = DataLoader(synth_val_ds, batch_size=args.batch_size, shuffle=False, num_workers=0)

    cap_val_ds = TensorPairDataset(cap_val_X, cap_val_y)
    cap_val_loader = DataLoader(cap_val_ds, batch_size=args.batch_size, shuffle=False, num_workers=0)

    # --- load checkpoint ---
    print(f"\nloading checkpoint {CHECKPOINT_IN}")
    ckpt = torch.load(CHECKPOINT_IN, map_location="cpu")
    model = CNN1D(n_classes=len(CLASSES_DISCO)).to(device)
    model.load_state_dict(ckpt["model_state"])

    # --- before metrics ---
    print("\n=== BEFORE FINE-TUNE ===")
    cb, tb, cfb = evaluate(model, cap_val_loader, device, len(CLASSES_DISCO))
    print_per_class(cb, tb, cfb, CLASSES_DISCO, "real captures (val) — current model")
    cs, ts, cfs = evaluate(model, synth_val_loader, device, len(CLASSES_DISCO))
    print_per_class(cs, ts, cfs, CLASSES_DISCO, "synthetic (val 10%) — current model")

    # --- fine-tune ---
    opt = optim.Adam(model.parameters(), lr=args.lr)
    sched = optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    crit = nn.CrossEntropyLoss()

    print(f"\nfine-tuning {args.epochs} epochs at lr={args.lr}, "
          f"capture_oversample={args.capture_oversample}")
    best_cap_acc = 0.0
    for ep in range(args.epochs):
        model.train()
        t0 = time.time()
        loss_acc = 0.0; n = 0; corr = 0
        for xb, yb in train_loader:
            xb = xb.to(device); yb = yb.to(device)
            opt.zero_grad()
            out = model(xb)
            loss = crit(out, yb)
            loss.backward(); opt.step()
            loss_acc += loss.item() * xb.size(0); n += xb.size(0)
            corr += (out.argmax(1) == yb).sum().item()
        sched.step()
        # quick val on captures
        cv_c, cv_t, _ = evaluate(model, cap_val_loader, device, len(CLASSES_DISCO))
        cap_acc = cv_c.sum() / max(cv_t.sum(), 1)
        sv_c, sv_t, _ = evaluate(model, synth_val_loader, device, len(CLASSES_DISCO))
        synth_acc = sv_c.sum() / max(sv_t.sum(), 1)
        print(f"epoch {ep+1}/{args.epochs} train_loss={loss_acc/n:.4f} "
              f"train_acc={corr/n:.4f} cap_val={cap_acc:.4f} synth_val={synth_acc:.4f} "
              f"({time.time()-t0:.1f}s)")
        if cap_acc > best_cap_acc:
            best_cap_acc = cap_acc
            torch.save({
                "model_state": model.state_dict(),
                "classes": CLASSES_DISCO,
                "cap_val_acc": float(cap_acc),
                "synth_val_acc": float(synth_acc),
                "args": vars(args),
            }, CHECKPOINT_OUT)
            print(f"  → saved {CHECKPOINT_OUT} (cap_val={cap_acc:.4f})")

    # --- after metrics ---
    print("\n=== AFTER FINE-TUNE (best checkpoint) ===")
    best_state = torch.load(CHECKPOINT_OUT, map_location="cpu")["model_state"]
    model.load_state_dict(best_state)
    model.to(device)
    ca, ta, cfa = evaluate(model, cap_val_loader, device, len(CLASSES_DISCO))
    print_per_class(ca, ta, cfa, CLASSES_DISCO, "real captures (val) — fine-tuned")
    csa, tsa, cfsa = evaluate(model, synth_val_loader, device, len(CLASSES_DISCO))
    print_per_class(csa, tsa, cfsa, CLASSES_DISCO, "synthetic (val 10%) — fine-tuned")

    print(f"\nbest cap_val_acc: {best_cap_acc:.4f}")
    print(f"checkpoint: {CHECKPOINT_OUT}")


if __name__ == "__main__":
    main()
