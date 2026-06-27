# macOS bring-up checklist

Ordered steps to take a **fresh macOS install on the Mac mini** (2018 Intel/T2) to a
working SDRangel + SDRTrunk scanner with Claude/Dispatch access. Do them in order.

> Phase-1 note: do this first on **external media (USB/TB SSD)** so the internal
> SB6 Ubuntu stays as rollback. Don't wipe internal until Phase 1 passes.

## 1. macOS base settings
- [ ] Sign in / skip Apple ID as preferred; set hostname (`scutil --set HostName mac-mini`; also ComputerName + LocalHostName).
- [ ] **System Settings → General → Sharing:** enable **Remote Login (SSH)** and **Screen Sharing**.
- [ ] **Firewall:** allow incoming for sshd, SDRangel, SDRTrunk, the scannerctl port. (Or leave firewall off on a trusted LAN.)
- [ ] Disable App Nap / "put hard disks to sleep" so background scanners don't get throttled (Energy settings).
- [ ] Create/confirm the run user (e.g. `willminkoff`).

## 2. Tailscale (remote + Claude access)
- [ ] Install Tailscale (`brew install --cask tailscale` or the App Store app), sign in, confirm the node appears on the tailnet.
- [ ] Note the tailnet name/IP (replaces `scannerbox.lan` / `100.116.45.115`).

## 3. SSH key for Claude access  ← critical for Path B (conversational control)
- [ ] Ensure `~/.ssh` exists; add Claude's public key to `~/.ssh/authorized_keys` (same model as `ubuntu@scannerbox` today).
- [ ] Test: `ssh willminkoff@<tailscale-name> 'echo ok'` non-interactively from the Claude environment.
- [ ] Configure passwordless `sudo` for the run user if Claude needs privileged ops (or document which ops need it).
- [ ] **GitHub auth:** generate a new key for git push from the Mac (the old `scannerbox_github` deploy key is Linux-side); add to the repo's deploy keys (write) OR use Will's account creds.

## 4. Homebrew + Java
- [ ] Install Homebrew (https://brew.sh).
- [ ] `brew install openjdk@21` (SDRTrunk needs Java 21+); symlink into `/Library/Java/JavaVirtualMachines/` so the GUI launcher finds it (see `mac-install-sdrtrunk.sh`).

## 5. SDRplay API
- [ ] Install the SDRplay API (vendor pkg, 3.15.x) — installs `/Library/SDRplayAPI/...` + the `com.sdrplay.service` LaunchDaemon (RunAtLoad + KeepAlive).
- [ ] Plug in BOTH RSPduos. Confirm the daemon is up: `pgrep sdrplay_apiService`.

## 6. SDRangel (analog)
- [ ] Run `scripts/mac-install-sdrangel.sh` (installs to /Applications/SDRangel.app).
- [ ] **Plug SDRs in BEFORE launching** (it enumerates at startup — see `docs/sdrangel-scan-38380.md`).
- [ ] Enable the **REST API** (Preferences → enable Web/REST on **:8091**) — needed by `scannerctl` + Claude.
- [ ] Smoke test: add `SDRPlayV3[0]`, AM demod (R0:0), NFM demod (R0:1), Frequency Scanner (R0:2), import a CSV from `data/`.

## 7. SDRTrunk (digital P25)
- [ ] Run `scripts/mac-install-sdrtrunk.sh` (installs to `~/SDRTrunk`).
- [ ] **Build + install JMBE** (voice): `bash macos/install/jmbe-build.sh` — SDRTrunk won't decode voice without it.
- [ ] Launch via `scripts/mac-start-sdrtrunk.sh`; add the SDRplay RSPduo tuner; import the playlist from `data/hpdb_to_sdrtrunk.py` output.
- [ ] **Definitive P25 test:** confirm it locks **MTRTRS** (P25 Phase II) on this antenna. (TACN may still not decode — RF/site, not decoder.)
- [ ] Enable the **Icecast broadcaster** (Playlist → Broadcaster) for remote audio if desired.

## 8. BOOM Bluetooth
- [ ] Pair the UE BOOM 2 (`C0:28:8D:34:6E:67`) in System Settings → Bluetooth.
- [ ] Set it as the audio output for SDRangel/SDRTrunk (CoreAudio). **Never script BOOM volume changes** (standing rule).

## 9. Auto-start (launchd)
- [ ] Adjust + load the `macos/launchd/*.plist` templates (`launchctl bootstrap gui/$(id -u) <plist>`): SDRangel, SDRTrunk, scannerctl.

## 10. Thermal proof (the whole point)
- [ ] Under full scanning load, confirm the **fan ramps** and CPU stays well under 100°C (`sudo powermetrics --samplers smc -i1 -n1` or iStat). This is what Linux couldn't do.

## 11. Verify Claude/Dispatch end-to-end
- [ ] From a Claude task: SSH in, hit SDRangel REST `:8091`, read SDRTrunk logs, `launchctl list`. Path B working.
