"""Convert radioml_cnn.pth checkpoint → radioml.onnx ready for disco classifier."""
import argparse
import torch
from train_radioml import CNN1D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", default="radioml_cnn.pth")
    ap.add_argument("--out", default="radioml.onnx")
    ap.add_argument("--samples", type=int, default=1024,
                    help="Input length the model expects (default 1024 — matches RML2018).")
    args = ap.parse_args()

    ckpt = torch.load(args.checkpoint, map_location="cpu")
    print(f"loaded checkpoint val_acc={ckpt.get('val_acc', '?')} classes={len(ckpt.get('classes', []))}")
    model = CNN1D(n_classes=len(ckpt.get("classes", [])) or 24)
    model.load_state_dict(ckpt["model_state"])
    model.eval()

    dummy = torch.randn(1, 2, args.samples)
    torch.onnx.export(
        model, dummy, args.out,
        input_names=["iq"], output_names=["logits"],
        dynamic_axes={"iq": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=14,
    )
    print(f"wrote {args.out}")
    print(f"classes ({len(ckpt.get('classes', []))}): {ckpt.get('classes', [])}")
    print()
    print("Next: scp this to the Micro at /home/ubuntu/scannerproject/disco/models/radioml.onnx")
    print("Then: sudo systemctl stop disco-classifier && sleep 2 && sudo systemctl start disco-classifier")


if __name__ == "__main__":
    main()
