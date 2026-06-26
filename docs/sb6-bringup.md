# SB6 Bring-up — Mac mini scanner stack

How to bring a fresh **2018 Mac mini (Macmini8,1, T2)** back to the **SB6 baseline**
(host `ScannerBox`). This captures the **system-level setup that is NOT in the
`etc/scannerbox/` snapshot** (that snapshot only holds the systemd drop-ins +
digital profiles). Reboot-validated 2026-06-26.

> Repo config (chirp, favorites, UI, systemd drop-ins) is version-controlled.
> Everything below is host/system state that must be re-applied by hand on a
> fresh install. The Micro→Mac-mini migration narrative is in
> [`mac-mini-port.md`](mac-mini-port.md).

---

## 1. Base OS
- **Ubuntu 24.04** + **t2linux kernel** support (Apple T2 hardware).
  - The box currently runs the stock `6.17.0-NN-generic` kernel; mainline is
    sufficient for boot + the rig (NVMe, tg3 ethernet, USB). `apple-bce` is **not**
    required for this headless dock-based rig.
- Key-based SSH + NOPASSWD sudo for `ubuntu`. Reachable at
  `scannerbox.lan` (mDNS) and Tailscale `100.116.45.115`.

## 2. Wi-Fi firmware (BCM4364) — `apple-firmware`
The Apple BCM4364 Wi-Fi firmware is proprietary and **not** in `linux-firmware`.
Install from the t2linux community repo (AdityaGarg8), which ships it pre-extracted:
```
curl -s --compressed "https://adityagarg8.github.io/t2-ubuntu-repo/KEY.gpg" \
  | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/t2-ubuntu-repo.gpg
echo "deb [signed-by=/etc/apt/trusted.gpg.d/t2-ubuntu-repo.gpg] https://github.com/AdityaGarg8/Apple-Firmware/releases/download/debian ./" \
  | sudo tee /etc/apt/sources.list.d/t2.list
sudo apt update && sudo apt install apple-firmware
sudo modprobe -r brcmfmac && sudo modprobe brcmfmac    # wlp3s0 then appears
```
Firmware lands in `/lib/firmware/brcm/` (Macmini8,1 uses the
`brcmfmac4364b2-pcie.apple,lanai.bin` variant). Persists across reboot.

## 3. Wi-Fi profiles (netplan-managed NetworkManager)
This box uses **netplan as NM's backend**, so `nmcli`-created Wi-Fi profiles
persist as **netplan YAMLs in `/etc/netplan/90-NM-<uuid>.yaml`** (mode 600,
passwords stored root-only) — NOT as keyfiles in
`/etc/NetworkManager/system-connections/` (that dir stays empty). Recreate with:
```
sudo nmcli connection add type wifi con-name '<SSID>' ifname wlp3s0 ssid '<SSID>' \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk '<password>' connection.autoconnect yes
```
(Do not commit the YAMLs — they contain PSKs.)

## 4. Headless display: disable gdm3 + enable linger
CRD spawns a real `Xorg :20` that races gdm3 for the GPU at boot. Disable gdm3,
but **enable linger FIRST** so the `ubuntu` user manager (and its PipeWire, which
the scanner audio / BOOM path needs) starts at boot without a graphical login:
```
sudo loginctl enable-linger ubuntu     # REQUIRED before disabling gdm3 (protects BOOM audio)
sudo systemctl stop gdm3 && sudo systemctl disable gdm3
```

## 5. Chrome Remote Desktop → XFCE
GNOME Shell's GL-composited output isn't capturable on the headless CRD display
(black screen + cursor). Use XFCE (xfwm4, non-GL-compositing):
```
sudo apt install xfce4 xfce4-goodies
printf "#!/bin/sh\nexec /etc/X11/Xsession 'startxfce4'\n" > ~/.chrome-remote-desktop-session
chmod +x ~/.chrome-remote-desktop-session
sudo systemctl restart chrome-remote-desktop@ubuntu
```
(`network-manager-gnome` provides the `nm-applet` tray icon in XFCE.)

## 6. op25 antenna patch (lives OUTSIDE this repo)
`/opt/op25/op25/gr-op25_repeater/apps/multi_rx.py` is patched (after
`osmosdr.source()`) to derive + set the antenna from `tuner=N` in the device
args (`Tuner N 50 ohm`). This is in the separate op25 checkout, **not** in
scannerproject — re-apply manually on a fresh op25 install. (It was a red
herring for the SL/Tuner-2 silence but is correct + benign; keep it.)

## 7. USB topology (OWC TB3 dock)
5 xHCI controllers, each a 480M + a USB-3 bus. Keep the two RSPduos on
**separate 480M buses** to avoid dual-tuner USB-2 starvation:
- **airband RSPduo `1809063632` → onboard PCH (`00:14.0` / usb1)**, direct Mac-mini USB-A.
- **digital RSPduo `180903EF32` → OWC dock Fresco controller** (`44:00.0`/usb7 or `45:00.0`/usb9).
- RTLs (VFO/waterfall/spare) share the remaining buses.
- Note: USB-A ports on travel docks route to the **onboard** controller; only
  Thunderbolt/USB-C devices reach the Titan Ridge controllers (usb3/usb5).
- RSPduo is USB-2 → always 480M even on a USB-3 port.
- Distinguish the identical `1df7:3020` duos by serial sticker. When claimed by
  the SDRplay API, sysfs `serial` reads blank — bind via
  `dmesg | grep 'usb 9-2.*SerialNumber'`.

## 8. Apply the repo's /etc snapshot
After the base system is up:
```
bash scripts/apply-etc-snapshot.sh      # rsync etc/scannerbox/ -> /etc + daemon-reload
# then restore the redacted icecast source password:
sudo sed -i 's/__ICECAST_SOURCE_PW__/<pw from /etc/airband-ui.conf>/' \
  /etc/systemd/system/gr-demod@{airband,ground}.service.d/cutover.conf
```
Also restore `data/homepatrol.db` + `data/hp_state.json` (gitignored; pull from
the Micro or a backup) for the HPDB-backed profile UI.

## 9. Reboot + recovery
- Soft reboot self-recovers in ~60s; **AutoBoot on AC loss does NOT fire**
  (T2/SMC governs AC-restore, not the EFI var) — needs macOS or a UPS.
- If a band wedges on device reopen ("no sdrplay device matches"), use
  **`recover-sdrplay.sh`** (stop chirp → restart sdrplay → start MA→SL → restart op25).
  Bare `systemctl restart gr-demod@airband` wedges the device reopen — avoid it.
- Post-reboot quirks: bounce `scanner-vlc-{vfo,ground}` if their mounts publish
  with 0 listeners; BOOM idle-disconnects (`bluetoothctl connect` fails
  `le-connection-abort-by-local` until physically woken — **never change BOOM volume**).
