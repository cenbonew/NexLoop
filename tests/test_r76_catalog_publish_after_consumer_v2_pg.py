"""R76 step 6 (dispatcher 2026-11-10): `stage_secrets.py publish` of c58f723e onto a tenant in the stage's state - the catalog
published without the ten Actions of batches 152b-160 (NX-062 M2b/M4a/M5c, NX-067), then the owner's real F3 publication of Consumer v2
(not in the repository manifests, which declare Consumer v1 only) and register deriving the :2 grants. The real publish command's dry
run plans exactly the ten Actions with no conflict and no Consumer type change; the real publish adds them and leaves the Consumer v2
publication, its successor Actions and the derived :2 grants as they were; after workbench_roles --apply the owner reaches each of the
ten Actions (action_for: published and granted, not 404) and executes every one of them at :1 although they bind Consumer:1 and every
Consumer is at v2 (L6 C1: their handlers never touch a Consumer object). Synthetic data."""
import json
import secrets
import subprocess
import sys

import pytest

from test_stage_secrets import S  # noqa: F401  (puts scripts/deploy on sys.path)
import assembly_dryrun_fixture as A  # noqa: E402
from test_assembly_dryrun_pg import TENANT, assemble, owner_workbench, stage, tree  # noqa: F401
from test_consumer_v2_stage_path_pg import owner_publishes_consumer_v2, register
from test_machine_reads_pg import commitment, put

NEW_ACTIONS = {  # stable name -> workbench operation (goal_controls.CAPABILITIES key)
    'nexloop.machine_client.register': 'register_machine_client', 'nexloop.machine_client.deactivate': 'deactivate_machine_client',
    'nexloop.machine_client.activate': 'activate_machine_client', 'nexloop.machine_key.issue': 'issue_machine_key',
    'nexloop.machine_key.rotate': 'rotate_machine_key', 'nexloop.machine_key.revoke': 'revoke_machine_key',
    'nexloop.machine_client.grant': 'grant_machine_capability', 'nexloop.machine_client.revoke_grant': 'revoke_machine_grant',
    'nexloop.object.add_note': 'object_note_add', 'nexloop.workbench_member.set_sso_group_mapping': 'set_sso_group_mapping'}


@pytest.fixture
def catalog_before_r76(monkeypatch):
    """The stage's catalog at 7a4dab53: the compiled catalog without the ten Actions."""
    original = A.compile_catalog

    def without_new(tenant):
        compiled = original(tenant)
        return {**compiled, 'actions': [a for a in compiled['actions'] if a['definition']['stable_name'] not in NEW_ACTIONS]}
    monkeypatch.setattr(A, 'compile_catalog', without_new)


@pytest.fixture
def stage_before_r76(catalog_before_r76, stage):  # noqa: F811  (catalog_before_r76 first: the stage fixture publishes the catalog)
    return assemble(stage)


def publish(st, capsys, *, dry_run):
    argv = ['publish', '--out', str(st.out), '--tenant', TENANT, '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-id', 'stage-1']
    capsys.readouterr()
    code = S.main(argv + (['--dry-run'] if dry_run else []))
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out.strip().splitlines()[-1])


def consumer_state(admin):
    return {
        'types': admin.execute("select version, definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' order by version",
                               (TENANT,)).fetchall(),
        'objects': admin.execute("select schema_version, count(*) from ontology.objects where tenant_id=%s and type_name='Consumer' group by 1",
                                 (TENANT,)).fetchall(),
        'successors': admin.execute("""select resource_id, definition, active from control.nexloop_action_definitions
            where tenant_id=%s and resource_id like '%%:2' order by resource_id""", (TENANT,)).fetchall(),
        'grants': admin.execute("""select entity_key, payload from authz.nexloop_authority_facts
            where tenant_id=%s and fact_kind='grants' and entity_key[2] like '%%:2' order by entity_key""", (TENANT,)).fetchall()}


def test_publish_after_the_owners_consumer_v2_adds_only_the_ten_actions(stage_before_r76, admin, capsys):
    st = stage_before_r76
    assert not admin.execute("select 1 from control.nexloop_action_definitions where tenant_id=%s and definition->>'stable_name'=any(%s)",
                             (TENANT, list(NEW_ACTIONS))).fetchall()
    owner_workbench(st, admin)
    published = owner_publishes_consumer_v2(st, admin)
    assert published['published'] is True
    register(st, dry_run=False)
    before = consumer_state(admin)
    assert [v for v, _ in before['types']] == [1, 2] and before['objects'] == [(2, 1)]
    assert any(r[0][1].endswith('Consumer.create:2') for r in before['grants'])

    plan = publish(st, capsys, dry_run=True)
    assert plan['dry_run'] is True and plan['conflicts'] == []
    assert plan['actions_to_publish'] == sorted(NEW_ACTIONS)
    assert consumer_state(admin) == before

    done = publish(st, capsys, dry_run=False)
    assert done['changed'] is True and done['actions_to_publish'] == sorted(NEW_ACTIONS)
    after = consumer_state(admin)
    assert after == before  # no Consumer type added, rewritten or lowered; v2 objects, :2 successors and :2 grants unchanged
    assert publish(st, capsys, dry_run=True)['actions_to_publish'] == []  # idempotent: nothing left to send

    # workbench_roles --apply (R76 step 7) after publish; then every new Action is reachable by the owner.
    argv = [sys.executable, str(A.ROOT / 'scripts/deploy/workbench_roles.py'), '--roles', str(A.ROOT / 'deploy/authorization/workbench-roles.v1.json'),
            '--members', str(st.root / 'workbench-members.json'), '--apply', '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-file',
            str(st.out / 'management' / 'signing.key.hex'), '--signing-key-id', 'stage-1']
    roles = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert roles.returncode == 0, (roles.stdout, roles.stderr)

    from nexloop_eios.backend import open_backend
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.context_strategy_setup import owner_session
    from nexloop_eios.goal_controls import CAPABILITIES, GoalGovernedActions
    rate_key = bytes.fromhex((st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key').read_text().strip())
    recipe = {'tenant_id': TENANT, 'workbench_application_id': 'stage-workbench'}
    with open_backend(database_url=st.api_dsn.read_text().strip(), artifact_root=st.artifacts, signing_key_file=st.signing_key, signing_key_id='stage-1') as api, \
            owner_session((st.root / 'identity-dsn').read_text().strip(), rate_key, recipe, 'dryrun-owner', (st.root / 'owner-password').read_text(),
                          'https://127.0.0.1:8443') as session:
        human = authenticate_browser_business(api._pool, session, world='real')
        found = {}
        with api._pool.connection() as db:
            for name, operation in NEW_ACTIONS.items():
                found[name] = db.execute('select authz.nexloop_workbench_action_for(%s,%s,%s)',
                                         (human.token_digest, human.world, CAPABILITIES[operation])).fetchone()[0]
        assert {n: (a or {}).get('granted') for n, a in found.items()} == {n: True for n in NEW_ACTIONS}
        assert {n: (a['stable_name'], a['version']) for n, a in found.items()} == {n: (n, 1) for n in NEW_ACTIONS}
        # L6 C1: all ten bind Consumer:1 in their scope (business-actions.v1.json object_type) while every real Consumer is at v2 and no :2
        # of them exists (they were published after the F3 publication). Each executes at :1: their handlers touch machine clients, keys,
        # grants, the SSO mapping or a note on a Commitment - never a Consumer object - so the Consumer version never enters the check.
        G = GoalGovernedActions(api._pool, human, api._signer)
        results = {}

        def run(name, **fields):
            action = found[name]
            results[name] = getattr(G, NEW_ACTIONS[name])(action_name=action['stable_name'], action_version=action['version'],
                                                         request_id='r76-' + NEW_ACTIONS[name], **fields)
            return results[name]

        def key_fields():
            from nexloop_eios import machine_keys as K
            return {'prefix_id': K.generate().prefix_id, 'digest': secrets.token_hex(32), 'pepper_version': 1}
        run('nexloop.machine_client.register', client_id='r76-check', display_name='R76 check', purpose='R76 step 6 local check (synthetic)')
        issued = run('nexloop.machine_key.issue', client_id='r76-check', world='test', scope=['object.add_note'], expires_in_days=30, **key_fields())
        rotated = run('nexloop.machine_key.rotate', previous_key_id=issued['key_id'], overlap_days=1, scope=['object.add_note'], expires_in_days=30, **key_fields())
        run('nexloop.machine_key.revoke', key_id=rotated['key_id'], reason='R76 本地核对')
        run('nexloop.machine_client.grant', client_id='r76-check', capability='object.add_note', object_types=['Commitment'])
        run('nexloop.machine_client.revoke_grant', client_id='r76-check', capability='object.add_note', reason='R76 本地核对')
        run('nexloop.machine_client.deactivate', client_id='r76-check', reason='R76 本地核对')
        run('nexloop.machine_client.activate', client_id='r76-check', reason='R76 本地核对')
        run('nexloop.workbench_member.set_sso_group_mapping', provider_id='corp', group_claim='groups', mappings={'ops': 'reviewer'}, expected_revision=0)
        consumer = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Consumer'", (TENANT,)).fetchone()[0]
        target = put(admin, 'Commitment', commitment(consumer), tenant=TENANT)  # a note on a Commitment made to the v2 Consumer
        run('nexloop.object.add_note', type_name='Commitment', object_id=target, text='R76 本地核对的合成备注')
    assert set(results) == set(NEW_ACTIONS) and all(type(r) is dict and r.get('outcome_id') for r in results.values()), results
    assert admin.execute('select count(*) from authz.nexloop_machine_clients where tenant_id=%s', (TENANT,)).fetchone() == (1,)
    assert admin.execute('select count(*) from runtime.nexloop_object_notes where tenant_id=%s and object_id=%s', (TENANT, target)).fetchone() == (1,)
    assert admin.execute("select schema_version from ontology.objects where tenant_id=%s and object_id=%s", (TENANT, consumer)).fetchone() == (2,)
    assert consumer_state(admin) == after
