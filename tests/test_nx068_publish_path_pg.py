"""NX-068 on the stage path (the NX-032 assembly with stage_secrets register, as tests/test_consumer_v2_stage_path_pg.py): the owner's
real F3 publication of Consumer v2 through SchemaPublicationPort.
- N2: register writes the follow marker into the grant facts of the FOLLOWABLE declarations only.
- Q8: without the registrar's follow declarations the precheck refuses with remedies and the publish transaction answers published:false
  (source gates) and writes nothing - no type version, no successor, no object moved, the release permit unused, no smoke queued.
- Q9: a publication queues two smoke rounds; they run read-only (nothing written but their own rows) and pass once register derived
  the followed grants; round 2 before register reports the holders still at :1.
Synthetic data."""
import json
import uuid

from test_stage_secrets import S  # noqa: F401  (puts scripts/deploy on sys.path)
from test_assembly_dryrun_pg import TENANT, assembled, owner_workbench, stage, tree  # noqa: F401
from test_consumer_v2_stage_path_pg import register
from test_nx040_schema_publication_pg import proposal

from nexloop_eios import service_grants as G

BIND = 'nexloop.plan.bind_effect_context'


def owner_port(st, admin, work):
    from nexloop_eios.backend import open_backend
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.context_strategy_setup import owner_session
    from nexloop_eios.schema_publication import SchemaPublicationPort
    rate_key = bytes.fromhex((st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key').read_text().strip())
    recipe = {'tenant_id': TENANT, 'workbench_application_id': 'stage-workbench'}
    with open_backend(database_url=st.api_dsn.read_text().strip(), artifact_root=st.artifacts, signing_key_file=st.signing_key, signing_key_id='stage-1') as api, \
            owner_session((st.root / 'identity-dsn').read_text().strip(), rate_key, recipe, 'dryrun-owner', (st.root / 'owner-password').read_text(),
                          'https://127.0.0.1:8443') as session:
        return work(SchemaPublicationPort(api._pool, authenticate_browser_business(api._pool, session, world='real'), api._signer))


def consumer_proposal(admin):
    return proposal({'admin': admin, 'tenant': TENANT}, 'add_optional_property', 'Consumer', {'property_name': 'delivery_windows', 'value_type': 'string'})


def publish(port, pid):
    return port.publish(pid, rationale='顾客常提送货时间', idempotency_key='nx068-' + str(uuid.uuid4()))


def state(admin):
    return admin.execute("""select (select max(version) from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'),
        (select count(*) from control.nexloop_action_definitions where tenant_id=%s),
        (select array_agg(distinct schema_version) from ontology.objects where tenant_id=%s and type_name='Consumer'),
        (select count(*) from ontology.nexloop_schema_publications where tenant_id=%s),
        (select count(*) from runtime.nexloop_schema_publish_smokes where tenant_id=%s)""", (TENANT,) * 5).fetchone()


def markers(admin):
    return sorted(tuple(k) for (k,) in admin.execute("""select entity_key from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants'
        and exists(select 1 from jsonb_array_elements(payload->'grants') g where g->>'grant_id' like %s)""", (TENANT, '%' + G.FOLLOW_MARKER)).fetchall())


def test_register_marks_only_the_followable_declarations(assembled, admin):  # noqa: F811
    st = assembled
    manifest, _ = S.merged_manifest()
    roles = {p['principal_id']: p['role'] for p in manifest['principals']}
    assert G.FOLLOWABLE == S.FOLLOWABLE
    declared = {(g['principal'], g['resource_id']) for g in manifest['grants'] if g.get(G.FOLLOW)}
    assert ('claim_matcher', 'eios:action:Consumer.edit:1') in declared  # declared follow, not FOLLOWABLE: never marked
    marked = {(roles[k[0]], k[1]) for k in markers(admin)}
    assert marked == {d for d in declared if d[1] in G.FOLLOWABLE} and {r for _, r in marked} == set(G.FOLLOWABLE)
    assert not any(r not in G.FOLLOWABLE for _, r in marked)
    del st


def test_without_the_registrar_follow_the_publication_is_refused_and_writes_nothing(assembled, admin, tmp_path):  # noqa: F811
    st = assembled
    owner_workbench(st, admin)
    stage = json.loads(S.PRINCIPALS.read_text())
    for g in stage['grants']:
        if g['resource_id'].startswith(f'eios:action:{BIND}:'):
            g.pop('follow_latest_version', None)
    principals = tmp_path / 'stage-principals.json'
    principals.write_text(json.dumps(stage))
    register(st, dry_run=False, principals_path=principals)
    assert not any(k[1].startswith(f'eios:action:{BIND}:') for k in markers(admin))
    pid = consumer_proposal(admin)
    readiness = owner_port(st, admin, lambda p: p.readiness(pid))
    refused = [i for i in readiness['items'] if i['level'] == 'refuse']
    assert readiness['verdict'] == 'refuse' and refused and all(i['remedy'] for i in readiness['items'])
    assert {i['code'].split(':')[0] for i in refused} == {'registrar_holder_without_follow'}
    before = state(admin)
    result = owner_port(st, admin, lambda p: publish(p, pid))
    assert result['published'] is False and result['source'] == 'gates', result
    assert sorted(result['codes']) == sorted('publish_gate:' + i['code'] for i in refused)
    assert state(admin) == before and before[0] == 1 and before[4] == 0
    assert admin.execute("""select used_at from runtime.nexloop_evo_release_permits p join ontology.nexloop_schema_proposals r on r.candidate_id=p.candidate_id
        where r.proposal_id=%s""", (pid,)).fetchone()[0] is None
    assert admin.execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'frozen'


def run_due(admin, round_):
    admin.execute("update runtime.nexloop_schema_publish_smokes set due_at=now()-interval '1 second' where tenant_id=%s and round=%s and outcome='pending'", (TENANT, round_))
    return admin.execute('select runtime.nexloop_run_schema_publish_smokes()').fetchone()[0]


def smokes(admin):
    return admin.execute('select round,outcome,checks from runtime.nexloop_schema_publish_smokes where tenant_id=%s order by round', (TENANT,)).fetchall()


def counts(admin):
    return admin.execute("""select (select count(*) from ontology.objects),(select count(*) from control.nexloop_action_definitions),
        (select count(*) from runtime.nexloop_effect_intents),(select count(*) from authz.nexloop_authority_facts),
        (select count(*) from control.nexloop_effect_scope_proofs)""").fetchone()


def test_a_publication_queues_read_only_smoke_rounds_that_pass_after_register(assembled, admin):  # noqa: F811
    st = assembled
    owner_workbench(st, admin)
    pid = consumer_proposal(admin)
    readiness = owner_port(st, admin, lambda p: p.readiness(pid))
    assert readiness['verdict'] in ('pass', 'warn') and not [i for i in readiness['items'] if i['level'] == 'refuse'], readiness
    assert readiness['acknowledge_required'] is (readiness['verdict'] == 'warn')
    published = owner_port(st, admin, lambda p: publish(p, pid))
    assert published['published'] is True, published
    assert [(r, o) for r, o, _ in smokes(admin)] == [(1, 'pending'), (2, 'pending')]
    assert admin.execute('select runtime.nexloop_run_schema_publish_smokes()').fetchone()[0] == 0  # nothing due yet
    before = counts(admin)
    assert run_due(admin, 1) == 1
    assert smokes(admin)[0][1:] == ('passed', []), smokes(admin)
    assert counts(admin) == before
    register(st, dry_run=False)
    after_register = counts(admin)
    assert run_due(admin, 2) == 1
    assert smokes(admin)[1][1:] == ('passed', []), smokes(admin)
    assert counts(admin) == after_register
    snapshot = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    assert snapshot['schema_publish_smoke'] == {'failed_24h': 0, 'overdue': 0, 'problems': 0}


def test_round_two_before_register_reports_the_holders_still_at_their_old_version(assembled, admin):  # noqa: F811
    st = assembled
    owner_workbench(st, admin)
    pid = consumer_proposal(admin)
    assert owner_port(st, admin, lambda p: publish(p, pid))['published'] is True
    run_due(admin, 1)
    run_due(admin, 2)
    (_, first, _), (_, second, checks) = smokes(admin)
    assert first == 'passed' and second == 'failed'
    assert any(c.startswith('holder_without_grant:') and c.endswith(f'eios:action:{BIND}:2') for c in checks), checks
    assert any(c.endswith('eios:action:Consumer.create:2') for c in checks), checks
    snapshot = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    assert snapshot['schema_publish_smoke']['failed_24h'] == 1 and snapshot['schema_publish_smoke']['problems'] == 1
