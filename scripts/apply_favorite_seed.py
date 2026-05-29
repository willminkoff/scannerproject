#!/usr/bin/env python3
"""Apply a single HP-favorite seed file into hp_state.json.

Usage: python3 scripts/apply_favorite_seed.py <seed.json>

Idempotent: if a favorite with the same uid=1005(peaceful-vibrant-clarke) gid=1005(peaceful-vibrant-clarke) groups=1005(peaceful-vibrant-clarke) already exists in
hp_state.json, the seed is skipped (does NOT overwrite live edits).
Pass --force to overwrite anyway.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

HP_STATE = Path(__file__).resolve().parent.parent / 'data' / 'hp_state.json'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('seed', type=Path)
    ap.add_argument('--force', action='store_true', help='overwrite even if id already present')
    ap.add_argument('--dry-run', action='store_true', help='print what would change')
    args = ap.parse_args()

    if not args.seed.is_file():
        sys.exit(f'seed file not found: {args.seed}')
    if not HP_STATE.is_file():
        sys.exit(f'hp_state.json not found at {HP_STATE}')

    seed = json.loads(args.seed.read_text())
    if not isinstance(seed, dict) or not seed.get('id'):
        sys.exit('seed file does not look like a single favorite entry (missing id)')
    seed_id = seed['id']

    state = json.loads(HP_STATE.read_text())
    favs = state.setdefault('favorites', [])
    idx = next((i for i, f in enumerate(favs) if isinstance(f, dict) and f.get('id') == seed_id), -1)

    if idx >= 0 and not args.force:
        print(f'skipped: {seed_id} already present (use --force to overwrite)')
        return
    if args.dry_run:
        print(f'would {("replace" if idx >= 0 else "append")}: {seed_id}')
        return

    shutil.copy(HP_STATE, str(HP_STATE) + '.bak.before-seed-' + seed_id)
    if idx >= 0:
        favs[idx] = seed
        action = 'replaced'
    else:
        favs.append(seed)
        action = 'appended'
    HP_STATE.write_text(json.dumps(state, indent=2))
    print(f'{action}: {seed_id} ({len(seed.get("custom_favorites") or [])} channels)')

if __name__ == '__main__':
    main()
