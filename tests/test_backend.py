from datetime import UTC, datetime, timedelta
import os
import secrets

import pytest
import psycopg
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.errors import AuthorizationUnavailable
from eios.adapters.postgres.database import StorageUnavailable
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend, BackendClosed
from nexloop_eios.postgres_artifacts import AuthoritySigner
from test_action_definitions import published_action


def signing_file(admin, tmp_path):
    material = secrets.token_bytes(32)
    key = tmp_path / 'synthetic-signing-key'
    key.write_bytes(material); key.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',
        ('synthetic-host', material))
    return key


def test_composed_backend_artifacts_reopen_and_closed_handles(admin, pg, tmp_path):
    bootstrap(admin)
    token, _ = seed_authority(admin, 'synthetic-a', 'eios:artifact:local_real',
        operations=(Operation.CREATE, Operation.READ, Operation.DELETE))
    key = signing_file(admin, tmp_path)
    config = dict(database_url=make_conninfo(pg, user='nexloop_api'),
        artifact_root=tmp_path/'blobs', signing_key_file=key, signing_key_id='synthetic-host')
    with open_backend(**config) as backend:
        services = backend.authenticate(token, world='real')
        ref = services.put_artifact(request_id='synthetic-host-artifact-001', payload=b'hello',
            media_type='text/plain', retention_until=datetime.now(UTC)+timedelta(days=1))
        assert services.read_artifact(ref.artifact_id, start=1, stop=4) == b'ell'
        with pytest.raises(AuthorizationUnavailable): backend.authenticate('bad-token', world='real')
        with backend._pool.connection() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute('select * from ontology.objects')
    assert backend._pool.closed and backend._store._root is None
    with pytest.raises(BackendClosed): services.read_artifact(ref.artifact_id)
    with pytest.raises(BackendClosed): backend.authenticate(token, world='real')
    with open_backend(**config) as reopened:
        assert reopened.authenticate(token, world='real').read_artifact(ref.artifact_id) == b'hello'


def test_composed_backend_uses_published_governed_create(published_action, admin, pg, tmp_path):
    reader, _, _ = published_action
    key = tmp_path/'synthetic-create-key'
    key.write_bytes(reader.signer.material); key.chmod(0o600)
    # Use the current synthetic service identity through normal authentication.
    token, _ = seed_authority(admin, 'synthetic-a', 'eios:action:Consumer.create:1',
        operation=Operation.EXECUTE, resource_type=ResourceType.ACTION, identity_suffix='-host')
    with open_backend(database_url=make_conninfo(pg,user='nexloop_api'), artifact_root=tmp_path/'blobs',
        signing_key_file=key, signing_key_id=reader.signer.key_id) as backend:
        service = backend.authenticate(token, world='real')
        args = dict(action_name='Consumer.create', action_version=1,
            intent_id='synthetic-host-create-001', type_name='Consumer', properties={})
        receipt = service.create_object(**args)
        assert service.create_object(**args) == receipt
        read_token, _ = seed_authority(admin, 'synthetic-a',
            'eios:object:Consumer/'+receipt['object_id'], operation=Operation.READ,
            resource_type=ResourceType.OBJECT, identity_suffix='-host-reader')
        result = backend.authenticate(read_token,world='real').read_object(
            type_name='Consumer',object_id=receipt['object_id'])
        assert result['object_id'] == receipt['object_id'] and result['properties'] == {}
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0] == 1


def test_admin_dsn_rejected_before_artifact_directory_creation(admin, pg, tmp_path):
    bootstrap(admin); key = signing_file(admin,tmp_path); root = tmp_path/'never-created'
    with pytest.raises(StorageUnavailable):
        with open_backend(database_url=pg, artifact_root=root, signing_key_file=key): pass
    assert not root.exists()


def test_invalid_key_rejected_before_artifact_directory_creation(tmp_path):
    key=tmp_path/'public-key'; key.write_bytes(secrets.token_bytes(32)); key.chmod(0o644)
    root=tmp_path/'never-created'
    with pytest.raises(PermissionError):
        with open_backend(database_url='not-connected', artifact_root=root, signing_key_file=key): pass
    assert not root.exists()


def test_signing_key_fifo_is_rejected_without_waiting_for_writer(tmp_path):
    fifo=tmp_path/'not-a-key'; os.mkfifo(fifo,0o600)
    with pytest.raises(PermissionError): AuthoritySigner.from_file(fifo)


@pytest.mark.parametrize('state',['mismatched','inactive','missing'])
def test_database_signer_must_match_before_artifact_creation(admin,pg,tmp_path,state):
    bootstrap(admin); key=signing_file(admin,tmp_path); root=tmp_path/'never-created'
    if state=='mismatched': key.write_bytes(secrets.token_bytes(32))
    elif state=='inactive': admin.execute('update authz.nexloop_authority_signing_keys set active=false')
    else: admin.execute('delete from authz.nexloop_authority_signing_keys')
    with pytest.raises(StorageUnavailable,match='backend authority signing key unavailable'):
        with open_backend(database_url=make_conninfo(pg,user='nexloop_api'),
            artifact_root=root,signing_key_file=key,signing_key_id='synthetic-host'): pass
    assert not root.exists()
    assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()[0]==0


def test_exceptional_backend_exit_closes_owned_resources(admin,pg,tmp_path):
    bootstrap(admin); key=signing_file(admin,tmp_path)
    with pytest.raises(RuntimeError,match='synthetic caller failure'):
        with open_backend(database_url=make_conninfo(pg,user='nexloop_api'),
            artifact_root=tmp_path/'blobs',signing_key_file=key,signing_key_id='synthetic-host') as backend:
            raise RuntimeError('synthetic caller failure')
    assert backend._pool.closed and backend._store._root is None
    with pytest.raises(BackendClosed): backend.authenticate('synthetic-token-unavailable',world='real')
