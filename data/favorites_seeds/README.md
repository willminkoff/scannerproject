# Favorites Seeds

Each JSON file here is a single HP favorite entry (matching the shape of
items in `hp_state.json["favorites"]`).  These are committed to git so
favorites hand-built outside the UI (e.g., the ACY Airshow profile
rebuilt from the legacy gen-2 rtl-airband .conf) survive a fresh deploy
or a state reset.

`hp_state.json` itself is gitignored because it carries runtime user
state — these seeds are the durable, sharable extract of what we want
reapplied to it.

## Apply

```
python3 scripts/apply_favorite_seed.py data/favorites_seeds/<id>.json
```

Idempotent: skips when the same id is already present.

### Options

- `--dry-run` — preview the change without writing.  Shows NEW for
  first-time append, +N/-N channel summary for replaces, IDENTICAL
  when the seed exactly matches what is already loaded.
- `--force` — silently overwrite an existing id (for scripted use).
- `--replace` — interactive y/N prompt with the diff before
  overwriting (for manual use).
- `--via-api` — POST through `/api/scan/state` on a running
  airband-ui (default `http://localhost:5050`) instead of editing
  `hp_state.json` directly.  Routes through the proper persistence
  + sync path so the change reaches rtl-airband / OP25 immediately
  without restarting airband-ui.
- `--url URL` — override the API endpoint for `--via-api`.

Direct-write mode makes a backup at
`data/hp_state.json.bak.before-seed-<id>` before writing.
