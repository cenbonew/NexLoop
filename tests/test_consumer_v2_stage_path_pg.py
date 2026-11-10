"""P0 2026-11-10 (L6 P4 on A'): the stage path with no simulated step. The NX-032 assembly (bootstrap, stage_secrets register, browser,
system catalog, business/catalog/enterprise setup, relay enrollment) then the owner's real Schema publication of Consumer v2 through
SchemaPublicationPort (the owner's workbench browser session, which holds ontology.schema.review; F3 authz.nexloop_schema_proposal_publish
moves the Consumer to v2 and publishes every successor), then stage_secrets register again: its dry run derives exactly the registrar
successor grants (relay_planner and role_planner -> nexloop.plan.bind_effect_context:2, scope Consumer 1 -> 2) and its apply writes them.
The existing customer's next message then passes the real message relay worker (issue, effect_bind with the registrar :2, context,
runtime accept: queued), where stage failed with 'registrar schema scope unavailable'. The proposal and its release permit are seeded as
tests/test_nx040_* seed them (F2/F4 are not this path); everything after is production code. Synthetic data."""
import json
import uuid
from datetime import UTC, datetime, timedelta

from test_stage_secrets import S  # noqa: F401  (puts scripts/deploy on sys.path)
import assembly_dryrun_fixture as A  # noqa: E402
from test_assembly_dryrun_pg import (TENANT, apply_webchat_consumer_role, assembled, owner_workbench, publish_strategy, run_workers,  # noqa: F401
                                     set_trial_budget, stage, tree, webchat_message)
from test_nx040_schema_publication_pg import proposal

BIND2 = 'eios:action:nexloop.plan.bind_effect_context:2'


def owner_publishes_consumer_v2(st, admin):
    from nexloop_eios.backend import open_backend
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.context_strategy_setup import owner_session
    from nexloop_eios.schema_publication import SchemaPublicationPort
    pid = proposal({'admin': admin, 'tenant': TENANT}, 'add_optional_property', 'Consumer', {'property_name': 'delivery_windows', 'value_type': 'string'})
    rate_key = bytes.fromhex((st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key').read_text().strip())
    recipe = {'tenant_id': TENANT, 'workbench_application_id': 'stage-workbench'}
    with open_backend(database_url=st.api_dsn.read_text().strip(), artifact_root=st.artifacts, signing_key_file=st.signing_key, signing_key_id='stage-1') as api, \
            owner_session((st.root / 'identity-dsn').read_text().strip(), rate_key, recipe, 'dryrun-owner', (st.root / 'owner-password').read_text(),
                          'https://127.0.0.1:8443') as session:
        human = authenticate_browser_business(api._pool, session, world='real')
        return SchemaPublicationPort(api._pool, human, api._signer).publish(pid, rationale='顾客常提送货时间', idempotency_key='stage-path-consumer-v2-' + str(uuid.uuid4()))


def register(st, *, dry_run):
    from configurator_window import configurator_window
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with configurator_window(st.bootstrap_dsn) as dsn:
        return S.register(st.out, tenant=TENANT, database_url_file=dsn, signing_key_id='stage-1', credential_expires_at=expires, dry_run=dry_run)


def test_after_the_owners_publication_and_register_the_existing_customer_is_served(assembled, admin, capsys):  # noqa: F811
    st = assembled
    owner_workbench(st, admin)
    publish_strategy(st)
    set_trial_budget(st, admin)
    apply_webchat_consumer_role(st, admin)
    published = owner_publishes_consumer_v2(st, admin)
    assert published['published'] is True and BIND2 in published['successor_actions']
    assert 'eios:action:nexloop.service.request:2' in published['successor_actions']
    assert admin.execute("select schema_version,count(*) from ontology.objects where tenant_id=%s and type_name='Consumer' group by 1", (TENANT,)).fetchall() == [(2, 1)]
    preview = register(st, dry_run=True)['follow_latest']
    derived = sorted((d['principal'], d['resource_id'], json.dumps(d['scope_diff'])) for d in preview['derived'] if 'bind_effect_context' in d['resource_id'])
    diff = json.dumps([{'type': 'Consumer', 'from': 1, 'to': 2}])
    assert derived == [('relay_planner', BIND2, diff), ('role_planner', BIND2, diff)] and preview['refused'] == []
    assert all(f"(schema publication {published['publication_id']})" in d['purpose'] for d in preview['derived'] if 'bind_effect_context' in d['resource_id'])
    applied = register(st, dry_run=False)
    assert applied['grants_added'] >= 2
    holders = sorted(k[0] for (k,) in admin.execute("select entity_key from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[2]=%s",
                                                     (TENANT, BIND2)).fetchall())
    manifest, _ = S.merged_manifest()
    roles = {p['principal_id']: p['role'] for p in manifest['principals']}
    assert sorted(roles[h] for h in holders) == ['relay_planner', 'role_planner']
    message = webchat_message(st, admin, capsys)
    code, detail = run_workers(st, admin, capsys, ('message-relay',))['message-relay']
    with capsys.disabled():
        print(f'\nCONSUMER V2 relay: {code} {detail[:300]}')
    assert code == 0 and '"status":"queued"' in detail, detail
    assert admin.execute('select count(*) from runtime.nexloop_message_routes where tenant_id=%s and message_id=%s', (TENANT, message)).fetchone() == (1,)
    assert sorted({v for (v,) in admin.execute("""select claim->'binding'->'action_reference'->>'version' from runtime.nexloop_action_claims
        where tenant_id=%s and action_name='nexloop.plan.bind_effect_context'""", (TENANT,)).fetchall()}) == ['2']


def test_a_new_customer_after_the_publication_is_refused_until_the_business_owner_follows_consumer_create(assembled, admin):  # noqa: F811
    """L6 Q6 on the stage path: business_owner holds Consumer.create:1 only, and L6 Q5 lets only the registrar's Actions follow, so after
    the publication a new customer is refused - no Consumer at v1 (no version inversion), no half setup. R76 step 7 (the new customer)
    needs the owner's decision first: a follow declaration on business_owner -> Consumer.create:1, or an explicit grant of :2."""
    st = assembled
    owner_workbench(st, admin)
    owner_publishes_consumer_v2(st, admin)
    register(st, dry_run=False)
    from test_assembly_dryrun_pg import create_human
    human = create_human(st, username='dryrun-second', label='second')
    business = A.private(st.root, 'business-recipe-2.json', json.dumps(A.filled('business-recipe.example.json', NEW_UUID=A.new_uuid(),
                         CONSUMER_PRINCIPAL_ID=human, TRIAL_PERIOD_END_UTC_ISO=A.end_of_trial())))
    code, out, err = A.run_tool('nexloop_eios.business_setup', [*st.backend_args(), '--owner-credential-file', str(st.setup_file('business-owner-credential')),
                                '--executor-credential-file', str(st.setup_file('business-executor-credential')), '--recipe-file', str(business)])
    assert code != 0 and 'Business setup unavailable' in out + err
    assert admin.execute("select schema_version,count(*) from ontology.objects where tenant_id=%s and type_name='Consumer' group by 1", (TENANT,)).fetchall() == [(2, 1)]
