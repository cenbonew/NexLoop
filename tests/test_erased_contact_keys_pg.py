"""NX-053 (0154) on clean catalog PostgreSQL: an erased contact key refuses contact, the owner revokes it, restore keeps both.

The erased key is seeded as the NX-029 erasure step leaves it (salted HMAC of the identity reference, per-tenant salt); the
erasure itself is tested in test_erasure_pg. Enforcement: the Consumer guard (identity association / import) and the
conversation ownership of a principal whose browser identity was erased (actual registrar, signed command). Revocation is the
owner's governed human Action through the workbench HTTP entry (production create_app, members applied by the real
configurator, role manifest v9). Restore: the journal and the replay of the key states. Synthetic data only.
"""
import json
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.conversation_messages import ConversationDenied
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from support.tenants import TENANT_A
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business, published_action  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, workbench_login, write_facts

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
REF = 'nexloop:browser-local:' + 'ab' * 32


def owner_sql(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute(sql, params).fetchone()[0]


def erase_refs(admin, tenant, refs, request_id='erasure-req-1', world='real'):
    """As the NX-029 erasure step leaves it: the tenant salt and one HMAC per identity reference."""
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute('insert into control.nexloop_erasure_salts(tenant_id,salt) values(%s,extensions.gen_random_bytes(32)) on conflict do nothing', (tenant,))
        for ref in refs:
            admin.execute('''insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id)
                select %s,%s,encode(extensions.hmac(convert_to(%s,'UTF8'),salt,'sha256'),'hex'),%s from control.nexloop_erasure_salts where tenant_id=%s
                on conflict do nothing''', (tenant, world, ref, request_id, tenant))


def revoke_directly(admin, tenant):
    """As the owner's revocation leaves every key of the tenant (the Action itself is tested through the workbench)."""
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("update control.nexloop_erased_contact_keys set revoked_at=now(),revoked_by='p',revoke_reason='r',revision=revision+1 where tenant_id=%s and revoked_at is null", (tenant,))


def erased(admin, tenant, ref, world='real'):
    return owner_sql(admin, 'select control.nexloop_contact_key_erased(%s,%s,%s)', (tenant, world, ref))


def keys(admin, tenant):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        return admin.execute('select key_digest,revoked_at is not null,revision,revoke_reason from control.nexloop_erased_contact_keys where tenant_id=%s order by key_digest',
                             (tenant,)).fetchall()


@pytest.fixture
def catalog(admin):
    bootstrap(admin)
    return admin


def test_comparison_is_by_hmac_only_and_revocation_and_a_new_erasure_change_it(catalog):
    admin, t = catalog, 'synthetic-tenant-a'
    assert erased(admin, t, REF) is False  # no salt, nothing erased
    erase_refs(admin, t, [REF])
    assert erased(admin, t, REF) is True and erased(admin, t, REF.replace('ab', 'cd')) is False and erased(admin, 'other-tenant', REF) is False
    assert erased(admin, t, REF, world='test') is False
    (digest, revoked, revision, reason), = keys(admin, t)
    assert REF not in json.dumps(keys(admin, t)) and revoked is False and revision == 1  # only the HMAC is stored
    revoke_directly(admin, t)
    assert erased(admin, t, REF) is False
    erase_refs(admin, t, [REF], request_id='erasure-req-2')  # the same identity erased again: the key refuses again
    assert erased(admin, t, REF) is True and keys(admin, t)[0][1:] == (False, 3, None)


def test_a_consumer_never_gains_an_erased_identity_reference(catalog):
    admin, t = catalog, 'synthetic-tenant-a'
    erase_refs(admin, t, [REF])
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # seed a Consumer as its creator leaves it
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Consumer','c1',1,'{"external_id_refs":[]}','nexloop-action','synthetic',now(),now())''', (t,))
    with pytest.raises(psycopg.Error) as refused:
        with admin.transaction():
            admin.execute('''update ontology.objects set properties='{"external_id_refs":["%s"]}' where tenant_id=%%s and object_id='c1' ''' % REF, (t,))
    assert refused.value.sqlstate == 'NXK01' and str(refused.value).startswith('contact unavailable') and REF not in str(refused.value)
    revoke_directly(admin, t)
    # Revoked: this guard lets the reference through (the 0061 identity guard still demands its own link receipt).
    with pytest.raises(psycopg.Error) as other:
        with admin.transaction():
            admin.execute('''update ontology.objects set properties='{"external_id_refs":["%s"]}' where tenant_id=%%s and object_id='c1' ''' % REF, (t,))
    assert other.value.sqlstate != 'NXK01'


def new_principal(admin, tenant, principal):
    """Another membership principal of the same browser subject (as the identity service creates one)."""
    row = admin.execute('select principal_id,subject_id,payload from control.nexloop_browser_memberships where tenant_id=%s and principal_id=%s', (tenant, principal)).fetchone()
    other = principal + '-again'
    admin.execute('insert into control.nexloop_browser_memberships(tenant_id,principal_id,subject_id,payload) values(%s,%s,%s,%s)',
                  (tenant, other, row[1], Jsonb({**row[2], 'principal_id': other})))
    return other


def audits(admin, tenant):
    return admin.execute("select request_id,payload from runtime.audit_events where tenant_id=%s and action='nexloop.contact_key.refused' order by created_at", (tenant,)).fetchall()


def test_a_principal_whose_identity_was_erased_is_not_assigned_to_a_new_consumer(conversations, admin):
    from nexloop_eios.object_actions import GovernedObjectCreator
    f = conversations
    tenant = TENANT_A
    refs = owner_sql(admin, 'select control.nexloop_principal_identity_refs(%s,%s,%s)', (tenant, 'real', f['principal']))
    assert refs and all(r.startswith('nexloop:browser-local:') for r in refs)
    erase_refs(admin, tenant, refs)
    other = new_principal(admin, tenant, f['principal'])
    session = f['registrar'].session
    consumer = GovernedObjectCreator(f['reader'].pool, session, f['reader'].signer).create(action_name='Consumer.create', action_version=1,
        intent_id='nx053-consumer-' + secrets.token_hex(8), type_name='Consumer', properties={})['object_id']
    with pytest.raises(ConversationDenied):
        f['registrar'].register_consumer_owner(consumer_id=consumer, principal_id=other, idempotency_key='nx053-owner-key-0001')
    assert admin.execute('select count(*) from control.nexloop_consumer_owners where principal_id=%s', (other,)).fetchone() == (0,)
    (request, payload), = audits(admin, tenant)
    assert request == 'conversation_owner' and payload == {'path': 'conversation_owner', 'world': 'real', 'code': 'NXK01'}
    assert not any(r in json.dumps(payload) for r in refs)
    # The existing ownership of the original principal is a replay, not a new contact.
    assert f['registrar'].register_consumer_owner(consumer_id=f['consumer'], principal_id=f['principal'], idempotency_key='synthetic-consumer-owner-key')['consumer_id'] == f['consumer']
    # Revoked (as the owner's Action leaves it): the new principal may be assigned.
    revoke_directly(admin, tenant)
    assert f['registrar'].register_consumer_owner(consumer_id=consumer, principal_id=other, idempotency_key='nx053-owner-key-0001')['consumer_id'] == consumer
    # A forged recording writes nothing: the references are re-derived and none is erased any more.
    assert len(audits(admin, tenant)) == 1


def private(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


@pytest.fixture
def workbench(commitments, identity, uow, admin, pg, tmp_path):
    c = commitments
    publish_registry_actions(admin, ('nexloop.contact_key.revoke',))
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator = add_human(admin, 'owner'), add_human(admin, 'operator')
    admin.execute('alter role nexloop_configurator login')
    members = members_file((owner, 'owner'), (operator, 'operator'))
    apply_members(ROLES, members, database_url_file=private(tmp_path, 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator')))
    write_facts(admin, compile_members(ROLES, members))
    signer = c['signer']
    (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(private(tmp_path, 'business-dsn', make_conninfo(pg, user='nexloop_api')), private(tmp_path, 'authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(private(tmp_path, 'identity-dsn', identity[0]), private(tmp_path, 'rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))

    def revoke(member, **body):
        with TestClient(create_app(config), base_url=ORIGIN) as client:
            csrf = workbench_login(client, member).json()['csrf_token']
            return act(client, csrf, 'revoke_contact_key', {'key_digest': None, 'erasure_request_id': None, **body})
    c.update(revoke=revoke, owner_member=owner, operator_member=operator)
    return c


@PLAN
def test_only_the_owner_revokes_with_a_reason_and_the_revocation_is_journaled_without_it(workbench, admin):
    c = workbench
    journal_before = admin.execute('select coalesce(max(seq),0) from control.nexloop_restore_journal').fetchone()[0]
    erase_refs(admin, TENANT, [REF, REF.replace('ab', 'ef')], request_id='erasure-req-7')
    digest = owner_sql(admin, "select encode(extensions.hmac(convert_to(%s,'UTF8'),salt,'sha256'),'hex') from control.nexloop_erasure_salts where tenant_id=%s", (REF, TENANT))
    assert c['revoke'](c['operator_member'], key_digest=digest, reason='客户重新授权').status_code == 403
    assert c['revoke'](c['owner_member'], key_digest=digest, reason='').status_code == 422  # a reason is required
    assert c['revoke'](c['owner_member'], key_digest=digest, erasure_request_id='erasure-req-7', reason='两者只能选一').status_code == 409
    assert c['revoke'](c['owner_member'], key_digest='0' * 64, reason='不存在').status_code == 409
    done = c['revoke'](c['owner_member'], key_digest=digest, reason='客户重新授权联系')
    assert done.status_code == 200 and done.json()['revoked'] == 1
    assert erased(admin, TENANT, REF) is False and erased(admin, TENANT, REF.replace('ab', 'ef')) is True  # only that key
    assert c['revoke'](c['owner_member'], erasure_request_id='erasure-req-7', reason='整次删除请求的键').json()['revoked'] == 1
    assert erased(admin, TENANT, REF.replace('ab', 'ef')) is False
    rows = keys(admin, TENANT)
    assert all(r[1] is True and r[2] == 2 for r in rows) and {r[3] for r in rows} == {'客户重新授权联系', '整次删除请求的键'}
    entries = [r[0] for r in admin.execute("select entry from control.nexloop_restore_journal where seq>%s and entry->>'subject'='erased_contact_key' order by seq", (journal_before,)).fetchall()]
    assert len(entries) == 4 and [e['revision'] for e in entries] == [1, 1, 2, 2] and all(e['revoked_at'] for e in entries[2:])
    assert not any('客户' in json.dumps(e, ensure_ascii=False) or REF in json.dumps(e) for e in entries)  # no reason, no reference
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_configurator'):
        assert admin.execute("select has_table_privilege(%s,'control.nexloop_erased_contact_keys','update')", (role,)).fetchone() == (False,)
        assert admin.execute("select has_function_privilege(%s,'control.nexloop_governed_revoke_contact_key(text,text,text,text,text,jsonb)','execute')", (role,)).fetchone() == (False,)


def test_replay_keeps_the_newest_state_of_each_key(catalog):
    admin, t = catalog, 'synthetic-tenant-a'
    erase_refs(admin, t, [REF])
    digest = keys(admin, t)[0][0]
    entry = lambda revision, revoked: {'subject': 'erased_contact_key', 'tenant_id': t, 'world': 'real', 'key_digest': digest, 'request_id': 'erasure-req-1',
                                       'created_at': '2026-10-11T00:00:00Z', 'revoked_at': '2026-10-11T01:00:00Z' if revoked else None,
                                       'revoked_by': 'owner-p' if revoked else None, 'revision': revision}
    started = admin.execute('select clock_timestamp()').fetchone()[0]
    owner_sql(admin, 'select runtime.nexloop_restore_begin(%s,%s,%s,%s,1)', ('nx053-replay', Jsonb({'backup_id': '20261011-010000F'}), started, started))
    base = admin.execute('select journal_from from control.nexloop_restore_state').fetchone()[0]
    # The restored database still has revision 1 (erased). The journal after it: revoked (2), erased again (3), and a stale copy of (2).
    journal = [{'seq': base + 1, 'kind': 'contact_restriction', 'entry': entry(2, True)}]
    out = owner_sql(admin, 'select runtime.nexloop_restore_replay(%s,%s)', ('nx053-replay', Jsonb(journal)))
    assert out['erased_contact_keys'] == 1 and erased(admin, t, REF) is False  # the revocation is not lost
    journal = [{'seq': base + 2, 'kind': 'contact_restriction', 'entry': entry(3, False)}, {'seq': base + 3, 'kind': 'contact_restriction', 'entry': entry(2, True)}]
    owner_sql(admin, 'select runtime.nexloop_restore_replay(%s,%s)', ('nx053-replay', Jsonb(journal)))
    assert erased(admin, t, REF) is True and keys(admin, t)[0][1:3] == (False, 3)  # the re-activation is not undone
    # A journal entry written before 0154 (no revision): the earlier rule, a revocation is never undone by replay.
    legacy = {k: v for k, v in entry(0, True).items() if k != 'revision'}
    owner_sql(admin, 'select runtime.nexloop_restore_replay(%s,%s)', ('nx053-replay', Jsonb([{'seq': base + 4, 'kind': 'contact_restriction', 'entry': legacy}])))
    assert erased(admin, t, REF) is False
    assert admin.execute('select journal_to from control.nexloop_restore_state').fetchone()[0] == base + 4
