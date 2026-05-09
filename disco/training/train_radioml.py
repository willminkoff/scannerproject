"""Train a CNN on RadioML 2018.01A for 24-class modulation classification.

Usage:
    python train_radioml.py --dataset /path/to/GOLD_XYZ_OSC.0001_1024.hdf5 --epochs 30 --device auto

Output:
    radioml_cnn.pth    -- PyTorch checkpoint
    radioml.onnx       -- ONNX export (drop into disco/models/)
"""
import argparse
import os
import time
import h5py
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, random_split

CLASSES_24 = [
    "OOK","4ASK","8ASK","BPSK","QPSK","8PSK","16PSK","32PSK",
    "16APSK","32APSK","64APSK","128APSK","16QAM","32QAM","64QAM","128QAM","256QAM",
    "AM-SSB-WC","AM-SSB-SC","AM-DSB-WC","AM-DSB-SC","FM","GMSK","OQPSK"
]


class RML2018Dataset(Dataset):
    """Lazy-load slices from RadioML 2018.01A HDF5."""
    def __init__(self, hdf5_path, snr_min_db=0, sample_per_class=None):
        self.hdf5_path = hdf5_path
        with h5py.File(hdf5_path, "r") as f:
            self.X = f["X"]
            self.Y = np.argmax(f["Y"][...], axis=1)
            self.Z = f["Z"][...].squeeze()
        self.mask = self.Z >= snr_min_db
        self.indices = np.nonzero(self.mask)[0]
        if sample_per_class:
            new_idx = []
            for c in range(24):
                pool = self.indices[self.Y[self.indices] == c]
                if len(pool):
                    pick = np.random.choice(pool, size=min(sample_per_class, len(pool)), replace=False)
                    new_idx.extend(pick.tolist())
            self.indices = np.array(new_idx)
        self._h = None

    def __len__(self):
        return len(self.indices)

    def _open(self):
        if self._h is None:
            self._h = h5py.File(self.hdf5_path, "r")
        return self._h

    def __getitem__(self, i):
        idx = int(self.indices[i])
        h = self._open()
        x = h["X"][idx]
        y = int(self.Y[idx])
        x = np.transpose(x, (1, 0)).astype(np.float32)
        x = x / (np.max(np.abs(x)) + 1e-12)
        return torch.from_numpy(x), y


class CNN1D(nn.Module):
    def __init__(self, n_classes=24):
        super().__init__()
        self.body = nn.Sequential(
            nn.Conv1d(2, 64, kernel_size=7, padding=3),  nn.BatchNorm1d(64),  nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(64, 128, kernel_size=5, padding=2), nn.BatchNorm1d(128), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(128, 256, kernel_size=3, padding=1), nn.BatchNorm1d(256), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(256, 256, kernel_size=3, padding=1), nn.BatchNorm1d(256), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.head = nn.Sequential(
            nn.Flatten(), nn.Dropout(0.4),
            nn.Linear(256, 128), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, n_classes),
        )

    def forward(self, x):
        return self.head(self.body(x))


def pick_device(name: str) -> torch.device:
    if name == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    return torch.device(name)


def train(args):
    device = pick_device(args.device)
    print(f"device: {device}")
    ds = RML2018Dataset(args.dataset, snr_min_db=args.snr_min_db, sample_per_class=args.sample_per_class)
    print(f"dataset size: {len(ds)}")
    train_n = int(len(ds) * 0.85)
    val_n = len(ds) - train_n
    train_ds, val_ds = random_split(ds, [train_n, val_n], generator=torch.Generator().manual_seed(42))
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=(device.type=="cuda"))
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=(device.type=="cuda"))

    model = CNN1D(n_classes=24).to(device)
    opt = optim.Adam(model.parameters(), lr=args.lr)
    sched = optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    crit = nn.CrossEntropyLoss()

    best_val = 0.0
    for ep in range(args.epochs):
        model.train()
        t0 = time.time()
        train_loss = 0.0; train_correct = 0; train_total = 0
        for xb, yb in train_loader:
            xb = xb.to(device, non_blocking=True); yb = yb.to(device, non_blocking=True)
            opt.zero_grad()
            out = model(xb)
            loss = crit(out, yb)
            loss.backward(); opt.step()
            train_loss += loss.item() * xb.size(0)
            train_correct += (out.argmax(1) == yb).sum().item()
            train_total += xb.size(0)
        train_loss /= train_total; train_acc = train_correct / train_total
        sched.step()

        model.eval()
        val_correct = 0; val_total = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb = xb.to(device); yb = yb.to(device)
                out = model(xb)
                val_correct += (out.argmax(1) == yb).sum().item()
                val_total += xb.size(0)
        val_acc = val_correct / val_total
        elapsed = time.time() - t0
        print(f"epoch {ep+1}/{args.epochs}  train_loss={train_loss:.4f}  train_acc={train_acc:.4f}  val_acc={val_acc:.4f}  ({elapsed:.1f}s)")
        if val_acc > best_val:
            best_val = val_acc
            torch.save({
                "model_state": model.state_dict(),
                "classes": CLASSES_24,
                "val_acc": val_acc,
                "args": vars(args),
            }, args.out_pth)
            print(f"  → saved {args.out_pth} (val_acc={val_acc:.4f})")

    print(f"best val_acc: {best_val:.4f}")
    print(f"checkpoint: {args.out_pth}")
    print("Run export_onnx.py to convert to ONNX for the disco classifier.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="Path to GOLD_XYZ_OSC.0001_1024.hdf5")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--snr-min-db", type=int, default=0,
                    help="Filter to samples with SNR >= this (dB). Higher = cleaner training set.")
    ap.add_argument("--sample-per-class", type=int, default=None,
                    help="If set, subsample N examples per class for faster iteration.")
    ap.add_argument("--device", default="auto", help="auto|cuda|mps|cpu")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out-pth", default="radioml_cnn.pth")
    args = ap.parse_args()
    train(args)


if __name__ == "__main__":
    main()
