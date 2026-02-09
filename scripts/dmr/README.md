# DMR Scripts

Manual test (single frequency):

```bash
DMR_DEFAULT_FREQ=461.0375 ./rtl_fm_dmr.sh | ./dsd_wrapper.sh | aplay -f S16_LE -r 48000 -c 1
```

These scripts target conventional DMR voice. Trunking control/channel following is not handled here.

Default Icecast mount is `/GND.mp3` (override with `DMR_MOUNT`).

Device selection uses `SCANNER2_RTL_DEVICE` from `/etc/airband-ui.conf`.
