# macos/clients

Python wrappers the thin UI (`scannerctl`) and Claude use to drive the backends.

## `sdrangel_client.py` — full control (REST :8091)
SDRangel has a rich Swagger/OpenAPI surface. The wrapper covers: instance +
deviceset enumeration, device tuning, channel (AM/NFM demod) settings incl.
squelch, and Frequency-Scanner run control. **Library + CLI.**
```
python3 sdrangel_client.py status
python3 sdrangel_client.py devicesets
python3 sdrangel_client.py set-squelch 0 0 -55
```
⚠️ Endpoint/field names written from docs; **verify against the live Swagger UI**
(`http://<host>:8091/api/`) once SDRangel is running — marked `(verify)` in code.

## `sdrtrunk_client.py` — limited control (logs + playlist)
**Key limitation:** SDRTrunk has **no runtime REST API**. Integration is:
- **Read** decode state → scrape `~/SDRTrunk/logs` (reuse `scripts/sdrtrunk-local-monitor.py`).
- **Change** monitored systems → rewrite the **playlist XML** (`data/hpdb_to_sdrtrunk.py`) + **restart** via launchd.
- **Audio** → CoreAudio (local/BOOM) + Icecast broadcaster (remote).

So digital control is coarser than analog: you reconfigure-and-restart, you don't
tweak live. Plan the UI/Claude flows around that asymmetry.

## Asymmetry summary
| | SDRangel (analog) | SDRTrunk (digital) |
|---|---|---|
| Runtime control | ✅ REST :8091 | ❌ playlist edit + restart |
| Live state read | ✅ REST | ⚠️ log scrape |
| Audio out | CoreAudio + icecast | CoreAudio + icecast/broadcaster |
