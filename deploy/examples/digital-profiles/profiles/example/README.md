Example SDRTrunk profile directory.

Place your SDRTrunk-exported configuration for this profile in this folder.
Do not commit proprietary or sensitive configuration to the repo.

Typical contents are whatever SDRTrunk exports for a working profile, copied here as-is.

Device binding:
- If you need to pin SDRTrunk to a specific RTL-SDR, set `DIGITAL_RTL_DEVICE` (index or serial)
  and reference that value in your SDRTrunk profile configuration.
