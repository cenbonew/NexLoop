"""Alert rules v27 (I35-g / I36-e, dispatcher 2026-10-31): v26 plus workbench_read_types_missing and commitment_orphan_seen.
The snapshots below follow the blocks of migrations workbench_read_types (control.nexloop_workbench_read_types_state) and
erasure_cancels_undated_commitments (commitment_orphans {total, seen_last_24h}); their PG tests assert the blocks themselves.
fired() reads a value the way the SQL evaluator (0131) does: only a JSON number is compared, anything else is skipped."""
import json
from pathlib import Path

import pytest

from nexloop_eios.observability import load_rules

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'
NEW = ('workbench_read_types_missing', 'commitment_orphan_seen')


def v27():
    return load_rules(RULES / 'alert-rules.v27.json')


def fired(snapshot, rules=None):
    out = set()
    for r in rules or [x for x in v27()['rules'] if x['rule_id'] in NEW]:
        value = snapshot
        for part in r['metric'].split('.'):
            value = value.get(part) if type(value) is dict else None
        if type(value) not in (int, float):
            continue
        if value > r['threshold'] if r['condition'] == 'gt' else value >= r['threshold']:
            out.add(r['rule_id'])
    return out


def test_v27_adds_exactly_the_two_rules_on_v26():
    v26, new = json.loads((RULES / 'alert-rules.v26.json').read_text()), v27()
    assert new['version'] == 27 and len(v26['rules']) == 91 and len(new['rules']) == 93 and new['rules'][:91] == v26['rules']
    assert new['decision'].startswith(v26['decision'] + ' v27 ')
    read_types, orphans = new['rules'][91:]
    assert (read_types['rule_id'], read_types['metric'], read_types['condition'], read_types['threshold'], read_types['severity']) == (
        'workbench_read_types_missing', 'workbench_read_types.missing', 'gt', 0, 'warning')
    assert (orphans['rule_id'], orphans['metric'], orphans['condition'], orphans['threshold'], orphans['severity']) == (
        'commitment_orphan_seen', 'commitment_orphans.seen_last_24h', 'gt', 0, 'warning')
    assert 'selector' not in read_types and 'selector' not in orphans


@pytest.mark.parametrize('block,expected', [
    ({'configured': True, 'roles_version': 53, 'present': False, 'missing': 1}, {'workbench_read_types_missing'}),
    ({'configured': True, 'roles_version': 52, 'present': True, 'missing': 0}, set()),
    ({'configured': False, 'missing': 0}, set()),  # no workbench configuration: a visible 0, not a false alarm
])
def test_workbench_read_types_missing(block, expected):
    assert fired({'workbench_read_types': block, 'commitment_orphans': {'total': 0, 'seen_last_24h': 0}}) == expected


def test_a_boolean_present_alone_would_never_fire():
    # Why the rule reads missing: the evaluator compares numbers only, so a rule on present could not see false.
    on_present = [{**v27()['rules'][91], 'metric': 'workbench_read_types.present'}]
    for present in (False, True):  # a boolean is skipped either way, like jsonb_typeof <> 'number' in SQL
        assert fired({'workbench_read_types': {'configured': True, 'present': present}}, on_present) == set()


@pytest.mark.parametrize('block,expected', [
    ({'total': 3, 'seen_last_24h': 1}, {'commitment_orphan_seen'}),
    ({'total': 3, 'seen_last_24h': 0}, set()),  # orphans seen only before the last 24 hours
    ({'total': 0, 'seen_last_24h': 0}, set()),  # no data: an explicit 0
])
def test_commitment_orphan_seen(block, expected):
    assert fired({'workbench_read_types': {'configured': False, 'missing': 0}, 'commitment_orphans': block}) == expected
