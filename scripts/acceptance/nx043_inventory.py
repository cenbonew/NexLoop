#!/usr/bin/env python3
"""NX-043 X7: read-only inventory of the result files nx043_stage.py wrote into a results_dir (no connection, no write).

    python3 nx043_inventory.py <results_dir> [<results_dir> ...]

One line per file, tab separated: segment, file name, size, schema, check, executed_at, passed, blocked count, run nonce (first 6
characters, only to tell runs apart), release (first 8 characters). Steps, observed values and the private state file's ids are never
printed; for nx043-state.json only its presence, size and the run nonce prefix. Files that are not results of this tool are listed as
`other` with their size only. Documents (including state) above MAX_BYTES are not parsed. The passed column shows True/False
only for JSON booleans, None for JSON null, and - for missing/invalid values. Whole JSON documents and the existing last-line
JSON format are supported. Exit 0 always (a listing, not a judgement).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RESULT_SCHEMA = 'nexloop-s6-stage-result/1'
# check -> the segment of docs/implementation/NX-043-X7-acceptance.md (dry runs never write a file)
SEGMENTS = {'nx043-readiness': 'R1', 'nx043-prepare': 'S1', 'nx043-run': 'S2', 'nx043-probe-before': 'S4', 'nx043-shadow-keys': 'S5',
            'nx043-decide': 'S6', 'nx043-report': 'S7', 'nx043-probe-after': 'S8', 'nx043-shadow': 'S9', 'nx043-online': 'O1',
            'nx043-online-probe': 'O3', 'nx043-online-stop': 'O4', 'nx043-online-drain': 'O5', 'nx043-budget-start': 'B1',
            'nx043-budget-before': 'B2', 'nx043-budget-runs': 'B3', 'nx043-budget-probe': 'B4', 'nx043-gaps': 'G'}
MAX_BYTES = 8 * 1024 * 1024


def _short(value, n):
    return value[:n] if isinstance(value, str) else '-'


def _document(path: Path, size: int, *, last_line=False):
    if size > MAX_BYTES:
        return None
    try:
        # Bound the actual read too, in case the file grew after stat().
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return None
        text = data.decode('utf-8')
        try:
            return json.loads(text)
        except ValueError:
            if last_line:
                return json.loads(text.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def describe(path: Path) -> list[str]:
    size = path.stat().st_size
    if path.name == 'nx043-state.json':
        doc = _document(path, size)
        run = doc.get('run') if isinstance(doc, dict) else None
        return ['state', path.name, str(size), '-', '-', '-', '-', '-', _short(run, 6), '-']
    if path.name == 'nx043-shadow-keys.json':
        return ['keys', path.name, str(size), '-', '-', '-', '-', '-', '-', '-']
    doc = _document(path, size, last_line=True) if path.suffix == '.json' else None
    if not isinstance(doc, dict) or doc.get('schema') != RESULT_SCHEMA or not str(doc.get('check', '')).startswith('nx043-'):
        return ['other', path.name, str(size), '-', '-', '-', '-', '-', '-', '-']
    check = str(doc['check'])
    blocked = doc.get('blocked')
    passed = doc.get('passed')
    passed_text = str(passed) if 'passed' in doc and (type(passed) is bool or passed is None) else '-'
    return [SEGMENTS.get(check, '?'), path.name, str(size), RESULT_SCHEMA, check, _short(doc.get('executed_at'), 20),
            passed_text, str(len(blocked)) if isinstance(blocked, list) else '-', _short(doc.get('run_nonce'), 6),
            _short(doc.get('release'), 8)]


def main(argv=None, out=sys.stdout) -> int:
    dirs = (argv if argv is not None else sys.argv[1:]) or []
    if not dirs:
        print('usage: nx043_inventory.py <results_dir> [...]', file=sys.stderr)
        return 2
    print('\t'.join(['segment', 'file', 'bytes', 'schema', 'check', 'executed_at', 'passed', 'blocked', 'nonce6', 'release8']), file=out)
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            print('\t'.join(['missing_dir', root.name, '-', '-', '-', '-', '-', '-', '-', '-']), file=out)
            continue
        rows = [describe(p) for p in sorted(root.iterdir()) if p.is_file() and not p.is_symlink()]
        for row in sorted(rows, key=lambda r: (r[0], r[5], r[1])):
            print('\t'.join(row), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
