Example SDRTrunk profile directory.

Place your SDRTrunk-exported configuration for this profile in this folder.
Do not commit proprietary or sensitive configuration to the repo.

Typical contents are whatever SDRTrunk exports for a working profile, copied here as-is.

Device binding:
- Prefer `DIGITAL_RTL_SERIAL` (dedicated RTL serial) and select that serial in SDRTrunk.
- If you must use indexes, set `DIGITAL_RTL_DEVICE` and reference that value in SDRTrunk.
