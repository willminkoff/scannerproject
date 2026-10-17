#!/bin/bash
# Build usb-reenumerate next to its source. Needs the Xcode Command Line
# Tools (clang); no Xcode project, no signing. Run once per checkout, and
# again after pulling a change to the .c file.
set -euo pipefail
cd "$(dirname "$0")"
clang -O2 -Wall -Werror -framework IOKit -framework CoreFoundation \
    usb-reenumerate.c -o usb-reenumerate
echo "built $(pwd)/usb-reenumerate"
