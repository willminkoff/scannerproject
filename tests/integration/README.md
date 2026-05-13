# SB3 reliability integration tests

Phase 1 of the reliability harness. Read-only SSH sampling of Micro plus
interactive prompts that ask the human operator to perform UI actions in a
browser. The tests assert process invariants (op25 stays up, classifier PID
unchanged, no kill-cascade) plus shape invariants on the picker output
(systems.json non-empty, every site has control channels).

Tests **do not write to Micro**. The only side effect is reading state via
`ssh root@micro …`.

## Prerequisites

- pytest installed locally (`pip install pytest` if not already)
- SSH access to Micro from the machine running pytest. Default host alias is
  `root@micro` (Tailscale SSH); override with `MICRO_SSH_HOST=user@host`.
- Micro is up, airband-ui and op25 are running.

## Run

```bash
# All Phase 1 scenarios (only one today: scenario 1)
pytest -s -v tests/integration/

# Single scenario
pytest -s -v tests/integration/test_zip_to_favorites_to_scan.py

# Filter by marker
pytest -s -v -m integration tests/
```

The `-s` flag is **required** so stdin isn't captured — tests use prompts
that block on `ENTER`. Without `-s`, tests will fail with a clear error
rather than hang.

## What each fixture does

| Fixture | Scope | Purpose |
|---|---|---|
| `micro_ssh_host` | session | Returns SSH target string (env-overridable) |
| `micro_ssh` | session | Returns `run(cmd)` callable that SSHes to Micro and returns CompletedProcess |
| `state_sampler` | function | Returns `sample()` callable. Each call SSHes once, returns a dict with op25 services, systems.json, multi_rx.json, classifier PID, kill-cascade count, op25 log tail |
| `prompt_user` | function | Returns `prompt(msg)` that prints msg and blocks on ENTER. Requires `pytest -s` |
| `wait_for` | function | Returns `wait(predicate, timeout, interval) -> bool`. Polls predicate, returns True the moment it's truthy, False on timeout |
| `result_writer` | function | Returns `write(record)` that appends one JSON line per call to `tests/integration/results/{test_name}__{ts}.jsonl` |

If SSH to Micro is unreachable when the suite starts, the whole integration
suite is skipped via the `_ssh_reachable` autouse session fixture — no
cascading errors.

## Markers

Two markers, both registered via `pytest_configure` so no warnings:

- `@pytest.mark.integration` — flags the test as integration (touches Micro)
- `@pytest.mark.requires_ssh` — flags it as needing SSH; used by the
  reachability skip logic

## Result files

JSONL under `tests/integration/results/`. One file per test invocation,
named `{test_name}__{ts_utc}.jsonl`. Each line is a JSON record with
`_test`, `_ts_utc`, `phase`, and a snapshot of state.

The directory has a `.gitignore` that excludes `*.jsonl`; results are local
artifacts, not committed.

To inspect a run:

```bash
ls -la tests/integration/results/
jq -r '.phase' tests/integration/results/test_zip_to_favorites_to_scan__*.jsonl
jq '.state.services' tests/integration/results/test_zip_to_favorites_to_scan__*.jsonl
```

## Scenario 1 — `test_zip_to_favorites_to_scan`

1. Snapshots pre-state, asserts airband-ui + op25 are active and no
   kill-cascade is in flight
2. Captures classifier PID for warm-cache invariant
3. Prompts you to enter ZIP + scan mode + service tags in the SB3 UI
4. Waits up to 30s for `systems.json` mtime to advance (proves picker fired)
5. Snapshots post-state, asserts:
   - systems.json mtime advanced
   - systems.json is non-empty and every site has control channels (generic
     — works from any ZIP)
   - op25 still active
   - no kill-cascade
   - classifier PID unchanged (skipped if classifier was masked/inactive)
6. Optional: waits up to 120s for op25 to actually decode voice traffic.
   Records the result. **Doesn't fail if no voice traffic** — coverage may
   legitimately be quiet during the test window.

## Phase 2 / Phase 3 (not yet implemented)

- Phase 2: scenarios 2-4 (full_db, service swap, Disco toggle)
- Phase 3: sampler mode, failure injection
