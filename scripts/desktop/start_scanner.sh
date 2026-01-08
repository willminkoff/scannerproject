#!/bin/bash
set -euo pipefail

sudo systemctl start icecast2
sudo systemctl start icecast-keepalive
sudo systemctl start rtl-airband
sudo systemctl start sdrtrunk
sudo systemctl start scanner-mixer
sudo systemctl start airband-ui

echo "Scanner stack started."
