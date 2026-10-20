"""NX-041 S1 (D1, D9): the Python world check against the shared cases (tests/support/nx041-world-cases.json), the same file the Agent
Host test reads (apps/agent-host/test/nx041-world-check.test.ts), so the two implementations cannot drift apart unnoticed."""
import json
from pathlib import Path

import pytest

from nexloop_eios.worlds import valid_world, world_kind

CASES = json.loads((Path(__file__).resolve().parent / 'support/nx041-world-cases.json').read_text())


def test_valid_worlds_have_their_kind():
    assert [(c['world'], world_kind(c['world'])) for c in CASES['valid']] == [(c['world'], c['kind']) for c in CASES['valid']]
    assert all(valid_world(c['world']) for c in CASES['valid'])


@pytest.mark.parametrize('value', CASES['invalid'], ids=lambda v: json.dumps(v))
def test_invalid_values_are_not_worlds(value):
    assert valid_world(value) is False and world_kind(value) is None


def test_the_run_command_rule_is_world_kind_equals_mode():
    for p in CASES['mode_pairs']:
        assert (world_kind(p['world']) == p['mode']) is p['ok'], p


def test_the_cases_match_the_sql_check_list():
    """The SQL test's literals (tests/test_nx041_world_check_pg.py) are all in the shared file."""
    import test_nx041_world_check_pg as sql
    assert set(sql.VALID) == {c['world'] for c in CASES['valid']} and set(sql.INVALID) <= {v for v in CASES['invalid'] if isinstance(v, str)}
