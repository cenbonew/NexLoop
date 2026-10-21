"""Outreach slice 5, positive path: a plan reevaluation Role Run whose Role execution ceiling holds nexloop.outreach.send:1, in a
tenant with a published operator persona, binds a Context v6 pack carrying both optional sections (operator_persona with the
tenant's AI disclosure, outreach with the enabled purposes). The pack matches the contract, the Role bind re-derives both and
cites them as role sources, the Host parser accepts it, the digest and the binding replay unchanged, and a producer that
alters either section is refused. Actual PostgreSQL and the built Host parser (Node 24); synthetic data, admin seeds/probes."""
import copy
import hashlib
import json
import uuid

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.context_artifacts import ContextArtifactUnavailable
from nexloop_eios.contracts import ContextPackV6
from nexloop_eios.role_context_artifacts import RoleContextV6ArtifactProducer
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from test_context_input import run_parser
from test_context_operator_persona_pg import DOCUMENT, seed_persona
from test_context_v6_pg import owner, sources
from test_role_context_v6_pg import STRATEGY_ID, new_role_run, role_row, role_v6  # noqa: F401

SEND = 'eios:action:nexloop.outreach.send:1'
PERSONA = {'persona_ref': 'operator-persona:xiaohe@1', 'persona_id': 'xiaohe', 'version': 1, 'source': 'default', 'document': DOCUMENT,
           'ai_disclosure': {'mode': 'none', 'when_asked': 'deflect', 'block_human_claims': False, 'content_label': False}}


def outreach_role_run(plan, admin):
    """The Role's ceiling also names the send (as its governed edit leaves it); the new Run is linked to a plan as reevaluation Runs are."""
    tenant = plan['tenant']
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""update ontology.objects set properties=jsonb_set(properties,'{action_resources}',properties->'action_resources'||%s)
            where tenant_id=%s and type_name='RoleExecutionCeiling' and not properties->'action_resources' ? %s""", (Jsonb([SEND]), tenant, SEND))
    source, run, command, ids = new_role_run(plan, admin)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into runtime.nexloop_plan_runs(tenant_id,world,run_id,plan_id,version,triggers) values(%s,'real',%s,%s,1,%s)",
                      (tenant, run.run_id, str(uuid.uuid4()), Jsonb([{'kind': 'reassess_due', 'cause': 'system'}])))
    return source, run, command, ids


def expected_outreach(admin, tenant):
    listed = admin.execute('select definition from control.nexloop_outreach_purpose_lists order by version desc limit 1').fetchone()[0]
    return {'schema': 'nexloop-outreach-context/1', 'list_version': listed['version'], 'marketing': {'enabled': False, 'consent_mode': 'opt_out'},
            'purposes': [{k: p[k] for k in ('purpose_id', 'kind', 'topic', 'frequency_class', 'requires_approval', 'content_rules')}
                         for p in listed['purposes'] if p['default_enabled']]}


def test_a_role_pack_with_persona_and_outreach_binds_completely_and_replays(role_v6, admin, tmp_path, monkeypatch):
    plan = role_v6; tenant = plan['tenant']
    seed_persona(admin, tenant)
    source, run, command, ids = outreach_role_run(plan, admin)
    # Keep the assembled pack: a Role Run is assembled once (a second assembly re-derives its items and is refused as an artifact
    # conflict, with or without these sections); the bind replay below resubmits exactly these bytes.
    assembled = []
    original = RoleContextV6ArtifactProducer.assemble

    def assemble(self, snapshot, command):
        if not assembled: assembled.append(original(self, snapshot, command))
        return assembled[0]
    monkeypatch.setattr(RoleContextV6ArtifactProducer, 'assemble', assemble)
    bound = RoleContextV6ArtifactProducer(source, STRATEGY_ID).prepare(run_token=run.token, command=command, body='计划复评：承诺即将到期', **ids)
    pack = json.loads(bound['input'])
    # Contract: both optional sections, exactly as SQL derives them.
    ContextPackV6.model_validate(pack)
    assert pack['role'] is not None and pack['user_statement'] is None and pack['current_event']['kind'] == 'service_trigger'
    assert pack['operator_persona'] == PERSONA
    assert pack['outreach'] == expected_outreach(admin, tenant) and [p['purpose_id'] for p in pack['outreach']['purposes']] == ['commitment_followup', 'service_followup']
    # The binding stores exactly these bytes; the digest is reproducible from them.
    row = role_row(admin, run.run_id)
    assert row[0] == bound['input'] and hashlib.sha256(row[0].encode()).hexdigest() == row[1] == bound['sha256']
    # Both sections are cited as role sources of the bound Context.
    cited = [(r[1], r[2], r[3]) for r in sources(admin, tenant, row[2]) if r[1] == 'role']
    assert ('role', 'operator-persona:xiaohe@1', '1') in cited and ('role', 'outreach-purposes@2', '2') in cited
    with admin.transaction():
        owner(admin, tenant)
        assert admin.execute("select count(*) from runtime.nexloop_context_packs where run_id=%s and pack_digest=%s", (run.run_id, row[1])).fetchone() == (1,)
    # The same pack for the same Run replays: same bytes, digest and binding, no second Context.
    replayer = RoleContextV6ArtifactProducer(source, STRATEGY_ID)
    try:
        again = replayer.prepare(run_token=run.token, command=command, body='计划复评：承诺即将到期', **ids)
    except ContextArtifactUnavailable:
        raise AssertionError(replayer.last_diagnostic)
    assert again['input'] == bound['input'] and again['sha256'] == bound['sha256'] and role_row(admin, run.run_id) == row
    with admin.transaction():
        owner(admin, tenant)
        assert admin.execute('select count(*) from runtime.nexloop_context_packs where run_id=%s', (run.run_id,)).fetchone() == (1,)
    # Host: as the activated Run receives it (its command names the bound Context artifact), the built parser accepts the Role pack
    # with both sections (data only) and refuses a forged one.
    command = {**command, 'context_manifest_ref': bound['artifact_ref']}
    assert json.loads(run_parser(tmp_path, command, pack))['body'] == pack['current_event']['body']
    for change in (lambda p: p['outreach'].update(would_dispatch=True), lambda p: p['outreach']['purposes'][0].update(body='您好'),
                   lambda p: p['operator_persona']['ai_disclosure'].update(when_asked='truthful')):
        forged = copy.deepcopy(pack); change(forged)
        assert run_parser(tmp_path, command, forged) == 'runtime_context_invalid'


@pytest.mark.parametrize('mode,expected', [('outreach_dropped', 'context v6 outreach mismatch'), ('outreach_purpose_added', 'context v6 outreach mismatch'),
                                           ('persona_dropped', 'context v6 operator persona mismatch')])
def test_sql_refuses_a_role_pack_whose_sections_differ(role_v6, admin, monkeypatch, mode, expected):
    from test_context_v6_pg import _recount
    plan = role_v6
    seed_persona(admin, plan['tenant'])
    original = RoleContextV6ArtifactProducer.assemble

    def assemble(self, snapshot, command):
        body, outcome, items, proofs = original(self, snapshot, command)
        if mode == 'outreach_dropped': body.pop('outreach')
        elif mode == 'persona_dropped': body.pop('operator_persona')
        else: body['outreach']['purposes'].append({**body['outreach']['purposes'][0], 'purpose_id': 'marketing_general', 'topic': 'marketing'})
        _recount(body)
        return body, outcome, items, proofs
    monkeypatch.setattr(RoleContextV6ArtifactProducer, 'assemble', assemble)
    source, run, command, ids = outreach_role_run(plan, admin)
    producer = RoleContextV6ArtifactProducer(source, STRATEGY_ID)
    with pytest.raises(ContextArtifactUnavailable):
        producer.prepare(run_token=run.token, command=command, body='计划复评', **ids)
    assert producer.last_diagnostic is not None and producer.last_diagnostic[1] == expected, producer.last_diagnostic
    assert role_row(admin, run.run_id) is None
