# Disco — RadioML model training bundle (Phase 2.1B)

Train a 24-class modulation classifier on RadioML 2018.01A and produce an ONNX
file you can drop into Disco for live inference.

## What's in here

- `train_radioml.py` — PyTorch CNN trainer (1D conv stack, ~500K params)
- `export_onnx.py` — Convert the trained `.pth` checkpoint to ONNX
- `download_dataset.sh` — Helper for the dataset (manual signup required)
- `requirements.txt` — pip deps for the training env

## Output

A `radioml.onnx` file you scp to:
`/home/ubuntu/scannerproject/disco/models/radioml.onnx` on the Micro.
The disco-classifier service auto-detects it on next start and switches from
heuristic to ONNX backend.

## Two execution paths

### Path 1 — Will's Mac (Apple Silicon MPS, free, ~3-6 hours)

```bash
# 1. Set up env (Python 3.10+ recommended)
cd disco-training
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Get the dataset (manual — DeepSig signup)
bash download_dataset.sh   # prints instructions
# ... after manual download, file should be at: data/GOLD_XYZ_OSC.0001_1024.hdf5

# 3. Train
python train_radioml.py \
  --dataset data/GOLD_XYZ_OSC.0001_1024.hdf5 \
  --epochs 30 \
  --batch-size 256 \
  --device mps

# Expected: ~5-10 min/epoch on M1/M2 → 2-5 hours total.
# val_acc target: >0.85 at SNR>=0dB

# 4. Export ONNX
python export_onnx.py --checkpoint radioml_cnn.pth --out radioml.onnx

# 5. Ship to Micro
scp radioml.onnx root@100.67.20.40:/home/ubuntu/scannerproject/disco/models/radioml.onnx
ssh root@100.67.20.40 'sudo systemctl stop disco-classifier && sleep 2 && sudo systemctl start disco-classifier && sudo journalctl -u disco-classifier -n 5 --no-pager'
```

You should see `backend=onnx` in the disco-classifier log.

### Path 2 — Cloud GPU ($20 budget, ~30-60 min)

Recommended providers: **Vast.ai** or **RunPod** (cheapest GPU minutes).

```bash
# 1. Sign up at vast.ai or runpod.io. Add $20 credit.
#    Pick: RTX 4090 instance, ~$0.40/hr. Or A100 if available, ~$1/hr.
#    OS: Ubuntu 22.04 + PyTorch (any 2.x) preinstalled image.

# 2. SSH to the instance:
ssh root@<vast-instance-ip>

# 3. Upload this training bundle:
# (from your laptop)
scp -r disco-training root@<vast-instance-ip>:/workspace/

# 4. On the instance:
cd /workspace/disco-training
pip install -r requirements.txt

# 5. Get dataset. If you already downloaded it locally, scp to the instance:
scp data/GOLD_XYZ_OSC.0001_1024.hdf5 root@<vast-instance-ip>:/workspace/disco-training/data/
# OR run download_dataset.sh on the instance for instructions.

# 6. Train (CUDA auto-detected):
python train_radioml.py \
  --dataset data/GOLD_XYZ_OSC.0001_1024.hdf5 \
  --epochs 30 \
  --batch-size 512 \
  --device cuda

# Expected: ~30 sec/epoch on 4090 → 15-25 min total.
# val_acc target: >0.90 at SNR>=0dB

# 7. Export ONNX (small — <30 MB):
python export_onnx.py --checkpoint radioml_cnn.pth --out radioml.onnx

# 8. Pull back to your laptop:
scp root@<vast-instance-ip>:/workspace/disco-training/radioml.onnx ./

# 9. Ship to Micro:
scp radioml.onnx root@100.67.20.40:/home/ubuntu/scannerproject/disco/models/radioml.onnx
ssh root@100.67.20.40 'sudo systemctl stop disco-classifier && sleep 2 && sudo systemctl start disco-classifier'

# 10. Tear down the GPU instance to stop billing.
```

Cost breakdown:
- Vast 4090 @ $0.40/hr × 1 hour ≈ $0.40
- Plus ~10 min idle for setup/scp ≈ $0.07
- **Total: ~$0.50** (well under $20 budget; difference is yours)

## Tips

- `--snr-min-db 0` (default) trains only on samples with SNR ≥ 0 dB — cleaner labels, faster convergence. Drop to `-10` for more robustness on noisy real-world signals.
- `--sample-per-class 5000` for fast iteration during debugging (~5 min/epoch).
- The CNN is intentionally small (~500K params); ResNet variants in the literature get +2-3% accuracy at 5-10× model size. Stick with this for now — fast inference on the Micro CPU.

## What the disco classifier expects

- Input shape: `[batch, 2, 1024]` (channels = real + imaginary)
- Output shape: `[batch, 24]` (logits — softmax applied client-side)
- Class order: see `CLASSES_24` list in `train_radioml.py`. The disco classifier maps `class_<idx>` → modulation name via this list when ONNX backend is active.

If your trained model uses a different shape, edit `init_onnx_model()` in
`/home/ubuntu/scannerproject/disco/src/classifier.py` accordingly.
