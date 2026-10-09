"""Governed accepted-Message READ derivation (0073); synthetic disposable PG only.

The Source holds static Consumer READ plus an explicit message_read_rule. No
per-Message authority fact exists; every READ is rebuilt from governed rows in SQL.
"""
import json
import psycopg
import pytest
from nexloop_eios.message_read import derived_message_read_envelope, message_read_basis, _sign
from nexloop_eios.postgres_artifacts import canonical_payload
from test_context_artifacts import context_message, assembled_message, business_plan, configured  # noqa: F401


def parts(f):
    services = f['source']
    return services._backend._pool, services._session, services._backend._signer


def read(f, envelope, *, world='real'):
    pool, session, _ = parts(f)
    with pool.connection() as db, db.transaction():
        return db.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',
                          (session.token_digest, world, envelope['text'], envelope['signature'])).fetchone()[0]


def envelope(f):
    pool, session, signer = parts(f)
    basis = message_read_basis(pool, session, f['message']['id'])
    assert basis['mode'] == 'derived'
    return derived_message_read_envelope(pool, session, signer, f['message']['id'], basis)


def denied(f, value, **kwargs):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read(f, value, **kwargs)


def test_accepted_message_read_is_derived_without_per_message_authority(context_message, admin):
    f = context_message; tenant = f['original']['tenant']; mid = f['message']['id']
    principal = f['source']._session.authentication.subject_principal_id
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where tenant_id=%s and entity_key[2] like %s",
                         (tenant, '%Message/' + mid + '%')).fetchone() == (0,)
    revision = admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (tenant,)).fetchone()
    value = read(f, envelope(f))
    assert set(value['properties']) == {'actor', 'body'} and value['properties']['body'] == f['message']['body']
    assert f['source'].read_object(type_name='Message', object_id=mid, fields=('body',))['properties'] == {'body': f['message']['body']}
    # Context generation (0062 current Source READ) runs through the derived path.
    assert f['relay'].run_once() == 'queued'
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone() == (1,)
    # Accepting/reading Messages never advances the authority epoch.
    assert admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (tenant,)).fetchone() == revision
    claims = json.loads(envelope(f)['text'])
    assert claims['derivation'] == 'accepted-message-v1' and claims['facts'] == [] and claims['principal_id'] == principal


def test_other_tenant_message_is_denied(context_message, admin):
    f = context_message; value = envelope(f); mid = f['message']['id']
    # Disposable fault: the accepted rows now belong to another tenant.
    admin.execute("update ontology.objects set tenant_id='synthetic-other' where type_name='Message' and object_id=%s", (mid,))
    denied(f, value)


def test_other_world_is_denied(context_message):
    f = context_message; value = envelope(f)
    denied(f, value, world='shadow')
    pool, session, signer = parts(f)
    claims = json.loads(value['text']); claims['world'] = 'shadow'
    text = canonical_payload(claims)
    denied(f, {'text': text, 'signature': _sign(signer, text)})


def test_unaccepted_message_is_denied(context_message, admin):
    f = context_message; value = envelope(f)
    admin.execute('delete from runtime.nexloop_message_outbox where message_id=%s', (f['message']['id'],))
    denied(f, value)


def test_deleted_message_is_denied(context_message, admin):
    f = context_message; value = envelope(f)
    admin.execute("delete from ontology.objects where type_name='Message' and object_id=%s", (f['message']['id'],))
    denied(f, value)


@pytest.mark.parametrize('revocation', ['consumer_read', 'rule_inactive', 'rule_expired'])
def test_revoked_authority_is_denied(context_message, admin, revocation):
    """Revocation through the governed trusted-configuration publication path."""
    from nexloop_eios.message_relay import MessageRelayUnavailable
    from test_context_artifacts import reconfigure
    f = context_message; value = envelope(f)
    principal = f['source']._session.authentication.subject_principal_id
    consumer = f['f']['recipe']['consumer_id']

    def change(manifest):
        for row in manifest['authority_facts']:
            if revocation == 'consumer_read' and row['kind'] == 'grants' and row['key'] == [principal, 'eios:object:Consumer/' + consumer]:
                row['payload'].pop('snapshot_digest', None); row['payload']['grants'] = []
            if revocation != 'consumer_read' and row['kind'] == 'message_read_rule' and row['key'] == [principal]:
                row['payload'].update(active=False) if revocation == 'rule_inactive' else row['payload'].update(valid_until='2000-01-01T00:00:00Z')
    reconfigure(f, admin, change)
    denied(f, value)
    pool, session, signer = parts(f)
    if revocation == 'consumer_read':
        with pytest.raises(Exception):
            derived_message_read_envelope(pool, session, signer, f['message']['id'], message_read_basis(pool, session, f['message']['id']))
    else:
        assert message_read_basis(pool, session, f['message']['id']) == {'mode': 'configured'}
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone() == (0,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone() == (0,)


def test_conversation_moved_to_other_consumer_is_denied(context_message, admin):
    f = context_message; value = envelope(f)
    conversation = json.loads(value['text'])['derivation_basis']['conversation_id']
    admin.execute("update runtime.nexloop_conversations set consumer_id='f'||repeat('0',63) where conversation_id=%s", (conversation,))
    denied(f, value)


@pytest.mark.parametrize('tamper', ['other_field', 'facts', 'expiry_beyond_rule', 'other_consumer_read'])
def test_out_of_scope_claims_are_denied(context_message, tamper):
    f = context_message; value = envelope(f); pool, session, signer = parts(f)
    claims = json.loads(value['text'])
    if tamper == 'other_field':
        claims['fields'] = ['actor', 'body', 'conversation_id']
        extra = dict(claims['property_authorities'][0]); extra['resource_id'] = extra['target_resource'] = \
            'eios:property:Message/' + f['message']['id'] + '/conversation_id'
        claims['property_authorities'].append(extra)
    elif tamper == 'facts':
        claims['facts'] = [{'kind': 'grants', 'key': ['x'], 'record_hash': '0' * 64}]
    elif tamper == 'expiry_beyond_rule':
        claims['expires_at'] = '2999-01-01T00:00:00+00:00'
    else:
        claims['derivation_basis']['consumer_id'] = 'f' * 64
    text = canonical_payload(claims)
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.InvalidParameterValue)):
        read(f, {'text': text, 'signature': _sign(signer, text)})
    with pytest.raises(ValueError):
        derived_message_read_envelope(pool, session, signer, f['message']['id'], message_read_basis(pool, session, f['message']['id']),
                                      fields=('conversation_id',))


def test_run_credential_never_derives(context_message, admin):
    """A Run-bound credential keeps 0034 allowed_resources; derivation is Source-only."""
    f = context_message; pool, source, signer = parts(f); mid = f['message']['id']
    run = f['source'].issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
    runner = f['backend'].authenticate_run(run.token, world='real', run_id=run.run_id)
    run_session = runner._session
    assert run_session.run_context is not None
    with pool.connection() as db, db.transaction():
        basis = db.execute('select authz.nexloop_message_read_basis(%s,%s,%s)', (run_session.token_digest, 'real', mid)).fetchone()[0]
    assert basis == {'mode': 'configured'}
    # Even a correctly shaped, correctly signed derived claim for the Run identity is refused.
    source_basis = message_read_basis(pool, source, mid)
    claims = json.loads(derived_message_read_envelope(pool, source, signer, mid, source_basis)['text'])
    auth = run_session.authentication
    for item in [claims, *claims['property_authorities']]:
        item.update(principal_id=auth.subject_principal_id, credential_id=auth.credential_id, directory_hash=run_session.directory_hash)
    text = canonical_payload(claims)
    with pool.connection() as db, db.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select authz.nexloop_read_object(%s,%s,%s,%s)', (run_session.token_digest, 'real', text, _sign(signer, text)))
