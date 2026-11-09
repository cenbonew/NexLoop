"""NX-043 X7 inventory (scripts/acceptance/nx043_inventory.py): lists result files by segment without their content. No database."""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import nx043_inventory as I  # noqa: E402
import nx043_stage as X  # noqa: E402

MARKER = 'observed-value-never-listed'


def _result(directory, check, executed_at, **extra):
    doc = {'schema': I.RESULT_SCHEMA, 'check': check, 'executed_at': executed_at, 'dry_run': False, 'passed': True, 'blocked': [],
           'run_nonce': 'q1w2e3r4t5y6', 'release': 'r73releasecommit', 'steps': [{'name': 'x', 'observed': MARKER}], **extra}
    (directory / f"{check}-{executed_at[:17].replace(':', '')}.json").write_text(json.dumps(doc) + '\n')


def test_every_phase_that_writes_a_result_has_a_segment():
    assert {('nx043-shadow' if p == 'merge' else 'nx043-' + p) for p in X.PHASES} == set(I.SEGMENTS)


def test_the_listing_names_segments_and_never_prints_content(tmp_path):
    for check, at in [('nx043-probe-before', '2026-10-26T09:56:00'), ('nx043-shadow-keys', '2026-10-26T10:00:00'),
                      ('nx043-probe-after', '2026-10-26T11:56:00')]:
        _result(tmp_path, check, at)
    (tmp_path / 'nx043-state.json').write_text(json.dumps({'run': 'q1w2e3r4t5y6', 'world': MARKER}))
    (tmp_path / 'notes.txt').write_text(MARKER)
    (tmp_path / 'broken.json').write_text('{')
    out = io.StringIO()
    assert I.main([str(tmp_path), str(tmp_path / 'absent')], out) == 0
    text = out.getvalue()
    assert MARKER not in text
    rows = [line.split('\t') for line in text.splitlines()[1:]]
    assert [r[0] for r in rows if r[0] in ('S4', 'S5', 'S8')] == ['S4', 'S5', 'S8']
    assert all(r[8] == 'q1w2e3' for r in rows if r[0] in ('S4', 'S5', 'S8', 'state'))
    assert {r[1] for r in rows if r[0] == 'other'} == {'notes.txt', 'broken.json'}
    assert rows[-1][0] == 'missing_dir'
