"""Fresh test-profile initialization; existing databases are verified, never migrated."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo,conninfo_to_dict

from nexloop_eios.bootstrap import bootstrap,verify
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.sandbox_profile import publish_test_artifact_identity

KEY_ID='compose-test-v1'
FILES={'api_dsn','service_credential','artifact_key','profile.json'}


def _private_read(path,*,uid,maximum):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_mode&0o077:
            raise PermissionError('test profile files must remain private and service-owned')
        value=stream.read(maximum+1)
    if not value or len(value)>maximum:raise ValueError('invalid test profile file')
    return value


def _write(path,value,uid):
    raw=value if type(value) is bytes else value.encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        if uid!=os.geteuid():os.fchown(stream.fileno(),uid,-1)
        stream.write(raw);stream.flush();os.fsync(stream.fileno())


def _verify_profile(connection,config,artifacts,uid):
    if set(p.name for p in config.iterdir())!=FILES:
        raise PermissionError('incomplete or foreign test configuration')
    profile=json.loads(_private_read(config/'profile.json',uid=uid,maximum=8192))
    if profile['mode']!='test' or profile['world']!='test' or profile['catalog']!=verify(connection) or profile['service_uid']!=uid:
        raise PermissionError('existing test profile is incompatible')
    values={name:_private_read(config/name,uid=uid,maximum=8192 if name=='api_dsn' else 512 if name=='service_credential' else 32) for name in ('api_dsn','service_credential','artifact_key')}
    if any(hashlib.sha256(value).hexdigest()!=profile['sha256'][name] for name,value in values.items()):
        raise PermissionError('existing test profile was changed')
    dsn=conninfo_to_dict(values['api_dsn'].decode())
    if dsn['user']!='nexloop_api' or not dsn.get('password') or (dsn.get('host'),int(dsn.get('port',0)),dsn.get('dbname'))!=(connection.info.host,connection.info.port,connection.info.dbname):
        raise PermissionError('restricted test DSN unavailable')
    credential=connection.execute("select count(*) from authz.nexloop_service_credentials where token_digest=%s and tenant_id='nexloop-sandbox' and worlds=array['test'] and status='active' and expires_at>clock_timestamp()",(hashlib.sha256(values['service_credential']).hexdigest(),)).fetchone()[0]
    key=connection.execute("select encode(sha256(key_material),'hex') from authz.nexloop_authority_signing_keys where key_id=%s and active",(KEY_ID,)).fetchone()
    if credential!=1 or not key or key[0]!=hashlib.sha256(values['artifact_key']).hexdigest():
        raise PermissionError('test credential or signer requires controlled renewal')
    for root in (config,artifacts):
        info=root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or info.st_mode&0o077:
            raise PermissionError('test volumes must remain private and service-owned')
    return {'passed':True,'mode':'test','world':'test','catalog':profile['catalog'],'already_initialized':True,'product_ready':False}


def initialize_test_profile(connection,*,config_root,artifact_root,host,port,dbname,service_uid=None):
    uid=os.geteuid() if service_uid is None else service_uid
    if type(uid) is not int or uid<0 or (uid!=os.geteuid() and os.geteuid()!=0):
        raise PermissionError('test service owner invalid')
    identity=connection.execute('select current_user,session_user,current_database()').fetchone()
    if identity!=('nexloop_bootstrap','nexloop_bootstrap',dbname):
        raise PermissionError('test bootstrap requires exact owner session/database')
    if (host,int(port),dbname)!=(connection.info.host,connection.info.port,connection.info.dbname):
        raise PermissionError('test connection endpoints must match')
    config=Path(config_root).absolute();artifacts=Path(artifact_root).absolute()
    if config==artifacts or config.is_symlink() or artifacts.is_symlink():
        raise PermissionError('independent private test volumes required')
    present=connection.execute("select to_regclass('control.schema_migrations')").fetchone()[0]
    if present:
        verify(connection)  # Exact head only: no existing/persistent migration, even in test mode.
        return _verify_profile(connection,config,artifacts,uid)
    existing=connection.execute("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p','v','m','S') and n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_toast%'").fetchone()[0]
    if existing or any(root.exists() and any(root.iterdir()) for root in (config,artifacts)):
        raise PermissionError('fresh empty database and test volumes required')
    for root in (config,artifacts):
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if root.stat().st_uid!=os.geteuid():raise PermissionError('new test volume owner invalid')
        root.chmod(0o700)
    fd=os.open(config,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        if any(config.iterdir()):raise PermissionError('test configuration already initialized')
        with connection.transaction():
            connection.execute("select pg_advisory_xact_lock(hashtextextended('nexloop-compose-test-init-v1',0))")
            # Suppress literal/password statement/error logging in this privileged session.
            connection.execute("set local log_statement='none'")
            connection.execute("set local log_min_error_statement='panic'")
            connection.execute("set local password_encryption='scram-sha-256'")
            bootstrap(connection);catalog=verify(connection)
            token=publish_test_artifact_identity(connection)
            password=secrets.token_urlsafe(48);material=secrets.token_bytes(32)
            connection.execute(sql.SQL('alter role {} password {}').format(sql.Identifier('nexloop_api'),sql.Literal(password)))
            connection.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(KEY_ID,material))
            dsn=make_conninfo(host=host,port=port,dbname=dbname,user='nexloop_api',password=password)
            values={'api_dsn':dsn.encode(),'service_credential':token.encode(),'artifact_key':material}
            profile={'mode':'test','world':'test','catalog':catalog,'service_uid':uid,'sha256':{name:hashlib.sha256(value).hexdigest() for name,value in values.items()}}
            for name,value in values.items():_write(config/name,value,uid)
            _write(config/'profile.json',json.dumps(profile),uid)
            for root in (config,artifacts):
                if uid!=os.geteuid():os.chown(root,uid,-1)
                directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(directory)
                finally:os.close(directory)
        return {'passed':True,'mode':'test','world':'test','catalog':catalog,'already_initialized':False,'product_ready':False}
    finally:os.close(fd)


def main():
    parser=argparse.ArgumentParser(description='Initialize only a fresh empty core test database/private volumes')
    parser.add_argument('--mode',required=True,choices=['test'])
    parser.add_argument('--database-url-file',required=True,type=Path)
    parser.add_argument('--config-root',required=True,type=Path)
    parser.add_argument('--artifact-root',required=True,type=Path)
    parser.add_argument('--postgres-host',required=True)
    parser.add_argument('--postgres-port',required=True,type=int)
    parser.add_argument('--postgres-db',required=True)
    parser.add_argument('--service-uid',type=int)
    args=parser.parse_args()
    try:
        dsn=read_private_text(args.database_url_file,maximum=8192)
        with psycopg.connect(dsn,autocommit=True) as connection:
            result=initialize_test_profile(connection,config_root=args.config_root,artifact_root=args.artifact_root,
                host=args.postgres_host,port=args.postgres_port,dbname=args.postgres_db,service_uid=args.service_uid)
    except Exception:
        result={'passed':False,'mode':'test','world':'test','product_ready':False,'error':'test initialization refused or unavailable'}
    print(json.dumps(result,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
