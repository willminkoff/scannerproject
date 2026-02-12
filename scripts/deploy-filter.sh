#!/bin/bash
# deploy-filter.sh
# Deployment script for noise filter on Raspberry Pi
# Run this on the Pi: bash deploy-filter.sh

set -e

echo "=== Noise Filter Deployment ==="
echo ""

# Step 1: Install SoX
echo "1/4: Installing SoX audio processor..."
sudo apt-get update
sudo apt-get install -y sox libsox-fmt-all
echo "✓ SoX installed"
echo ""

# Step 2: Make wrapper executable
echo "2/4: Making wrapper script executable..."
if [ -f "/home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh" ]; then
    chmod +x /home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh
    echo "✓ Wrapper script is executable"
else
    echo "⚠ Warning: Wrapper script not found at /home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh"
fi
echo ""

# Step 3: Reload systemd
echo "3/4: Reloading systemd configuration..."
sudo systemctl daemon-reload
echo "✓ Systemd reloaded"
echo ""

# Step 4: Restart services
echo "4/4: Restarting scanner services..."
sudo systemctl restart rtl-airband rtl-airband-ground
echo "✓ Services restarted"
echo ""

# Verification
echo "=== Verification ==="
echo ""
echo "SoX version:"
sox --version
echo ""

echo "Filter config files:"
ls -lh /run/rtl_airband*filter.json 2>/dev/null || echo "  (will be created on first run)"
echo ""

echo "Service status:"
sudo systemctl status rtl-airband --no-pager | head -5
echo ""
sudo systemctl status rtl-airband-ground --no-pager | head -5
echo ""

echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. Check service logs: sudo journalctl -u rtl-airband -n 50"
echo "2. Listen to the stream: http://<pi-ip>:8000/scannerbox.mp3"
echo "3. Adjust filter in UI: open http://<pi-ip>:5050"
