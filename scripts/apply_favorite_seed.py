#!/usr/bin/env python3
"""Apply a single HP-favorite seed file into hp_state.json.

Usage: apply_favorite_seed.py <seed.json> [options]

The seed is a single favorite entry — same shape as items in
hp_state.json['favorites'] — with at minimum `id`, `label`, and
`custom_favorites`.

Modes:
  default        Direct file write to data/hp_state.json (fast,
                 idempotent, makes a backup before writing).  Bypasses
                 airband-ui's favorites-runtime sync; if you want the
                 change to propagate to the live rtl-airband / OP25
                 profiles, restart airband-ui or use --via-api.
  --via-api      POST through /api/scan/state on a running airband-ui
                 (default http://localhost:5050/api/scan/state).  Goes
                 through the proper persistence + downstream sync path
                 so changes appear in the runtime without a restart.

Collision handling (when a favorite with the same `id` already exists):
  default        Skip (does NOT overwrite).  Print "skipped:".
  --force        Silently overwrite.
  --replace      Interactive y/n prompt showing a brief diff first.

Other:
  --dry-run      Print what would change without writing or POSTing.
                 Works with all modes.
"""
import argparse
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HP_STATE = Path(__file__).resolve().parent.parent / 'data' / 'hp_state.json'
DEFAULT_API_URL = 'http://localhost:5050/api/scan/state'


def _channel_summary(fav):
    chans = fav.get('custom_favorites') or []
    return f'{len(chans)} channel(s)'


def _diff_favorites(existing, incoming):
    """Return a short string describing what changes between two favorite dicts."""
    if not existing:
        return f'NEW favorite ({_channel_summary(incoming)})'
    a = {(c.get('frequency'), c.get('alpha_tag')): c for c in (existing.get('custom_favorites') or [])}
    b = {(c.get('frequency'), c.get('alpha_tag')): c for c in (incoming.get('custom_favorites') or [])}
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    label_changed = (existing.get('label') != incoming.get('label'))
    parts = []
    if label_changed:
        parts.append(f'label {existing.get("label")!r} -> {incoming.get("label")!r}')
    if added:
        parts.append(f'+{len(added)} channel(s): ' + ', '.join(f'{f:.4f} {a or "?"}' for (f, a) in added[:5])
                     + (' ...' if len(added) > 5 else ''))
    if removed:
        parts.append(f'-{len(removed)} channel(s): ' + ', '.join(f'{f:.4f} {a or "?"}' for (f, a) in removed[:5])
                     + (' ...' if len(removed) > 5 else ''))
    if not parts:
        return 'IDENTICAL (no change)'
    return '; '.join(parts)


def _prompt_yes(question):
    try:
        ans = input(question + ' [y/N] ').strip().lower()
    except EOFError:
        return False
    return ans in ('y', 'yes')


def _direct_write(seed, *, force, replace, dry_run):
    if not HP_STATE.is_file():
        sys.exit(f'hp_state.json not found at {HP_STATE}')
    state = json.loads(HP_STATE.read_text())
    favs = state.setdefault('favorites', [])
    seed_id = seed['id']
    idx = next((i for i, f in enumerate(favs) if isinstance(f, dict) and f.get('id') == seed_id), -1)

    if idx >= 0:
        existing = favs[idx]
        diff_text = _diff_favorites(existing, seed)
        if dry_run:
            print(f'would replace: {seed_id} | {diff_text}')
            return 0
        if not force and not replace:
            print(f'skipped: {seed_id} already present (use --force or --replace)')
            return 0
        if replace:
            print(f'collision: {seed_id} | {diff_text}')
            if not _prompt_yes('replace?'):
                print('aborted')
                return 1
    else:
        diff_text = _diff_favorites(None, seed)
        if dry_run:
            print(f'would append: {seed_id} | {diff_text}')
            return 0

    shutil.copy(HP_STATE, str(HP_STATE) + '.bak.before-seed-' + seed_id)
    if idx >= 0:
        favs[idx] = seed
        action = 'replaced'
    else:
        favs.append(seed)
        action = 'appended'
    HP_STATE.write_text(json.dumps(state, indent=2))
    print(f'{action}: {seed_id} | {diff_text}')
    return 0


def _via_api(seed, *, url, force, replace, dry_run):
    # Pull current state so we can merge intelligently and produce a real diff.
    base = url.rstrip('/').rsplit('/', 1)[0]  # http://host:port/api/scan
    get_url = base + '/state'
    # Normalize: /api/scan/state -> /api/scan/state (POST), but for GET we try
    # the same path; servers handle GET on it.
    try:
        with urllib.request.urlopen(url) as resp:
            current = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        sys.exit(f'failed to read current state from {url}: {e}')

    state_obj = current.get('state') if isinstance(current.get('state'), dict) else current
    favs = state_obj.get('favorites') or []
    seed_id = seed['id']
    idx = next((i for i, f in enumerate(favs) if isinstance(f, dict) and f.get('id') == seed_id), -1)

    if idx >= 0:
        existing = favs[idx]
        diff_text = _diff_favorites(existing, seed)
        if dry_run:
            print(f'would replace via {url}: {seed_id} | {diff_text}')
            return 0
        if not force and not replace:
            print(f'skipped: {seed_id} already present (use --force or --replace)')
            return 0
        if replace:
            print(f'collision: {seed_id} | {diff_text}')
            if not _prompt_yes('replace via api?'):
                print('aborted')
                return 1
        favs[idx] = seed
    else:
        diff_text = _diff_favorites(None, seed)
        if dry_run:
            print(f'would append via {url}: {seed_id} | {diff_text}')
            return 0
        favs.append(seed)

    payload = json.dumps({'favorites': favs}).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8')
        except Exception:
            body = '<no body>'
        sys.exit(f'POST {url} failed: {e.code} {body[:200]}')
    except Exception as e:
        sys.exit(f'POST {url} failed: {e}')

    print(f'{"replaced" if idx >= 0 else "appended"}: {seed_id} via {url} | {diff_text}')
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('seed', type=Path, help='path to a single-favorite JSON seed')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--force', action='store_true', help='silently overwrite existing id')
    g.add_argument('--replace', action='store_true', help='interactive y/n if id exists')
    ap.add_argument('--dry-run', action='store_true', help='show what would change, write nothing')
    ap.add_argument('--via-api', action='store_true', help='POST through /api/scan/state instead of direct file write')
    ap.add_argument('--url', default=DEFAULT_API_URL, help=f'API endpoint (default {DEFAULT_API_URL})')
    args = ap.parse_args()

    if not args.seed.is_file():
        sys.exit(f'seed file not found: {args.seed}')
    try:
        seed = json.loads(args.seed.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f'seed is not valid JSON: {e}')
    if not isinstance(seed, dict) or not seed.get('id'):
        sys.exit('seed does not look like a single favorite entry (missing id)')

    if args.via_api:
        return _via_api(seed, url=args.url, force=args.force, replace=args.replace, dry_run=args.dry_run)
    return _direct_write(seed, force=args.force, replace=args.replace, dry_run=args.dry_run)


if __name__ == '__main__':
    sys.exit(main() or 0)
