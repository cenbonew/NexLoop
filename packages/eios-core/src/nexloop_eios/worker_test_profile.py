"""Fresh disposable maintenance identity; no production grants or migration."""
from datetime import UTC,datetime,timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import psycopg
from eios.authz.operations import Operation
from nexloop_eios.bootstrap import verify,catalog
from nexloop_eios.compose_bootstrap import KEY_ID,_private_read,_write
from nexloop_eios.sandbox_profile import _artifact_test_records
from nexloop_eios.private_configuration import read_private_text


def initialize_worker_test_profile(connection,*,core_root,worker_root,host,port,dbname,service_uid=None):
    uid=os.geteuid() if service_uid is None else service_uid
    if type(uid) is not int or uid<0 or (uid!=os.geteuid() and os.geteuid()!=0):raise PermissionError('invalid worker owner')
    if connection.execute('select current_user,session_user,current_database()').fetchone()!=('nexloop_bootstrap','nexloop_bootstrap',dbname):raise PermissionError('fresh test bootstrap owner required')
    if (host,int(port),dbname)!=(connection.info.host,connection.info.port,connection.info.dbname):raise PermissionError('test endpoint mismatch')
    catalog=verify(connection)
    core=Path(core_root).absolute();root=Path(worker_root).absolute()
    if core.is_symlink() or root==core or root.is_relative_to(core) or core.is_relative_to(root) or root.is_symlink() or (root.exists() and any(root.iterdir())):raise PermissionError('independent empty private worker volume required')
    info=core.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or info.st_mode&0o077:raise PermissionError('private core owner required')
    profile=json.loads(_private_read(core/'profile.json',uid=uid,maximum=8192))
    if profile.get('mode')!='test' or profile.get('world')!='test' or profile.get('catalog')!=catalog or profile.get('service_uid')!=uid:raise PermissionError('fresh core test profile required')
    material=_private_read(core/'artifact_key',uid=uid,maximum=32)
    if hashlib.sha256(material).hexdigest()!=profile['sha256']['artifact_key']:raise PermissionError('test signer drift')
    key=connection.execute('select key_material from authz.nexloop_authority_signing_keys where key_id=%s and active',(KEY_ID,)).fetchone()
    if not key or bytes(key[0])!=material:raise PermissionError('active test signer required')
    tenant='nexloop-sandbox';world='test'
    binding,expiry,rows=_artifact_test_records(tenant,'eios:artifact:orphans_test',world,Operation.DELETE,identity_suffix='-maintenance')
    token=secrets.token_urlsafe(48);password=secrets.token_urlsafe(48)
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.stat().st_uid!=os.geteuid():raise PermissionError('fresh worker volume owner invalid')
    root.chmod(0o700)
    with connection.transaction():
        connection.execute("select pg_advisory_xact_lock(hashtextextended('nexloop-worker-test-init-v1',0))")
        if connection.execute('select count(*) from authz.nexloop_service_credentials where tenant_id=%s and credential_id=%s',(tenant,binding.credential_id)).fetchone()[0]:raise PermissionError('existing maintenance identity requires controlled renewal')
        connection.execute("set local log_statement='none'");connection.execute("set local log_min_error_statement='panic'")
        connection.execute("set local password_encryption='scram-sha-256'")
        connection.execute(sql.SQL('alter role {} password {}').format(sql.Identifier('nexloop_domain_worker'),sql.Literal(password)))
        connection.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',(hashlib.sha256(token.encode()).hexdigest(),tenant,binding.credential_id,Jsonb(binding.model_dump(mode='json')),[world],'nexloop-core','active',expiry))
        for kind,entity,fact in rows:
            payload=fact.model_dump(mode='json')
            old=connection.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',(tenant,kind,entity)).fetchone()
            if old:
                if old[0]!=payload:raise PermissionError('foreign authority fact collision')
            else:connection.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',(tenant,kind,entity,Jsonb(payload)))
        values={'maintenance_dsn':make_conninfo(host=host,port=port,dbname=dbname,user='nexloop_domain_worker',password=password),'service_credential':token,'artifact_key':material,
                'profile.json':json.dumps({'mode':'test','world':world,'tenant_id':tenant,'catalog':catalog,'service_uid':uid,'business_action_grants':False})}
        for name,value in values.items():_write(root/name,value,uid)
        if uid!=os.geteuid():os.chown(root,uid,-1)
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    return {'passed':True,'mode':'test','product_ready':False,'maintenance_role':'nexloop_domain_worker','business_action_grants':False}


def bootstrap_worker_container():
    if os.geteuid()!=0:raise PermissionError('privileged test bootstrap job required')
    path=Path('/run/secrets/bootstrap_dsn')
    dsn=_private_read(path,uid=path.stat().st_uid,maximum=16384).decode().strip()
    if not dsn or '\0' in dsn:raise ValueError('invalid explicit bootstrap secret')
    with psycopg.connect(dsn,autocommit=True) as connection:
        return initialize_worker_test_profile(connection,core_root='/private/config',worker_root='/private/worker',host='postgres',port=5432,dbname='nexloop_test',service_uid=10001)


def check_worker_container():
    """Explicit disposable physical orphan fixture, followed by actual governed work."""
    from nexloop_eios.backend import open_backend
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.local_artifacts import LocalBlobStore
    from nexloop_eios.domain_worker import run_page
    root=Path('/private/worker');artifacts=Path('/var/lib/nexloop/artifacts')
    profile=json.loads(read_private_text(root/'profile.json',maximum=8192))
    if profile!={'mode':'test','world':'test','tenant_id':'nexloop-sandbox','catalog':{'lineage':'nexloop-eios-v1','revision':catalog()[-1].version,'migration_count':len(catalog())},'service_uid':os.geteuid(),'business_action_grants':False}:raise PermissionError('explicit disposable worker profile required')
    with open_backend(database_url=read_private_text(root/'maintenance_dsn',maximum=16384),artifact_root=artifacts,signing_key_file=root/'artifact_key',signing_key_id=KEY_ID) as backend:
        with backend._pool.connection() as c:
            role=verify_application_role(c)
            if role!='nexloop_domain_worker':raise PermissionError('maintenance role required')
        service=backend.authenticate(read_private_text(root/'service_credential',maximum=2048),world='test')
        # Permission checked before this explicitly synthetic filesystem-only fixture.
        service.collect_final_artifact_orphans(older_than=datetime.now(UTC)-timedelta(seconds=120),limit=1)
        with LocalBlobStore(artifacts) as store:
            ref=store.put(tenant_id='nexloop-sandbox',world='test',payload=b'synthetic-community-worker-orphan',media_type='text/plain')
            path=store.root/ref.namespace/ref.artifact_id
            old=(datetime.now(UTC)-timedelta(seconds=180)).timestamp();os.utime(path,(old,old))
            report=run_page(service,older_than=datetime.now(UTC)-timedelta(seconds=120),limit=25)
            removed=any(row['artifact_id']==ref.artifact_id and row['disposition']=='removed' for row in report['outcomes']) and not path.exists()
            repeated=run_page(service,older_than=datetime.now(UTC)-timedelta(seconds=120),resume=report['sweep_id'])==report
        checks={'restricted_role':role=='nexloop_domain_worker','governed_orphan_removed':removed,'durable_resume_idempotent':repeated}
    return {'passed':all(checks.values()),'checks':checks,'mode':'test','world':'test','product_ready':False,'business_action_grants':False}
