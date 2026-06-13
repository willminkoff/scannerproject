#!/usr/bin/env python3
"""Query the chirp RSPduo's available antenna names via SoapySDR."""
import SoapySDR

# Try chirp's exact gr-osmosdr device_args syntax for MA tuner=1 on chirp's RSPduo.
dev_args = "driver=sdrplay,serial=1809063632,mode=MA,tuner=1"
print(f"opening: {dev_args}")
sdr = SoapySDR.Device(dev_args)
print(f"\nlistAntennas(RX, 0): {sdr.listAntennas(SoapySDR.SOAPY_SDR_RX, 0)}")
print(f"getAntenna(RX, 0): {sdr.getAntenna(SoapySDR.SOAPY_SDR_RX, 0)}")
print(f"listGains(RX, 0): {sdr.listGains(SoapySDR.SOAPY_SDR_RX, 0)}")
try:
    print(f"getGainRange(RX, 0): {sdr.getGainRange(SoapySDR.SOAPY_SDR_RX, 0)}")
except Exception as e:
    print(f"getGainRange err: {e}")
del sdr
