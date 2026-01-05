#!/bin/bash
set -euo pipefail

sudo systemctl start icecast2
sudo systemctl start icecast-keepalive
sudo systemctl start rtl-airband rtl-airband-ground
sudo systemctl start airband-ui

echo "Scanner stack started."
