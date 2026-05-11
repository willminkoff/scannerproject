"""Train CNN1D on the disco synthetic IQ dataset.

Reuses the CNN1D architecture from train_radioml.py but with n_classes=10 to
match CLASSES_DISCO. Loads the entire dataset into RAM (it's only ~400 MB) so
DataLoader workers=0 is fine.

Usage:
    python train_synthetic.py [--data data/disco_synth_v1.hdf5] [--epochs 30]

Output:
    disco_synth_cnn.pth     -- best PyTorch checkpoint by val_acc
    radioml.classes.json    -- sidecar with CLASSES_DISCO (for the deployed classifier)
"""
import argparse
import json
import os
import time

import h5py
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset, random_split

from train_radioml import CNN1D, pick_device  # reuse architecture + device picker

CLASSES_DISCO = [
    "NOISE",
    "FM_BROADCAST",
    "FM_NARROW",
    "AM_VOICE",
    "DMR",
    "GMSK",
    "QPSK",
    "OQPSK",
    "QAM",
    "OOK",
]


class DiscoSynthDataset(Dataset):
    """Eager-loaded dataset of synthetic disco IQ slices."""

    def __init__(self, hdf5_path):
        with h5py.File(hdf5_path, "r") as f:
            t0 = time.time()
            print(f"loading {hdf5_path} into RAM ...", flush=True)
            X = f["X"][...]  # (N, 1024, 2) float32
            Y = f["Y"][...]  # (N,) int64
            classes_json = f.attrs.get("classes")
            print(f"X: {X.shape} {X.dtype}  Y: {Y.shape} {Y.dtype}  ({time.time()-t0:.1f}s)", flush=True)

        # Transpose to (N, 2, 1024) once up-front.
        X = np.transpose(X, (0, 2, 1)).astype(np.float32)
        # Data is already peak-normalized at synthesis time, so no per-sample
        # normalize here. Re-applying would be a no-op anyway. Keep raw.
        self.X = X
        self.Y = Y.astype(np.int64)
        if classes_json is not None:
            saved = json.loads(classes_json)
            assert saved == CLASSES_DISCO, f"dataset class list mismatch: {saved} vs {CLASSES_DISCO}"

    def __len__(self):
        return len(self.X)

    def __getitem__(self, i):
        return torch.from_numpy(self.X[i]), int(self.Y[i])


def per_class_metrics(model, loader, device, n_classes):
    """Compute per-class precision and recall on val set."""
    model.eval()
    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    with torch.no_grad():
        for xb, yb in loader:
            xb = xb.to(device)
            yb = yb.to(device)
            pred = model(xb).argmax(1)
            for t, p in zip(yb.cpu().numpy(), pred.cpu().numpy()):
                cm[t, p] += 1
    # precision = diag / col_sum, recall = diag / row_sum
    prec = np.zeros(n_classes)
    rec = np.zeros(n_classes)
    for c in range(n_classes):
        col = cm[:, c].sum()
        row = cm[c, :].sum()
        prec[c] = cm[c, c] / col if col > 0 else 0.0
        rec[c] = cm[c, c] / row if row > 0 else 0.0
    return prec, rec, cm


def train(args):
    device = pick_device(args.device)
    print(f"device: {device}")

    ds = DiscoSynthDataset(args.data)
    print(f"dataset size: {len(ds)}")

    train_n = int(len(ds) * 0.85)
    val_n = len(ds) - train_n
    train_ds, val_ds = random_split(ds, [train_n, val_n], generator=torch.Generator().manual_seed(42))
    print(f"train: {train_n}  val: {val_n}")

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False, num_workers=0)

    n_classes = len(CLASSES_DISCO)
    model = CNN1D(n_classes=n_classes).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"model: CNN1D n_classes={n_classes}  params={n_params/1e3:.1f}k")

    opt = optim.Adam(model.parameters(), lr=args.lr)
    sched = optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    crit = nn.CrossEntropyLoss()

    best_val = 0.0
    for ep in range(args.epochs):
        model.train()
        t0 = time.time()
        train_loss = 0.0
        train_correct = 0
        train_total = 0
        for step, (xb, yb) in enumerate(train_loader):
            # Blocking transfer; PyTorch 2.11 + MPS has stale-data issues with
            # non_blocking=True per project notes.
            xb = xb.to(device)
            yb = yb.to(device)
            opt.zero_grad()
            out = model(xb)
            loss = crit(out, yb)
            loss.backward()
            opt.step()
            train_loss += loss.item() * xb.size(0)
            train_correct += (out.argmax(1) == yb).sum().item()
            train_total += xb.size(0)
            if ep == 0 and step in (0, 5, 25, 100):
                print(
                    f"  step {step}: loss={loss.item():.4f} acc={(out.argmax(1)==yb).float().mean().item():.4f}",
                    flush=True,
                )
        train_loss /= train_total
        train_acc = train_correct / train_total
        sched.step()

        model.eval()
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb = xb.to(device)
                yb = yb.to(device)
                out = model(xb)
                val_correct += (out.argmax(1) == yb).sum().item()
                val_total += xb.size(0)
        val_acc = val_correct / val_total
        elapsed = time.time() - t0
        print(
            f"epoch {ep+1}/{args.epochs}  train_loss={train_loss:.4f}  train_acc={train_acc:.4f}  val_acc={val_acc:.4f}  ({elapsed:.1f}s)",
            flush=True,
        )

        if val_acc > best_val:
            best_val = val_acc
            torch.save(
                {
                    "model_state": model.state_dict(),
                    "classes": CLASSES_DISCO,
                    "val_acc": val_acc,
                    "args": vars(args),
                },
                args.out_pth,
            )
            print(f"  -> saved {args.out_pth} (val_acc={val_acc:.4f})", flush=True)

    print(f"best val_acc: {best_val:.4f}")

    # Reload best and compute per-class metrics on val set.
    ckpt = torch.load(args.out_pth, map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state"])
    prec, rec, cm = per_class_metrics(model, val_loader, device, n_classes)

    print()
    print("per-class metrics (val set, best checkpoint):")
    print(f"  {'class':14s}  {'precision':>9s}  {'recall':>9s}  {'support':>9s}")
    for c in range(n_classes):
        sup = int(cm[c, :].sum())
        print(f"  {CLASSES_DISCO[c]:14s}  {prec[c]*100:>8.1f}%  {rec[c]*100:>8.1f}%  {sup:>9d}")

    print()
    print("confusion matrix (rows=true, cols=pred):")
    header = "        " + "".join(f"{CLASSES_DISCO[c][:6]:>7s}" for c in range(n_classes))
    print(header)
    for r in range(n_classes):
        row = f"{CLASSES_DISCO[r][:6]:>6s}: " + "".join(f"{cm[r,c]:>7d}" for c in range(n_classes))
        print(row)

    # Write sidecar classes file for the deployed classifier.
    sidecar = args.classes_out
    with open(sidecar, "w") as f:
        json.dump(CLASSES_DISCO, f, indent=2)
    print(f"wrote {sidecar}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/disco_synth_v1.hdf5")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--out-pth", default="disco_synth_cnn.pth")
    ap.add_argument("--classes-out", default="radioml.classes.json")
    args = ap.parse_args()
    train(args)


if __name__ == "__main__":
    main()
