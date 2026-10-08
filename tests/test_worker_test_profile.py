import json
from pathlib import Path
import pytest
from nexloop_eios.compose_bootstrap import initialize_test_profile
from nexloop_eios.worker_test_profile import initialize_worker_test_profile,check_worker_container
from test_compose_bootstrap import settings


def test_actual_worker_profile_and_job_use_independent_test_authority(admin,pg,tmp_path,monkeypatch):
    import nexloop_eios.worker_test_profile as module
    core=settings(pg,tmp_path);initialize_test_profile(admin,**core)
    root=tmp_path/'worker'
    args=dict(core_root=core['config_root'],worker_root=root,host=core['host'],port=core['port'],dbname=core['dbname'])
    result=initialize_worker_test_profile(admin,**args)
    assert result['passed'] and not result['business_action_grants']
    assert set(p.name for p in root.iterdir())=={'maintenance_dsn','artifact_key','service_credential','profile.json'}
    assert all(p.stat().st_mode&0o777==0o600 for p in root.iterdir())
    assert (root/'service_credential').read_bytes()!=(core['config_root']/'service_credential').read_bytes()
    assert admin.execute("select rolpassword like 'SCRAM-SHA-256$%' from pg_authid where rolname='nexloop_domain_worker'").fetchone()[0]
    real_path=Path
    monkeypatch.setattr(module,'Path',lambda value:{'/private/worker':root,'/var/lib/nexloop/artifacts':core['artifact_root']}.get(value,real_path(value)))
    report=check_worker_container()
    assert report['passed'] and all(report['checks'].values())
    assert admin.execute("select count(*) from runtime.nexloop_orphan_sweeps where status='finished'").fetchone()[0]==2
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0
    from nexloop_eios.backend import open_backend
    from nexloop_eios.compose_bootstrap import KEY_ID
    from eios.authz.errors import AuthorizationUnavailable
    with open_backend(database_url=(root/'maintenance_dsn').read_text(),artifact_root=core['artifact_root'],signing_key_file=root/'artifact_key',signing_key_id=KEY_ID) as backend:
        with pytest.raises(AuthorizationUnavailable):backend.authenticate((root/'service_credential').read_text(),world='real')
        service=backend.authenticate((root/'service_credential').read_text(),world='test')
        with pytest.raises(AuthorizationUnavailable):service.read_artifact('0'*32)
    old={p.name:p.read_bytes() for p in root.iterdir()}
    with pytest.raises(PermissionError):initialize_worker_test_profile(admin,**args)
    assert old=={p.name:p.read_bytes() for p in root.iterdir()}
    assert (root/'service_credential').read_text() not in json.dumps(report)


@pytest.mark.parametrize('mutation',['wrong-endpoint','foreign-root','signer-drift'])
def test_worker_profile_denies_foreign_inputs_before_authority_write(admin,pg,tmp_path,mutation):
    core=settings(pg,tmp_path);initialize_test_profile(admin,**core)
    root=tmp_path/'worker'
    args=dict(core_root=core['config_root'],worker_root=root,host=core['host'],port=core['port'],dbname=core['dbname'])
    if mutation=='wrong-endpoint':args['dbname']='foreign'
    elif mutation=='foreign-root':root.mkdir();(root/'existing').write_text('preserve')
    else:(core['config_root']/'artifact_key').write_bytes(b'x'*32)
    with pytest.raises(PermissionError):initialize_worker_test_profile(admin,**args)
    assert admin.execute("select count(*) from authz.nexloop_service_credentials where credential_id like '%maintenance%'").fetchone()[0]==0
