"""K1 (design dsh-superagent-idea-intake §3.2): the model-visible surface of each Run kind, as one whole.

The request comes from the Host's own assembly, exactly as NX-023 records it (the request snapshot's request_text):
tests/support/model_surface_dump.mjs runs RuntimeHost's tool selection and PiRuntimeAdapter with the real-model instructions
against a deterministic provider. Nothing is assembled here. Checked per Run kind:

- the wire tool names are the ones apps/agent-host/src/provider-tool-names.ts allows (and DeepSeek's mapping stays unique);
- every nexloop.* name the model reads (instructions and tool descriptions) is a published Action, or a Host-only tool listed
  below with its reason;
- the framing (everything in the request except the context pack itself) stays within the Run kind's budget;
- every object type name the model reads is defined in the world-model or workbench glossary.

Budgets are the measured baseline of main 366295aa plus about 15%: a new tool or a longer instruction that crosses them fails
here and has to raise the budget on purpose. Tokens use the context engine's own estimator (ceil(UTF-8 bytes / 2)).
"""
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DUMP = ROOT / 'tests' / 'support' / 'model_surface_dump.mjs'
# The latest strategies manifest: the one a stage publishes next (S82: v2).
CONTEXT_STRATEGIES = max((ROOT / 'deploy' / 'configuration').glob('context-strategies.v*.json'), key=lambda p: int(p.stem.rsplit('.v', 1)[1]))

TRIGGER = {'kind': 'service_trigger'}
MESSAGE_PACK = {'schema_version': 'nexloop.context-pack.v6', 'user_statement': {'message_id': 'm' * 64, 'body': '你好，我想问一下订单。'},
                'current_event': {'kind': 'consumer_message'},
                # An enterprise offering in the supply: the message Run is offered nexloop.enterprise.deliver too (its widest surface).
                'effect_supplies': [{'action': 'nexloop.enterprise.deliver:1', 'offering_id': 'o' * 64, 'offering_revision': 1,
                                     'properties': {'service_code': 'enterprise.synthetic', 'title': '合成企业交付', 'price_amount': '100.00'}}]}
PLAN_PACK = {'schema_version': 'nexloop.context-pack.v6', 'user_statement': None, 'current_event': TRIGGER}
# S85 (S83 batch 2): the incentive tool only with an IncentiveOffering of the customer in open work. The budgets below are for the
# widest surface, so these packs carry one. (The reply's request_scope is always offered: the supply's allowed terms are empty by
# contract, maxItems 0, and request_scope exists to ask for terms outside them, which are refused and recorded - CI 101.)
WIDEST = {'open_work': [{'subsection': 'incentive', 'ref': 'eios:object:IncentiveOffering/' + '1' * 64}]}
MESSAGE_PACK = {**MESSAGE_PACK, **WIDEST}
PLAN_PACK = {**PLAN_PACK, **WIDEST}
SIM_PACK = {'schema_version': 'nexloop.context-pack.sim.v1', 'data_mode': 'simulation', 'current_event': None, 'consumer_state': [], 'evidence': []}
TOOLS = {'effect_tools': True, 'plan_outcome_tool': True, 'incentive_tool': True}
RUNS = {
    'message_reply': {'host': TOOLS, 'plan': False, 'pack': MESSAGE_PACK},
    'plan_reevaluation': {'host': TOOLS, 'plan': True, 'pack': PLAN_PACK},
    # ADR-023 §2.7: the fallback reply Run's Context carries the customer's statement; its request id is not a plan's.
    'fallback_reply': {'host': TOOLS, 'plan': False, 'pack': {**MESSAGE_PACK, 'effect_supplies': []}},
    'outreach': {'host': {**TOOLS, 'outreach_tool': True}, 'plan': True, 'pack': PLAN_PACK},
    # NX-041 (K1b): a simulation Host's reply Run: SIM_RUN_INSTRUCTIONS and the stand-in tools (same names, simulated answers),
    # through the same RuntimeHost/PiRuntimeAdapter path. No plan tools (a simulation Host offers them to non-message Runs only
    # when configured), no enterprise delivery (it has no stand-in).
    'simulation_reply': {'host': {'effect_tools': True, 'incentive_tool': True, 'world_group': 'simulation'}, 'plan': False, 'pack': SIM_PACK},
}
# Framing budget per Run kind, in estimated tokens (baseline at 366295aa in the comment).
FRAMING_BUDGET = {
    # S84 (S83 D2, 86dca8e8): enterprise.find refers to deliver's result guide; plan.outcome, incentive.issue and service.request
    # offer a compact equivalent schema. Measured 3254 / 2670 / 1970 / 3223 (were 3409 / 2822 / 1997 / 3375); each budget is
    # the measurement x 1.15 rounded up to 50.
    'message_reply': 3750,       # 3254: instructions + incentive.issue, enterprise.deliver/find, service.request/find
    'plan_reevaluation': 3100,   # 2670: + plan.outcome, no enterprise
    'fallback_reply': 2300,      # 1970: no enterprise supply
    'outreach': 3750,            # 3223: + outreach.send
    'simulation_reply': 1700,    # 1445 (K1b, 8ba485e6): SIM_RUN_INSTRUCTIONS + stand-in incentive.issue, service.request/find
}
# Host-only tools: offered to the model, but not EIOS Actions of the published catalogs.
HOST_ONLY = {
    'nexloop.service.find': 'reads the receipt of this Run\'s own nexloop.service.request intent; no write',
    'nexloop.enterprise.find': 'reads the receipt of this Run\'s own nexloop.enterprise.deliver intent; no write',
    'nexloop.plan.outcome': 'records the Run outcome through the runtime guard (run-outcome contract), not an Action',
}
ACTION_MANIFESTS = ('system-actions.v1.json', 'business-actions.v1.json')


def _published_actions():
    names = set()
    for name in ACTION_MANIFESTS:
        names |= {a['stable_name'] for a in json.loads((ROOT / 'deploy' / 'configuration' / name).read_text())['actions']}
    return names


def _glossary_terms():
    world = json.loads((ROOT / 'deploy' / 'configuration' / 'world-model-glossary.v1.json').read_text())
    terms = set(world['object_types']) | set(world['sections'])
    workbench = (ROOT / 'docs' / 'design' / 'workbench-glossary.md').read_text()
    terms |= set(re.findall(r'^\| \*\*([^*|]+)\*\* \|', workbench, re.M)) | set(re.findall(r'^\| ([A-Za-z][A-Za-z ]+) \|', workbench, re.M))
    return terms


@pytest.fixture(scope='module')
def requests(tmp_path_factory):
    node = shutil.which('node')
    if node is None or not (ROOT / 'apps' / 'agent-host' / 'dist' / 'runtime-host.js').exists():
        pytest.fail('node and a built apps/agent-host/dist are required (npx tsc -p apps/agent-host/tsconfig.json)')
    path = tmp_path_factory.mktemp('model-surface') / 'runs.json'
    path.write_text(json.dumps(RUNS, ensure_ascii=False))
    result = subprocess.run([node, '--no-warnings', str(DUMP), str(path)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def _system(request):
    messages = request['context']['messages']
    assert [m['role'] for m in messages] == ['system', 'user']
    return messages[0]


def _tools(request):
    return _system(request).get('toolsAdded', [])


def _model_text(request):
    system = _system(request)
    return '\n'.join(list(system['sections'].values()) + [t['description'] for t in system.get('toolsAdded', [])])


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def framing_tokens(request):
    pack = request['context']['messages'][1]['content']
    return math.ceil((_bytes(request) - len(pack.encode())) / 2)


def test_every_run_kind_is_assembled(requests):
    assert set(requests) == set(RUNS)
    names = {kind: {t['name'] for t in _tools(r)} for kind, r in requests.items()}
    assert 'nexloop.service.request' in names['message_reply'] and 'nexloop.enterprise.deliver' in names['message_reply']
    assert 'nexloop.plan.outcome' in names['plan_reevaluation'] and 'nexloop.outreach.send' not in names['plan_reevaluation']
    assert 'nexloop.outreach.send' in names['outreach'] and 'nexloop.plan.outcome' not in names['fallback_reply']
    # The simulation Run reads the simulation instructions, never the real ones, and its tools are the stand-ins of the real names
    # (no simulation-only tool exists, so HOST_ONLY needs no entry for it).
    simulation = _system(requests['simulation_reply'])['sections']['instructions']
    assert 'This Run is in a simulation world.' in simulation and 'formal_facts' not in simulation
    assert names['simulation_reply'] == {'nexloop.incentive.issue', 'nexloop.service.request', 'nexloop.service.find'}
    assert all(t['description'].startswith('Simulation world:') for t in _tools(requests['simulation_reply']))


@pytest.mark.parametrize('kind', RUNS)
def test_tool_names_match_the_provider_wire_names(requests, kind):
    source = (ROOT / 'apps' / 'agent-host' / 'src' / 'provider-tool-names.ts').read_text()
    assert r"/^nexloop\.[A-Za-z0-9_.-]+$/" in source and r"/^[A-Za-z0-9_-]{1,128}$/" in source and "replaceAll('.', '_')" in source
    names = [t['name'] for t in _tools(requests[kind])]
    assert names and len(set(names)) == len(names)
    assert all(re.fullmatch(r'nexloop\.[A-Za-z0-9_.-]+', n) for n in names)
    wire = [n.replace('.', '_') for n in names]
    assert all(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', w) for w in wire) and len(set(wire)) == len(wire)


@pytest.mark.parametrize('kind', RUNS)
def test_every_action_the_model_reads_is_published(requests, kind):
    published = _published_actions()
    tools = {t['name'] for t in _tools(requests[kind])}
    named = set(re.findall(r'nexloop\.[a-z_]+\.[a-z_]+', _model_text(requests[kind])))
    for name in sorted(tools | named):
        assert name in published or name in HOST_ONLY, f'{kind}: {name} is neither a published Action nor a listed Host-only tool'


def test_host_only_tools_are_still_offered_and_not_actions(requests):
    offered = {t['name'] for r in requests.values() for t in _tools(r)}
    for name, reason in HOST_ONLY.items():
        assert name in offered and name not in _published_actions() and reason


@pytest.mark.parametrize('kind', RUNS)
def test_framing_stays_within_the_run_kind_budget(requests, kind):
    tokens = framing_tokens(requests[kind])
    assert tokens <= FRAMING_BUDGET[kind], f'{kind}: framing {tokens} tokens > budget {FRAMING_BUDGET[kind]}'


@pytest.mark.parametrize('kind', RUNS)
def test_object_type_names_the_model_reads_are_in_a_glossary(requests, kind):
    terms = _glossary_terms()
    camel = set(re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', _model_text(requests[kind])))
    assert camel <= terms, f'{kind}: undefined terms {sorted(camel - terms)}'


# S84: v3 (3750) is held after the D1 calibration (L6); v2 (3900) still covers every budget below. S82 (owner 2026-11-01): context strategies v2 reserve 3900 for framing, the K1 maximum (3409) plus 15%; v1's 1500 was below every
# Run kind's framing, so pack plus framing could exceed input_token_budget.
@pytest.mark.parametrize('kind', RUNS)
def test_framing_fits_the_context_strategy_framing_reserve(requests, kind):
    reserve = min(s['framing_reserve'] for s in json.loads(CONTEXT_STRATEGIES.read_text())['strategies'])
    assert framing_tokens(requests[kind]) <= reserve


def test_the_reserve_covers_every_run_kind_budget():
    # Every strategy may serve every Run kind (relay --context-strategy, the reply guarantee's fallback), so each reserves the largest.
    assert all(s['framing_reserve'] >= max(FRAMING_BUDGET.values()) for s in json.loads(CONTEXT_STRATEGIES.read_text())['strategies'])


NARROW = {kind: {**run, 'pack': {k: v for k, v in run['pack'].items() if k not in WIDEST}} for kind, run in RUNS.items() if kind != 'simulation_reply'}


@pytest.fixture(scope='module')
def narrow_requests(tmp_path_factory):
    node = shutil.which('node')
    path = tmp_path_factory.mktemp('model-surface-narrow') / 'runs.json'
    path.write_text(json.dumps(NARROW, ensure_ascii=False))
    result = subprocess.run([node, '--no-warnings', str(DUMP), str(path)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


@pytest.mark.parametrize('kind', NARROW)
def test_tools_are_offered_only_when_the_context_needs_them(requests, narrow_requests, kind):
    """S85: without an IncentiveOffering in open work no incentive tool; the reply keeps request_scope (scope denials, CI 101).
    The surface shrinks; the widest still bounds the budget."""
    names = {t['name'] for t in _tools(narrow_requests[kind])}
    assert 'nexloop.incentive.issue' not in names and 'nexloop.incentive.issue' in {t['name'] for t in _tools(requests[kind])}
    (reply,) = [t for t in _tools(narrow_requests[kind]) if t['name'] == 'nexloop.service.request']
    assert set(reply['parameters']['properties']) == {'message', 'request_scope'} and 'request_scope' in reply['description']
    assert framing_tokens(narrow_requests[kind]) < framing_tokens(requests[kind]) - 400
