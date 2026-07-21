# Phase 0 status — Neptune measurement pass

**Date:** 2026-07-16 · **Box:** Neptune (Mac mini 2021, M1, macOS Tahoe 26.5.2, arm64)
**Access:** `willminkoff@100.102.3.20` · **Boot:** Wed 15 Jul 2026 15:38:34 (uptime 1d 3h)
**Scope:** read-only measurement against `docs/sb3-neptune-architecture.md` @ `98f7356` (§5, §6 Phase 0, §7.6).
**Nothing on the box was modified, restarted, or reconfigured.**

> ## 🟡 **PHASE 0: COMPLETE, WITH HARDWARE CAVEATS** — updated 2026-07-16 evening
>
> **Every measurement Phase 0 existed to take has been taken, and the topology question is
> fully answered — better than the plan assumed** (§1). What remains is **three items that
> need Will's hands** and cannot be closed remotely (§6). None of them block repo-side work,
> and **Phase 1 has proceeded**: the scaffold is delivered on **`sb3-phase1-scaffold`
> (`95caf4b`)**, dry-run only, `--execute` refused.
>
> | | Status |
> |---|---|
> | USB controller map | ✅ **measured** — 3 native controllers, no dock needed |
> | §7.6 Q5 + Q6 | ✅ **both closed** |
> | "Both NESDRs off-bus" premise | ✅ **disproven** — both enumerate |
> | Fleet policy rev 5.0 | ✅ **landed** — `62fddc5` |
> | SoapyHackRF install | ✅ **already present** in radioconda |
> | `SOAPY_SDR_PLUGIN_PATH` gotcha | ✅ **confirmed + path corrected** |
> | **HackRF on the bus** | 🔴 **NOT ENUMERATING** — needs a cable swap (§3) |
> | **RTL `56919602` at 12 Mb/s** | 🔴 **needs hands** — cable/hub-port/PSU |
> | **VIA hub external PSU** | 🟡 **unconfirmed** — check before condemning the dongle |
>
> **The HackRF bring-up sequence is written and queued for tomorrow** — enumeration + serial +
> firmware, controller placement, SoapySDR probe, first sweep, and the 8/10/16/20 Msps
> sustained-rate bench with drop counters. It runs the moment the device appears on the bus.

---

## 0. Headline — read this first

> ### 🔴 `neptune-angel.mp3` was already DOWN when this pass started, and stayed down.
>
> The task brief said *"neptune-angel.mp3 must stay 200 throughout Phase 0 measurement."*
> **It was 404 before the first measurement and 404 after the last one.** It is not merely
> silent — **the mount does not exist in icecast at all.** The analog/airband path on Neptune
> has been dead since the 2026-07-15 15:38 boot, ~27 hours before this pass.
>
> **This was not caused by the measurement pass.** Proof: `neptune-trunk.mp3` (SDRTrunk
> digital) held **200 continuously**, and its icecast `stream_start` is still
> `Wed, 15 Jul 2026 15:39:15` — unchanged start-to-finish. All PIDs unchanged.

| Mount | Before | After | Notes |
|---|---|---|---|
| `neptune-trunk.mp3` | **200** | **200** | ✅ SDRTrunk P25, `stream_start` unchanged, never dropped |
| `neptune-angel.mp3` | **404** | **404** | 🔴 **Pre-existing outage.** Mount absent from icecast |

The premise that Neptune "is currently running SDRangel with the recent airband + FRS/GMRS
work" is **not true of the live box**. SDRangel is running, and the airband *channel config*
survives — but it is bound to a **phantom device**. See §4.

---

## 1. USB controller topology — MEASURED

`system_profiler SPUSBDataType` **returns empty over SSH on Tahoe** (0 lines — it needs a GUI
session). `ioreg -p IOUSB` is the authoritative substitute and is what this section uses.
**Phase 0's prescribed command does not work headless; use `ioreg`.**

### Neptune has THREE independent xHCI controllers — today, with no dock

| Controller | `locationID` | Physical (inferred) | Devices |
|---|---|---|---|
| `AppleT8103USBXHCI@00000000` | `0x0` | USB-C / TB port | VIA Labs hub tree → **all 3 RTLs** + USB3 hub + 2 card readers |
| `AppleT8103USBXHCI@01000000` | `0x1000000` | USB-C / TB port | **EMPTY — nothing attached** |
| `AppleEmbeddedUSBXHCIFL1100@02000000` | `0x2000000` | **USB-A pair** | **RSPduo `180903EF32`, alone** |

The M1 Mac mini drives its two USB-A ports with an **embedded Fresco Logic FL1100** — the same
controller family that de-stacked the Micro, except here it is *already on the SoC*. The two
USB4/Thunderbolt ports each get their **own** `AppleT8103USBXHCI`.

### This overturns the doc's §5.2 assumption — in Neptune's favour

> §5.2 predicted: *"Realistically that is **one USB-2 domain shared by the two USB-A ports**,
> plus whatever each Thunderbolt port's attached device brings. **Not 5 domains. Probably 1,
> expandable to 3.**"*

**Measured: 3 independent controllers already exist, natively, with nothing plugged into
Thunderbolt.** The doc was right that a hub never makes a domain and right that only a
controller does — it just under-counted the controllers Neptune ships with.

### §7.6 open questions — BOTH CLOSED

**Q6 — Do Neptune's two Thunderbolt ports share a USB controller? → ✅ NO.**
Each has its own `AppleT8103USBXHCI` (`@00000000`, `@01000000`), on two separate
Thunderbolt/USB4 buses (`SPThunderboltDataType`: Bus 0 and Bus 1, distinct Domain UUIDs).
**The no-dock fallback in §5.3 is alive.** This also answers the sibling question
`sb7-northstar-program.md:227` left open: **yes, the two USB-A ports DO share one controller**
(the single FL1100) — currently harmless, because only the RSPduo is there.

**Q5 — Is the OWC TB3 dock available? → Not attached, and no longer needed.**
Both Thunderbolt buses report `Status: No device connected`. Whether the dock is physically
free is still unknown (needs Will), but **the layout no longer depends on it.** Keep it as
spare capacity, not a dependency.

### Recommended topology — §5.3's 3 domains, for $0, no dock, no purchase

| Domain | Controller | Device(s) | Load / ~300 Mbps | Status |
|---|---|---|---|---|
| **A** | `T8103@01000000` (free TB port) | **HackRF — alone** | 160–320 Mbps · 55–100% | ⚠️ needs the HackRF plugged in |
| **B** | `FL1100@02000000` (USB-A) | RSPduo `180903EF32` — alone | ~128 Mbps · ~43% | ✅ **already correct** |
| **C** | `T8103@00000000` (TB port + VIA hub) | 3 RTLs | ~98 Mbps · ~33% | ✅ **already correct** |

**Both §5.3 non-negotiables are already satisfied by the current cabling**: HackRF and RSPduo
would be on separate xHCI controllers, and neither shares with the RTL hub. The only action is
to put the HackRF in the **free Thunderbolt port** — not the hub, not USB-A.

The hub already installed is **VIA Labs**, which is the doc's own recommended chipset family
(VL817/VL822, §5.2). ⚠️ **Unverified: whether it is externally powered** — see §6.

**No hub purchase is required.** Phase 0's *"Install the powered USB-3 hub for the 3 RTLs"*
item is already done, pending the power confirmation.

---

## 2. SDR inventory + wedge status — MEASURED

IOKit `"Device Speed"`: `0`=low 1.5M · `1`=**full 12M** · `2`=**high 480M** · `3`=super 5G.

| # | Device | Serial | Speed | Controller | Status |
|---|---|---|---|---|---|
| 1 | **RSPduo** | `180903EF32` | **2** ✅ 480 | `FL1100@02` (alone) | ✅ **HEALTHY** — SDRTrunk decoding, `neptune-trunk.mp3` 200 |
| 2 | **RTL Blog V4** | `83241970` | **2** ✅ 480 | `T8103@00` / VIA hub `@00130000` | 🟡 on bus; SDRangel enumerates it; **not bound to any deviceset** |
| 3 | **NESDR SMArt v5** | `61108285` | **2** ✅ 480 | `T8103@00` / nested hub `@00112000` | 🟡 on bus at full speed; **SDRangel does not see it** |
| 4 | **NESDR SMArt v5** | `56919602` | **1** 🔴 **12 Mbps** | `T8103@00` / nested hub `@00113000` | 🔴 **LINK FAULT** — enumerated at full speed |
| 5 | **HackRF One** | — | — | — | 🔴 **NOT PLUGGED IN** |

### 🔴 Correction to the doc: "both NESDRs are off-bus" is WRONG as measured

§5.1 states *"Both NESDRs are known-wedged"* and §5.4 #2 *"Both NESDRs off-bus after reboot.
Recurring."* citing `reference_two_box_audio_harness`.

**Measured: both NESDRs are ON the bus.** `ioreg` finds each exactly once — the count is 1,
not 0. The doc's own discipline (§5.4: *"Check `ioreg` before attempting any software
recovery — if the count is 0, every software remedy is moot"*) **cuts the other way here:**
the count is not 0, so this is a software/link problem and reboot is aimed at the wrong target.

The two NESDRs have **two different, unrelated faults**, and neither is "off-bus":

- **`56919602` — enumerated at 12 Mbps (full speed), not 480.** This is the exact
  cable/port-fault signature §5.3 tells us to check for (*"12 Mb/s = cable/port fault — the
  `VDL2A001` failure mode"*). An RTL-SDR needs ~33 Mbps for 2.048 Msps; **at 12 Mbps it
  physically cannot deliver its stream.** It will open and then starve. This is a real,
  measured hardware fault and it needs Will's hands.
- **`61108285` — on the bus at a clean 480, but invisible to SDRangel.** No link fault. This
  is an enumeration/boot-race problem, not a hardware one.

**Why this correction matters:** "off-bus, needs reboot" and "on-bus at the wrong speed" have
opposite remedies. Rebooting Neptune will not fix a bad cable, and it is the expensive move.
The doc's Phase 0 item *"Root-cause and fix the two wedged NESDRs. Power? Enumeration?
Tahoe/libusb?"* now has a measured answer: **`56919602` = link/power; `61108285` = enumeration.
Not Tahoe/libusb, and not a reboot.**

### 🔍 UPDATE 2026-07-16 evening — `61108285` re-enumerated between reads

Re-measuring during the HackRF attempt caught the RTL tree changing underneath us:

| | First read | Second read |
|---|---|---|
| `61108285` IOKit ID | `0x100004b05` | **`0x100004fea`** |
| `61108285` busy timer | 17 ms | **reset to 15 ms** |
| tree position | listed before `56919602` | **listed after** |

**A new IOKit ID plus a reset busy timer means the device re-enumerated** — someone was
physically working the hub, which matches Will being at the box. Useful as corroboration that
the ioreg reads are live rather than cached, and worth noting for the boot-race theory below:
`61108285` re-enumerating *after* SDRangel started is exactly the mechanism that would leave
it out of SDRangel's device list.

**Two things did NOT change, and both still need hands:**

- 🔴 **`56919602` is still `Device Speed = 1`** — still 12 Mb/s. Whatever was touched, this
  wasn't fixed. Still needs a cable/port swap, and the hub PSU checked first (§6).
- 🔴 **The HackRF still never appeared** (§3).

### SDRangel sees only ONE of three RTLs

`GET /sdrangel/devices?direction=0` returns exactly one real RTL and one real SDRplay:

```
RTLSDR       seq=0  serial=83241970     ← the Blog V4, the only RTL SDRangel knows about
SDRplayV3    seq=0  serial=180903EF32   ← visible, but SDRTrunk owns it (correctly, §5.4 #3)
AaroniaRTSA / AudioInput / FileInput / SigMFFileInput / KiwiSDR /
LocalInput / RemoteTCPInput / RemoteInput / TestSource   ← software pseudo-devices
```

Neither NESDR appears. This list is built at SDRangel startup — consistent with a **boot race**:
SDRangel launched at 15:38:34 and enumerated before the nested VIA hub finished bringing up the
two NESDRs behind it.

### ⚠️ RTL probing deliberately stopped early

`SoapySDRUtil --find="driver=rtlsdr"` (with the plugin path corrected, §3) found two R820T
tuners and then printed **`Detaching kernel driver failed!`** and returned **no device list**.

**I stopped RTL probing there rather than escalating to `rtl_test`.** Rationale: further open
attempts against dongles in this state risk wedging one, and **Will is driving I-81 and cannot
replug anything.** `ioreg` already answers bus presence and speed authoritatively, which is what
Phase 0 actually asked for. `rtl_test -t` from the brief was **not run** for this reason.

---

## 3. HackRF bring-up — device ABSENT, software ALREADY READY

> ### 🔴 **UPDATE 2026-07-16 evening — physical insertion ATTEMPTED. LEDs lit. Host still sees nothing.**
>
> Will plugged the HackRF in and reports its **LEDs are lit**. The Mac still does not see it,
> and re-measurement confirms the device is **not on the bus**:
>
> ```
> hackrf_info                    → No HackRF boards found.   (libhackrf 2026.01.3 / 0.9.2)
> Total USB devices              → 12   (UNCHANGED from before the insertion)
> idVendor set                   → 1507, 3034, 7671, 8457
>                                  ← no 7504 (0x1d50 Great Scott Gadgets). No 0x6089, no 0x604b.
> AppleT8103USBXHCI@01000000     → still ZERO child devices
> USB port events in system log, last 10 min → NONE
> ```
>
> #### Lit LEDs and this finding AGREE — that agreement is the diagnosis
>
> **LEDs prove VBUS. They do not prove data.** The HackRF's power rails come up from bus
> voltage alone, so the board boots and lights whether or not D+/D− are connected.
>
> **The decisive measurement is the log, not the LED.** If the host had seen *any* electrical
> attach — a device that enumerated and dropped, a failed negotiation, an overcurrent trip, a
> port reset — macOS would have logged a port event. **There are none.** From the Mac's point
> of view, nothing was ever plugged in. Board powered + host blind + zero log events = a
> **data-path** failure, not a device failure.
>
> This also rules out the entire software layer: `ioreg` is the kernel's own registry, below
> any driver, permission, or libusb concern. **If `ioreg` doesn't see it, no software fix
> exists** — the same discipline §5.4 applies to the RSPduo: a zero bus count makes every
> software remedy moot.
>
> #### Ranked causes — cable first
>
> 1. **🔴 Charge-only micro-USB cable.** The classic HackRF One failure, and it produces
>    *exactly* this signature. A large share of micro-USB cables in circulation carry power
>    only. **Swap for a known-data cable — try this first.** Cheap proof: the same cable plus
>    any micro-USB device that should mount. If that doesn't appear either, it's the cable.
> 2. **🟡 Not landing on the Mac.** Confirm it's seated in the Mac's free Thunderbolt port and
>    not a charger, a monitor passthrough, or an unconnected hub leg.
> 3. **🟡 Hub power, if it went into the VIA hub.** A HackRF draws up to ~500 mA, the hub's PSU
>    is **unconfirmed**, and RTL `56919602` is *already* negotiating 12 Mb/s on that hub — a
>    marginal-power signature. It belongs in the free TB port regardless (§5.3).
>
> **Everything downstream is queued, not blocked-by-software.** The free controller
> (`AppleT8103USBXHCI@01000000`) is still empty and reserved, and the software is already in
> place. The moment it enumerates: serial + firmware, controller placement, SoapySDR probe,
> first sweep, and the 8/10/16/20 Msps drop-counter bench.
>
> ⚠️ **Neptune has neither `timeout` nor `gtimeout`**, so the wall-clock-bounded bench commands
> in the brief will not run as written. Use `hackrf_transfer -n <num_samples>` instead — a
> fixed sample count is the better instrument anyway, since it makes drop counts directly
> comparable across the four rates rather than varying with process lifetime.

**Present on the bus: NO.** `ioreg -p IOUSB` grep for `hackrf|great scott|0x1d50` → **0 matches**.
`hackrf_info` (version `2026.01.3`, libhackrf `2026.01.3 (0.9.2)`) → **`No HackRF boards found.`**

| Phase 0 HackRF item | Result |
|---|---|
| Enumerates / serial / firmware | 🔴 **BLOCKED — not plugged in.** Needs Will's hands |
| Pin serial into policy | 🔴 blocked (no serial to read) |
| **Install SoapyHackRF** | ✅ **ALREADY INSTALLED — no action needed** |
| SoapySDR sees it via the right plugin path | 🟡 module present + loads; device absent |
| First `hackrf_sweep` capture | 🔴 blocked |
| Sustained-rate bench (8/10/16/20 Msps + drop counters) | 🔴 blocked |

### ✅ Good news: the SoapyHackRF worry in §5.1 / Phase 0 is MOOT

The doc budgets real effort here — *"Try `brew install soapyhackrf` first… but Homebrew's
SoapySDR and radioconda's are different installs with different module paths… If the brew route
doesn't land the module where radioconda's SoapySDR looks, **build from source against
radioconda's SoapySDR**."*

**None of that is necessary. radioconda already ships both halves:**

```
~/radioconda/lib/SoapySDR/modules0.8/libHackRFSupport.so     ← the module, already in place
~/radioconda/bin/hackrf_info hackrf_sweep hackrf_transfer
                 hackrf_biast hackrf_clock hackrf_cpldjtag
                 hackrf_debug hackrf_operacake hackrf_spiflash   ← full CLI suite
```

**Homebrew has no SoapySDR and no HackRF at all** (`brew list | grep -iE 'hackrf|soapy'` → empty;
`/opt/homebrew/lib/SoapySDR/` does not exist). So there is **exactly one SoapySDR on the box** —
radioconda's — and the brew-vs-radioconda path ambiguity the doc feared **cannot arise.** Delete
that concern from Phase 0.

### ⚠️ The `SOAPY_SDR_PLUGIN_PATH` gotcha is REAL and CONFIRMED — but not the predicted shape

The doc predicted *"A HackRF that `hackrf_info` finds but SoapySDR doesn't is the failure mode to
expect."* The actual bug is **worse and broader**: it is not HackRF-specific, and it hits
**every** driver.

`SOAPY_SDR_PLUGIN_PATH` is **unset**. With it unset, SoapySDR tries to `dlopen` the radioconda
**root directory** as if it were a module and loads **nothing at all**:

```
[ERROR] SoapySDR::loadModule(/Users/willminkoff/radioconda)
  dlopen() failed: ... '/Users/willminkoff/radioconda' (not a file)
No devices found! driver=rtlsdr        ← zero drivers, not just HackRF
```

Setting it explicitly fixes it (RTLs then enumerate):

```bash
export SOAPY_SDR_PLUGIN_PATH=$HOME/radioconda/lib/SoapySDR/modules0.8
```

> ### 📌 Correct the doc's Phase 0 command — the path is wrong
> §5.1 / Phase 0 gives:
> `SOAPY_SDR_PLUGIN_PATH=/opt/scannerproject/radioconda/lib/SoapySDR/modules0.8 SoapySDRUtil --find="driver=hackrf"`
>
> **On Neptune radioconda lives at `$HOME/radioconda`, not `/opt/scannerproject/radioconda`.**
> That path does not exist on this box. Any Phase 5 disco work inheriting it silently loads zero
> modules and reports "no devices" — which reads as a hardware fault. This is precisely the
> *"catching it here costs an hour; catching it at Phase 5 costs a day of misdiagnosis"* case the
> doc wanted, so it is worth landing the fix now.

**Bottom line:** the HackRF is a **plug-and-test**, not an integration. Software is ready, and
the free Thunderbolt port at `T8103@01000000` gives it a dedicated controller on arrival.

---

## 4. Root cause: why `neptune-angel.mp3` is 404

A clean five-link chain, every link verified:

1. **SDRangel's only deviceset is a phantom.** `DS0` is
   `hwType=AaroniaRTSA, serial=None, state=idle, centerFrequency=1450000` (1.45 MHz — a default,
   nowhere near airband). `AaroniaRTSA` is SDRangel's index-0 fallback when the intended device
   isn't bound. The airband channel config **survives on it**:
   `ch[0] AMDemod "135.100 KPHL Tower"` — *Philadelphia*, matching the §7.5 stale-doc trail.
2. **No real device → no samples → no `copyToUDP`.** Nothing is ever written to `udp:9998`.
3. **The ffmpeg bridge is healthy and starving.** PID 680, alive 1d3h, `lsof` confirms it bound
   to `UDP *:9998`, correctly aimed at
   `icecast://…@127.0.0.1:8000/neptune-angel.mp3`. It has never received a byte — so it never
   connects to icecast, so **the mount is never created.** That is why this is **404 (absent)**
   rather than a silent 200. Matches memory `reference_two_box_audio_harness`: *"mount is
   DYNAMIC."*
4. **`sdrangel-restore.py` — the thing that would fix link 1 — is doubly disabled:**
   - 🔴 **`~/scannerproject/.sdrangel-restore-paused` exists, dated Jul 12 20:47** — four days
     before the boot that broke this.
   - 🔴 **No restore LaunchAgent is loaded** (`launchctl list | grep -c restore` → 0), and **no
     restore plist exists** in `~/Library/LaunchAgents/`. The script is on disk and executable;
     nothing invokes it. No crontab either.
5. **`copytoudp-watchdog` is not loaded either.** Its plist is present in
   `~/Library/LaunchAgents/` but `launchctl print … copytoudp-watchdog` → *"Could not find
   service."* So the tap-arming half that §4.4 relies on **is not running on Neptune.**

Loaded agents (the entire SB layer that is actually alive):
```
633  com.scannerproject.neptune-audio-bridge   ← ffmpeg, starving on udp:9998
636  com.scannerproject.icecast                ← up; serving only neptune-trunk.mp3
637  com.scannerproject.sdrangel               ← up, phantom DS0, no real device
650  com.scannerproject.sdrtrunk               ← up, decoding P25 ✅
643  com.scannerproject.caffeinate
```
`tuner-broker`, `copytoudp-watchdog`, `airband-ui`, `chirp-*`, `vfo` — **plists on disk, none
loaded.**

### 📌 This is the fail-OPEN sentinel bug — Phase 1 already predicted it, and here is the damage

Phase 1 says: *"Fail-**closed** sentinel: `$SB3_STATE/killed` missing ≠ permission to reconcile.
Positive state required. **(This inverts the `.sdrangel-restore-paused` bug.)**"*

**This outage is that exact bug, observed in the wild.** A pause sentinel dropped on Jul 12 —
presumably for a few minutes of debugging — silently persisted through a Jul 15 reboot and has
kept Neptune's analog path dead for ~27 hours **with every process reporting healthy.** No
alarm, no non-zero exit, no log line anyone saw. `launchctl` says five green agents. It is the
textbook *"useful liar"* / *"third state"* that `sb7-northstar-program.md` is named for.

**Design consequence — the strongest argument yet for building `status` first:** `sb3-ctl status`
as specified in §4.3 would have caught all of this in one command on day one. That vindicates the
doc's own call that *"`status` and `kill` are the real deliverables."*

⚠️ **Also worth knowing:** `~/scannerproject` **on Neptune is not a git repo** (`git rev-parse` →
*"fatal: not a git repository"*). It is an unversioned copy. Phase 1 deployment needs to reckon
with that — there is no `git pull` deploy path on this box today.

**I did not fix any of this.** Restoring the mount is a write, the brief said measurement-only,
and the pause sentinel is deliberate-looking human state I will not silently clear. See §7.

---

## 5. Phase 1 kickoff sketch — the smallest useful `sb3-ctl` commit

### Where it lives

`broker/` is the pattern to copy: a Python package with `__main__.py`, run as `python -m broker`,
driven by a launchd plist, hard-failing on bad policy (`broker/policy.py` exit 3). Mirror it.
**`bin/` exists in the repo and is empty — free real estate for the shim.**

```
bin/sb3-ctl                       ← thin shim → `exec python3 -m sb3 "$@"`
sb3/__init__.py
sb3/__main__.py                   ← argparse: status | kill | resume | apply | diff
sb3/ownership.py                  ← §4.2 encoded as DATA (the live check — see below)
sb3/backends.py                   ← read-only probes: launchctl print, icecast status-json.xsl
sb3/state.py                      ← $SB3_STATE + the fail-CLOSED sentinel
tests/test_sb3_ownership.py       ← the boundary test
etc/mac/launchd/neptune/com.scannerproject.sb3-reconciler.plist   ← empty agent
```

### Where the §4.2 ownership diagram becomes a live check → `sb3/ownership.py`

Encode the §4.2 table as **data, not prose** — one row per state: `(state, owner, on_kill)`.
Two frozen sets of launchd labels:

```python
SB3_LAYER    = {tuner-broker, sb3-reconciler, sb3-ui, disco, acars, survey}   # die on kill
BACKEND_KEEP = {sdrangel, sdrtrunk, icecast, neptune-audio-bridge,
                copytoudp-watchdog}                                            # never touched
```

Then the boundary stops being a diagram and starts being enforced in three places:

1. **`sb3-ctl status`** renders the table against live `launchctl` + live mounts.
2. **`sb3-ctl kill`** walks only `SB3_LAYER`, in the §4.3 order (consumers → broker last).
3. **`tests/test_sb3_ownership.py`** asserts `SB3_LAYER ∩ BACKEND_KEEP = ∅` **and that every
   `com.scannerproject.*` label observed on the box falls in exactly one set.** An unclassified
   label is a **hard failure**. That is the check that keeps the boundary honest: a new agent
   added in Phase 4 cannot silently escape the taxonomy, which is exactly the *"find out at Phase
   4 that the boundary was never real"* failure Phase 1 exists to prevent.

### The smallest kickoff commit — prints boundaries, kills nothing

- `sb3/ownership.py` — the two sets + the `(state, owner, on_kill)` table. **Pure data, no side
  effects, no imports beyond stdlib.**
- `sb3/__main__.py`:
  - `status` → probe `launchctl list` + each mount, classify every label, print the table, exit 0.
    **Fully real, and immediately useful — it would have caught today's outage.**
  - `kill` → **`--dry-run` behaviour only.** Prints the §4.3 teardown order and the mounts it
    *would* verify, then **refuses without `--force`, which is unimplemented.** Gets the ordering
    committed and reviewable — the part §6 warns *"is hard to retrofit"* — with zero risk.
  - `resume` / `apply` / `diff` → `NotImplementedError`, exit 2.
- `bin/sb3-ctl` shim.
- `tests/test_sb3_ownership.py`.

**Nothing in this commit can stop a process.** It reads `launchctl` and `curl`s mounts. That is
the right first commit: it makes the §4.2 boundary executable and reviewable before it is ever
load-bearing.

**Gate:** Phase 0 still owes `sdr_fleet_policy.json` **rev 5.0** (§7.5 — the reversed RSPduo
serials). `broker/policy.py` hard-fails on a bad policy, so that genuinely blocks Phase 1.
**Today's `ioreg` independently confirms the correction: `180903EF32` is physically on Neptune,
on the FL1100, held by SDRTrunk.** Rev 4.1's claim that it *"was physically UNPLUGGED and taken
to a DIFFERENT computer"* is now disproven by direct measurement, not just by inference from
`sdrangel-restore.py`. **Memory `reference_rspduo_serial_assignment` and §5.1 are confirmed
correct.**

---

## 6. 🔧 Physical actions needed from Will (blocked until onsite)

Ordered by value.

1. **🔴 Plug in the HackRF — into the FREE Thunderbolt/USB4 port.**
   **Not the VIA hub. Not a USB-A port.** The free port is the one on the empty
   `AppleT8103USBXHCI@01000000` controller — i.e. **whichever TB port does *not* currently have
   the VIA hub in it.** That lands the HackRF alone on its own xHCI, satisfying §5.3's
   non-negotiable with no dock and no purchase. Software is already in place; bring-up should
   then be `hackrf_info` → serial → sweep, in minutes.
2. **🔴 NESDR `56919602` — replug + swap cable.** It is on the bus at **12 Mbps instead of 480**.
   Move it to a **top-level port on the VIA hub** (it is currently behind a *nested* hub at
   `@00110000`) and **use a different cable**. If it still comes up at Speed 1, the dongle or that
   hub port is faulty and it is a hardware swap.
3. **🟡 Confirm the VIA hub has its external PSU connected.** Three RTLs plus two nested hubs on
   bus power is a brownout candidate, and §5.2 is explicit that **brownout presents as
   intermittent USB errors — i.e. as a software bug, for hours.** A 12 Mbps link negotiation is
   exactly what marginal power looks like. **Check this before replacing the dongle.**
4. **🟡 Confirm the physical port map.** Which port has the VIA hub; which has the RSPduo. My
   mapping (TB→hub, USB-A→RSPduo) is **inferred from controller class**, and it is sound — the M1
   mini's USB-A ports are FL1100-driven — but two seconds of looking beats an inference.
5. **🟢 Is the OWC TB3 dock physically available?** Closes §7.6 Q5 for the record. **No longer
   needed** — keep as spare.
6. **🟢 Read the SDRTrunk tuner-label strings** (`View → Tuners`). Needs the GUI; can't be done
   over SSH. Phase 0 wants these because §3.5's in-repo formats disagree. A screenshot suffices.

**Not blocked on Will:** `sdr_fleet_policy.json` rev 5.0 + the §7.5 cleanup, and the Phase 1
`sb3-ctl` scaffold (§5). Both are repo-side and can proceed now.

---

## 7. ~~Open decision~~ ✅ **ANSWERED — the pause is intentional; analog stays quiet**

> ## **Will, 2026-07-16: "Pause is intentional. DO NOT restore `neptune-angel.mp3`. Leave the
> marker in place. Don't touch the phantom deviceset. Neptune's analog is deliberately quiet."**
>
> **Nothing below was done.** `.sdrangel-restore-paused` remains untouched (Jul 12 20:47), DS0
> remains the `AaroniaRTSA` phantom, and `neptune-angel.mp3` remains 404 **by design**.
>
> **Consequences now baked into the tooling:**
> - `neptune-angel.mp3` is **excluded from the Phase 0 invariant** — it is not a failure.
> - `sb3-ctl kill`'s invariant check reports it as *"was already down; not ours"* and does not
>   fail on it. `kill` is accountable for what it breaks, not what it inherited
>   (`sb3/killswitch.py`, `95caf4b`).
> - The §4.6 lesson still stands and is independent of this decision: a *deliberate* pause and
>   an *forgotten* pause are indistinguishable to every check on the box. That is exactly why
>   §4.4's sentinel must fail **closed** — the fix is not "don't pause", it's "make absence
>   mean refuse-to-act rather than resume-clobbering."

The original analysis is retained below as the reasoning trail.

**Not done, deliberately.** The brief said measurement-only, and the `.sdrangel-restore-paused`
sentinel is **deliberate-looking human state from Jul 12** — clearing something Will put there on
purpose is not a call to make unilaterally while he's driving. *(That instinct was right: Will
confirmed the pause is intentional.)*

The likely recovery is small and reversible, and does **not** require physical access:

```bash
rm ~/scannerproject/.sdrangel-restore-paused        # ← was this pause still intentional?
~/scannerproject/macos/bin/sdrangel-restore.py      # rebinds DS0 → RTLSDR 83241970, re-arms copyToUDP
```

Three things to weigh first:

- **Why is the sentinel there?** If Jul 12's pause was for a reason that still holds, removing it
  is wrong.
- **The channel is `135.100 KPHL Tower` — Philadelphia, not Nashville.** Restoring blind would
  bring back a *Philly* airband config. The brief mentions *"recent airband + FRS/GMRS work"*,
  which may be unsaved GUI state, or may be on Venus.
- **Restoring only fixes the Blog V4 path.** The two NESDRs stay broken regardless — one needs a
  cable (§6.2), one needs an SDRangel rescan.

Also needs a decision: **should `sdrangel-restore` and `copytoudp-watchdog` be loaded as
LaunchAgents on Neptune at all?** Memory `reference_two_box_audio_harness` describes a
*"self-healing launchd"* trio — **on Neptune, two thirds of it is not loaded.** That gap is
arguably the real bug behind this outage, and it is Phase 1 territory (§4.4 splits
`copytoudp-watchdog` into tap-arming vs route-restoration precisely here).

---

## 8. Phase 0 scorecard

| Item | Status |
|---|---|
| Controller map measured; device→controller recorded | ✅ **DONE** — 3 controllers, §1 |
| Do the two TB ports share a controller? (Q6) | ✅ **NO** — separate xHCIs, fallback alive |
| OWC dock available? (Q5) | ✅ **Not attached — and no longer needed** |
| All 5 SDRs enumerate at 480 Mb/s | 🔴 **NO** — 3 of 5 at 480; `56919602` at 12; HackRF absent |
| HackRF + RSPduo on separate controllers | ✅ **Achievable today**, free TB port reserved |
| Powered USB-3 hub installed for the RTLs | 🟡 VIA hub present; **power unconfirmed** |
| Root-cause the two "wedged" NESDRs | ✅ **DONE** — and the "off-bus" premise is **wrong**, §2 |
| SoapyHackRF installed | ✅ **Already present** in radioconda — no work needed |
| `SOAPY_SDR_PLUGIN_PATH` gotcha | ✅ **Confirmed + path correction found**, §3 |
| HackRF enumerates / serial / sweep / rate bench | 🔴 **BLOCKED — insertion attempted, LEDs lit, host sees nothing (§3). Cable swap pending; sequence queued for tomorrow** |
| `sdr_fleet_policy.json` rev 5.0 | ✅ **DONE — `62fddc5`** on `sb3-serial-reversal-fix` |
| §7.5 serial-reversal cleanup (7 artifacts → 8) | ✅ **DONE — `62fddc5`.** An 8th artifact found (`macos/killswitch/`, likely the *source*); 2 List B entries were Intel-box history and were annotated, not rewritten |
| Stale docs (`scan-philadelphia.md`, neptune README) | ⬜ not started — **not blocked** |
| SDRTrunk tuner-label strings | 🔴 blocked — needs GUI |
| `neptune-angel.mp3` + `neptune-trunk.mp3` both live | 🔴 **trunk 200; angel 404 — DELIBERATE** (Will: pause is intentional, marker stays) |

### Verdict: 🟡 **Phase 0 COMPLETE, with hardware caveats**

**The measurement Phase 0 existed to take is taken, and the answer is better than the plan
assumed** — Neptune already has the three independent controllers §5.3 wants, so the layout
costs nothing and no dock is needed. Both §7.6 questions are closed, the "NESDRs off-bus"
premise is disproven, rev 5.0 has landed, and the HackRF's software is already in place.

**The strict Phase 0 invariant is NOT met** — it requires all 5 SDRs at 480 Mb/s and one real
`hackrf_sweep`, and three items need Will physically at the box (§6): the HackRF cable,
`56919602`'s 12 Mb/s link, and the hub PSU. `neptune-angel.mp3` stays 404 by Will's explicit
decision, so it is excluded from the invariant rather than failing it.

**None of that blocked Phase 1, and Phase 1 has proceeded.** The `sb3-ctl` scaffolding is
delivered on **`sb3-phase1-scaffold` (`95caf4b`)** — `status` real, `kill` dry-run only,
`--execute` refused until review. It was verified dry-run against this box's live state and
modified nothing; its `status` output independently reproduces §4's findings (angel 404 ABSENT,
DS0 PHANTOM, watchdog unloaded) from a single command.

### 📌 What this pass contributed beyond the measurements

Phase 0 turned up a bug **class**, not just bugs. `soak-c.py`'s empty `system_profiler` →
silent log-grep fallback (§4-adjacent, and the likely author of the rev-4.1 reversal) and the
`.sdrangel-restore-paused` sentinel (§4) are the same shape: **a check that reports success
without having checked.** A third instance then appeared *inside the Phase 1 scaffold's own
invariant check* (a HEAD mount probe; icecast answers HEAD with 400). All three are now
codified as **§4.6 of the architecture plan** — *"any verification must be provable-to-execute,
or it must fail CLOSED"* — with rules bound to concrete sites. That is arguably this pass's
most durable output.

---

## 9. Commands used (all read-only)

```bash
sw_vers -productVersion; uname -m; uptime; sysctl -n kern.boottime
system_profiler SPUSBDataType            # ← returns EMPTY over SSH on Tahoe; use ioreg
system_profiler SPThunderboltDataType
ioreg -rc AppleUSBXHCI -d1
ioreg -p IOUSB -w0 -l
launchctl list | grep com.scannerproject
launchctl print gui/501/com.scannerproject.copytoudp-watchdog
ps -o pid,etime,command -p <pid>; pgrep -fl ffmpeg; lsof -nP -iUDP:9998
curl -s http://127.0.0.1:8000/status-json.xsl
curl -s "http://127.0.0.1:8091/sdrangel"                    # devicesets
curl -s "http://127.0.0.1:8091/sdrangel/devices?direction=0"
hackrf_info
SOAPY_SDR_PLUGIN_PATH=$HOME/radioconda/lib/SoapySDR/modules0.8 SoapySDRUtil --find="driver=hackrf"
```

**Not run, deliberately:** `rtl_test -t` (§2 — wedge risk with no one onsite to replug).
**Nothing was restarted, written, or reconfigured on Neptune.**
