#!/bin/bash
set -euo pipefail

sudo systemctl stop airband-ui
sudo systemctl stop scanner-mixer
sudo systemctl stop sdrtrunk
sudo systemctl stop rtl-airband
sudo systemctl stop icecast-keepalive
sudo systemctl stop icecast2

echo "Scanner stack stopped."
