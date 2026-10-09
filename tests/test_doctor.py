from support.release_metadata import BOOTSTRAP_REVISION
import json
from psycopg.conninfo import make_conninfo
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.doctor import diagnose


def test_readonly_doctor_restricted_pg_and_existing_artifact(admin,pg,tmp_path):
    bootstrap(admin);root=tmp_path/'artifacts';root.mkdir(mode=0o700)
    report=diagnose(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=root,environment={})
    assert report['foundation_checks_passed'] and not report['product_ready']
    assert report['details']['database_catalog']['revision']==BOOTSTRAP_REVISION
    assert report['checks']['search_extensions'] and set(report['details']['database_extensions'])=={'vector','pg_trgm'}
    assert tuple(int(p) for p in report['details']['database_extensions']['vector'].split('.')[:2])>=(0,8)
    assert not list(root.iterdir())
    assert report['details']['model']['provider']=='test'


def test_doctor_rejects_admin_and_does_not_create_artifact_directory(admin,pg,tmp_path):
    bootstrap(admin);root=tmp_path/'absent'
    report=diagnose(database_url=pg,artifact_root=root,environment={})
    assert not report['checks']['postgres'] and not report['checks']['artifact_directory']
    assert not report['foundation_checks_passed'] and not root.exists()
    assert pg not in json.dumps(report)


def test_doctor_reports_missing_search_extension(admin,pg,tmp_path):
    bootstrap(admin);root=tmp_path/'artifacts';root.mkdir(mode=0o700)
    # Synthetic fault on a disposable cluster: a PostgreSQL image without pgvector.
    admin.execute('drop extension vector cascade')
    report=diagnose(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=root,environment={})
    assert not report['checks']['search_extensions'] and not report['foundation_checks_passed']
    assert report['details']['database_extensions']=={'pg_trgm':report['details']['database_extensions']['pg_trgm']}


def test_doctor_rejects_public_artifact_root_without_changing_mode(admin,pg,tmp_path):
    bootstrap(admin);root=tmp_path/'artifacts';root.mkdir();root.chmod(0o755)
    report=diagnose(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=root,environment={})
    assert not report['checks']['artifact_directory']
    assert root.stat().st_mode&0o777==0o755


def test_missing_dsn_never_uses_default_local_database(monkeypatch,tmp_path):
    import nexloop_eios.doctor as doctor
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(True)
        raise AssertionError('must not connect')
    monkeypatch.setattr(doctor.psycopg,'connect',forbidden)
    root=tmp_path/'private';root.mkdir(mode=0o700)
    assert not diagnose(database_url='',artifact_root=root,environment={})['checks']['postgres']
    assert calls==[]


def test_doctor_cli_private_dsn_wins_and_explicit_test_mode_ignores_model_credentials(admin,pg,tmp_path):
    import os,subprocess,sys
    bootstrap(admin);root=tmp_path/'artifacts';root.mkdir(mode=0o700)
    path=tmp_path/'dsn';path.write_text(make_conninfo(pg,user='nexloop_api'));path.chmod(0o600)
    environment=dict(os.environ,NEXLOOP_DATABASE_URL=pg,DATABASE_URL_FILE=str(tmp_path/'wrong-file'),
        MODEL_PROVIDER='invalid-provider',MODEL_API_KEY='synthetic-doctor-unused-secret')
    completed=subprocess.run([sys.executable,'-m','nexloop_eios.doctor','--artifact-root',str(root),
        '--database-url-file',str(path),'--mode','test'],env=environment,capture_output=True,text=True,timeout=15)
    assert completed.returncode==0, 'private-file doctor failed'
    report=json.loads(completed.stdout)
    assert report['foundation_checks_passed'] and not report['product_ready']
    assert report['details']['database_role']=='nexloop_api'
    assert report['details']['database_configuration_source']=='private_file'
    assert report['details']['model']['provider']=='test'
    assert not list(root.iterdir())
    assert pg not in completed.stdout+completed.stderr
    assert 'synthetic-doctor-unused-secret' not in completed.stdout+completed.stderr


def test_invalid_doctor_file_never_falls_back_to_environment(monkeypatch,tmp_path,capsys):
    import sys
    import nexloop_eios.doctor as doctor
    root=tmp_path/'private';root.mkdir(mode=0o700)
    monkeypatch.setenv('NEXLOOP_DATABASE_URL','synthetic-do-not-connect')
    monkeypatch.setenv('DATABASE_URL_FILE',str(tmp_path/'missing'))
    monkeypatch.setattr(sys,'argv',['nexloop-doctor','--artifact-root',str(root),'--mode','test'])
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(True)
        raise AssertionError('must not connect on invalid explicit DSN file')
    monkeypatch.setattr(doctor.psycopg,'connect',forbidden)
    assert doctor.main()==1
    assert calls==[]
    text=capsys.readouterr().out;report=json.loads(text)
    assert not report['checks']['database_configuration'] and not report['checks']['postgres']
    assert report['details']['database_configuration_source']=='private_file'
    assert 'synthetic-do-not-connect' not in text and str(tmp_path) not in text
