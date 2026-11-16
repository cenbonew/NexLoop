"""NX-043 X7 inventory (scripts/acceptance/nx043_inventory.py): lists result files by segment without their content. No database."""
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import nx043_inventory as I  # noqa: E402
import nx043_stage as X  # noqa: E402

MARKER = 'observed-value-never-listed'


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    import socket
    import psycopg

    def forbidden(*args, **kwargs):
        pytest.fail('inventory regression must remain offline')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(psycopg, 'connect', forbidden)


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


def _inventory(directory):
    out = io.StringIO()
    assert I.main([str(directory)], out) == 0
    return {row[1]: row for row in (line.split('\t') for line in out.getvalue().splitlines()[1:])}, out.getvalue()


def test_real_result_writer_pass_fail_and_blocked_are_distinct(tmp_path):
    # Exercise the actual shared generator, with local synthetic steps only.
    from s6_common import Result
    for label, passed in [('passed', True), ('failed', False), ('blocked', None)]:
        directory = tmp_path / label
        result = Result('nx043-readiness', [], {'results_dir': str(directory), 'release': 'syntheticrelease', 'executor': 'synthetic'},
                        nonce='abcdefgh1234')
        if passed is None:
            result.block('synthetic', MARKER)
        else:
            result.step('synthetic', 'local only', MARKER, passed)
        assert result.write(io.StringIO()) == (0 if passed is True else 1)
        rows, text = _inventory(directory)
        row, = rows.values()
        assert row[0] == 'R1'
        assert row[6] == str(passed)
        assert row[7] == ('1' if passed is None else '0')
        assert MARKER not in text


def test_non_boolean_passed_cannot_impersonate_a_real_result(tmp_path):
    for index, value in enumerate(['True', 'False', 'None', 1, 0, [], {}, MARKER]):
        directory = tmp_path / str(index)
        directory.mkdir()
        _result(directory, 'nx043-readiness', '2026-11-17T00:00:00', passed=value)
        rows, text = _inventory(directory)
        row, = rows.values()
        assert row[6] == '-', (value, row)
        assert MARKER not in text


def test_formatted_json_and_existing_last_line_format_are_listed(tmp_path):
    _result(tmp_path, 'nx043-readiness', '2026-11-17T00:00:00')
    original, = tmp_path.iterdir()
    doc = json.loads(original.read_text())
    (tmp_path / 'formatted.json').write_text(json.dumps(doc, indent=2) + '\n\n')
    (tmp_path / 'last-line.json').write_text('synthetic log preamble\n' + json.dumps(doc) + '\n\n')
    rows, text = _inventory(tmp_path)
    assert len(rows) == 3
    assert all(row[0] == 'R1' and row[6] == 'True' for row in rows.values())
    assert MARKER not in text


def test_oversized_state_is_not_opened_or_parsed(tmp_path, monkeypatch):
    path = tmp_path / 'nx043-state.json'
    path.write_text(json.dumps({'run': 'abcdefgh1234', 'padding': 'x' * 100}))
    monkeypatch.setattr(I, 'MAX_BYTES', path.stat().st_size - 1)
    opened = []
    original_open = Path.open

    def tracked_open(self, *args, **kwargs):
        opened.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', tracked_open)
    rows, _ = _inventory(tmp_path)
    assert rows[path.name][0] == 'state'
    assert rows[path.name][8] == '-'
    assert path not in opened


def test_exact_byte_limit_and_over_limit_results(tmp_path, monkeypatch):
    _result(tmp_path, 'nx043-readiness', '2026-11-17T00:00:00')
    path, = tmp_path.iterdir()
    monkeypatch.setattr(I, 'MAX_BYTES', path.stat().st_size)
    rows, _ = _inventory(tmp_path)
    assert rows[path.name][0] == 'R1'
    monkeypatch.setattr(I, 'MAX_BYTES', path.stat().st_size - 1)
    rows, _ = _inventory(tmp_path)
    assert rows[path.name][0] == 'other'


def test_missing_passed_and_malformed_documents_do_not_report_success(tmp_path):
    _result(tmp_path, 'nx043-readiness', '2026-11-17T00:00:00')
    path, = tmp_path.iterdir()
    doc = json.loads(path.read_text())
    del doc['passed']
    path.write_text(json.dumps(doc))
    for name, data in [('array.json', b'[]'), ('null.json', b'null'), ('broken.json', b'{'),
                       ('invalid-utf8.json', b'\xff'), ('nx043-state.json', b'[]')]:
        (tmp_path / name).write_bytes(data)
    rows, _ = _inventory(tmp_path)
    assert rows[path.name][6] == '-'
    assert rows['nx043-state.json'][8] == '-'
    assert all(rows[name][0] == 'other' for name in ('array.json', 'null.json', 'broken.json', 'invalid-utf8.json'))
    assert all(len(row) == 10 for row in rows.values())


def test_read_limit_still_applies_if_file_grew_after_stat(tmp_path, monkeypatch):
    path = tmp_path / 'nx043-state.json'
    path.write_text(json.dumps({'run': 'abcdefgh1234'}))
    limit = path.stat().st_size
    monkeypatch.setattr(I, 'MAX_BYTES', limit)
    reads = []

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr(Path, 'open', lambda *a, **kw: GrowingFile(path.name.encode() + b'x' * limit))
    rows, _ = _inventory(tmp_path)
    assert rows[path.name][8] == '-'
    assert reads == [limit + 1]
