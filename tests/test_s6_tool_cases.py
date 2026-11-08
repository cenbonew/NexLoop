"""r8's D23 tool regression set (scripts/acceptance/s6_stage_schema.py tool_cases), on the Consumer-bound Actions as the catalog builds
them - the stage shape of 2026-11-09 (R72): most Actions have the open input schema {}, a few have patterns, uuid formats, consts,
lengths and bounds. A valid case is built from the schema's own constraints and checked against the Parent (refused when none can be
built); an invalid case is one the Parent refuses; an Action whose Parent accepts every value gets no invented invalid case - it is
named and the replay reports it incomplete (D23 decision pending, never a silent pass). A tightened candidate is still a regression.
No database."""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

from nexloop_eios import catalog_publish as P
from nexloop_eios import schema_gates as G

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import s6_stage_schema as S  # noqa: E402


def _stage_secrets():
    spec = importlib.util.spec_from_file_location('stage_secrets_for_tool_cases', ROOT / 'scripts/deploy/stage_secrets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def consumer_schemas():
    """{stable_name: {parent, candidate}} of every Consumer-bound Action; an optional property leaves the inputs as they are."""
    catalog = P.compile_catalog('tenant-synthetic', **_stage_secrets().CATALOG)
    out = {}
    for action in catalog['actions']:
        d = action['definition']
        if any(ref['stable_name'] == 'Consumer' for ref in d.get('object_types', [])):
            schema = d.get('input_schema') or {}
            out[d['stable_name']] = {'parent': schema, 'candidate': schema}
    return out


def test_the_stage_shape_has_open_and_constrained_input_schemas(consumer_schemas):
    open_names = [n for n, s in consumer_schemas.items() if not s['parent']]
    assert len(consumer_schemas) >= 90 and len(open_names) >= 80 and len(consumer_schemas) - len(open_names) >= 5


def test_every_constrained_action_gets_a_valid_and_an_invalid_case_that_the_parent_judges_as_expected(consumer_schemas):
    cases, open_actions = S.tool_cases(consumer_schemas)
    constrained = sorted(n for n, s in consumer_schemas.items() if s['parent'])
    assert sorted(open_actions) == sorted(n for n, s in consumer_schemas.items() if not s['parent'])
    for name in constrained:
        mine = {c['expect']: c['params'] for c in cases if c['action'] == name}
        assert set(mine) == {'valid', 'invalid'}, name
        assert S._accepts(consumer_schemas[name]['parent'], mine['valid']) and not S._accepts(consumer_schemas[name]['parent'], mine['invalid'])
    # e.g. a 64-hex pattern, a uuid and a const are satisfied by the valid sample (the old 'x' failed them)
    bind = next(c['params'] for c in cases if c['id'] == 'nexloop.context.bind-valid')
    assert len(bind['message_id']) == 64 and bind['run_id'] == S._SAMPLE_UUID


def test_the_replay_passes_every_constrained_action_and_names_every_open_one_incomplete(consumer_schemas):
    cases, open_actions = S.tool_cases(consumer_schemas)
    started = time.monotonic()
    tools = G.replay_tools(cases, consumer_schemas)
    assert time.monotonic() - started < 5  # ~100 Actions, two passes in the report: well under a second each
    assert tools['failures'] == []
    assert tools['incomplete'] == ['action:' + n for n in sorted(open_actions)]  # not a pass: tool_context_compatible fails on them
    gate = G._gate('tool_context_compatible', not tools['incomplete'], 'tools:regression_set', *tools['incomplete'])
    assert gate['result'] == 'fail' and len(gate['evidence_ids']) <= 64  # the evidence stays bounded


def test_a_tightened_candidate_is_still_a_regression(consumer_schemas):
    """Positive control: the candidate refuses what the Parent accepted (a summary shorter than the valid sample): failures."""
    schemas = {**consumer_schemas}
    deliver = schemas['nexloop.enterprise.deliver']
    tight = {**deliver['parent'], 'properties': {**deliver['parent']['properties'], 'summary': {'type': 'string', 'minLength': 1, 'maxLength': 2}}}
    schemas['nexloop.enterprise.deliver'] = {'parent': deliver['parent'], 'candidate': tight}
    cases, _ = S.tool_cases(schemas)
    assert G.replay_tools(cases, schemas)['failures'] == ['case:nexloop.enterprise.deliver-valid:nexloop.enterprise.deliver']


def test_no_valid_sample_is_refused_never_left_to_the_gate():
    with pytest.raises(S.Refused, match='no valid sample'):
        S.tool_cases({'synthetic.impossible': {'parent': {'type': 'string', 'pattern': '^[0-9]{3}$'}, 'candidate': {}}})
    cases, open_actions = S.tool_cases({'synthetic.open': {'parent': {}, 'candidate': {}}})
    assert open_actions == ['synthetic.open'] and [c['expect'] for c in cases] == ['valid']  # no invented invalid case
