# archive/

Retired code kept for reference and one-step restore. Files here are **not**
wired into any running service — they are the previous implementation of
something that has since been replaced. Each entry below records what it was,
when/why it was retired, and how to bring it back.

## `waterfall.py`

The custom 2× RTL-SDR stitched-spectrum waterfall that drove the `/sb5` Live IQ
pane via `scanner-waterfall.service`.

- **Retired:** 2026-06-01, pilot commit `406ed67`.
- **Why:** replaced by **OpenWebRX+** (Docker container `owrxp` on `:8073`), which
  gives drag-to-tune across the whole RTL-SDR range, multi-mode demod, and a
  maintained codebase — versus the fixed-window stitched view this script
  produced. The pilot also freed a dongle (`70613472`). See
  `docs/openwebrx-pilot.md` and `docs/OWRX_OPS.md`.
- **What changed at retirement:** `scanner-waterfall.service` was masked (its unit
  moved to `…service.owrx-pilot-bak`), and dongle `83241970` was reassigned from
  this script to OWRX.
- **Restore:** see the full, verified revert sequence in **`docs/OWRX_OPS.md` →
  "Revert to the old waterfall + VFO system"**. In short: `docker stop owrxp`,
  copy this file back to `scripts/waterfall.py`, unmask + restore the unit, then
  `systemctl enable --now scanner-waterfall.service`. (The shorthand
  `unmask && start` alone will not work — the unit file and this script both have
  to be put back first.)

> `scripts/vfo.py` / `scanner-vfo.service` / `/VFO.mp3` were **not** retired by
> the OWRX pilot — they kept running on dongle `80000003`. Retiring the VFO is
> Phase 2 (bridge OWRX audio → icecast), tracked in `docs/openwebrx-pilot.md`.
