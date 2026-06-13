#!/usr/bin/env python3
"""Probe digital tuner enumeration to understand why we see only 1."""
import sys, os
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui import favorites_runtime as fr

print("CHIRP_RSPDUO_SERIAL env:", os.environ.get("CHIRP_RSPDUO_SERIAL"))
print()
print("_chirp_dedicated_rspduo_serials():", fr._chirp_dedicated_rspduo_serials())
print("_rtl_airband_dedicated_rspduo_serials():", fr._rtl_airband_dedicated_rspduo_serials())
print()
print("_rspduo_usb_serials():", fr._rspduo_usb_serials())
print()
print("_rspduo_tuner_ids(max_tuners=1):", fr._rspduo_tuner_ids(max_tuners=1))
print("_rspduo_tuner_ids(max_tuners=2):", fr._rspduo_tuner_ids(max_tuners=2))
print("_rspduo_tuner_ids(max_tuners=4):", fr._rspduo_tuner_ids(max_tuners=4))
print()
print("_digital_serials():", fr._digital_serials())
