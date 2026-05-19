#!/usr/bin/env bash
# Download RadioML 2018.01A dataset for training.
# DeepSig hosts this behind a free signup at https://www.deepsig.ai/datasets/
set -euo pipefail

DATA_DIR="${DATA_DIR:-./data}"
mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

if [ -f "GOLD_XYZ_OSC.0001_1024.hdf5" ]; then
  echo "Dataset already present: $(pwd)/GOLD_XYZ_OSC.0001_1024.hdf5"
  ls -la GOLD_XYZ_OSC.0001_1024.hdf5
  exit 0
fi

cat <<EOF
The RadioML 2018.01A dataset (GOLD_XYZ_OSC.0001_1024.hdf5, ~14 GB) requires
a free DeepSig signup. Steps:

  1. Visit  https://www.deepsig.ai/datasets/
  2. Sign up (free) and accept the data agreement
  3. Download "RadioML 2018.01A Dataset (Open Source)"
  4. Place GOLD_XYZ_OSC.0001_1024.hdf5 in: $(pwd)
  5. Re-run this script to verify.

If you have a direct download URL from DeepSig, set DATASET_URL=<url> and re-run.
EOF

if [ -n "${DATASET_URL:-}" ]; then
  echo "DATASET_URL provided; downloading..."
  wget -O GOLD_XYZ_OSC.0001_1024.hdf5 "$DATASET_URL"
fi
