"""Explicit disposable TLS/ACL cache test; never an authoritative fact store."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import time

from nexloop_eios.private_configuration import read_private_text

INPUTS=('cache-cert.pem','cache-key.pem','cache.acl','cache-credentials.json')
SERVER_CONFIG='''bind 0.0.0.0
protected-mode yes
port 0
tls-port 6379
tls-cert-file /private/cache-server/server.crt
tls-key-file /private/cache-server/server.key
tls-ca-cert-file /private/cache-server/server.crt
tls-auth-clients no
aclfile /private/cache-server/users.acl
save ""
appendonly no
maxmemory 64mb
maxmemory-policy allkeys-lru
loglevel warning
logfile ""
'''


def initialize_cache_test_profile():
    if os.geteuid()!=0:raise PermissionError('privileged disposable cache initializer required')
    inputs={}
    for name in INPUTS:
        path=Path('/run/secrets')/name
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno());raw=stream.read(32769)
        if not stat.S_ISREG(info.st_mode) or not raw or len(raw)>32768:
            raise PermissionError('bounded regular cache input required')
        inputs[name]=raw
    credentials=json.loads(inputs['cache-credentials.json'])
    password=credentials.get('password','')
    if set(credentials)!={'username','password'} or credentials['username']!='nexloop_cache_test' or len(password)!=64 or any(c not in '0123456789abcdef' for c in password):
        raise PermissionError('explicit test cache credentials required')
    acl=('user default off\nuser nexloop_cache_test on #'+hashlib.sha256(password.encode()).hexdigest()+' resetkeys ~nexloop:test:cache:* resetchannels -@all +ping +get +set +del\n').encode()
    if inputs['cache.acl']!=acl:raise PermissionError('least-privilege cache ACL required')
    roots={Path('/private/cache-server'):{'server.crt':inputs['cache-cert.pem'],'server.key':inputs['cache-key.pem'],'users.acl':acl,'valkey.conf':SERVER_CONFIG.encode()},
           Path('/private/cache-client'):{'ca.crt':inputs['cache-cert.pem'],'credentials.json':inputs['cache-credentials.json']}}
    for root in roots:
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid!=0 or any(root.iterdir()):
            raise PermissionError('fresh empty owned cache volumes required')
    for root,files in roots.items():
        root.chmod(0o700)
        for name,raw in files.items():
            fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:
                os.fchown(stream.fileno(),10001,-1);stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chown(root,10001,-1)
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    return {'passed':True,'mode':'test','product_ready':False,'cache_authoritative':False}


def _context(ca=None):
    # Explicit context preserves verification without SSLKEYLOGFILE inheritance.
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca is not None:context.load_verify_locations(cadata=ca)
    return context


def _connect(context,*,server_name='valkey'):
    raw=socket.create_connection(('valkey',6379),timeout=3)
    try:return context.wrap_socket(raw,server_hostname=server_name)
    except BaseException:raw.close();raise


def _command(connection,*args):
    encoded=[arg.encode() for arg in args]
    request=b'*'+str(len(encoded)).encode()+b'\r\n'+b''.join(b'$'+str(len(arg)).encode()+b'\r\n'+arg+b'\r\n' for arg in encoded)
    connection.sendall(request)
    # Only small RESP2 simple/error/bulk replies needed by this diagnostic.
    with connection.makefile('rb',buffering=0) as stream:
        line=stream.readline(4097)
        if len(line)>4096 or not line.endswith(b'\r\n'):raise ValueError('invalid bounded cache reply')
        if line[:1] in (b'+',b'-'):return line[:1],line[1:-2]
        if line[:1]!=b'$':raise ValueError('unsupported cache reply')
        length=int(line[1:-2])
        if length==-1:return b'$',None
        if not 0<=length<=4096:raise ValueError('cache payload too large')
        payload=b''
        while len(payload)<length+2:
            chunk=stream.read(length+2-len(payload))
            if not chunk:raise ValueError('truncated cache payload')
            payload+=chunk
        if payload[-2:]!=b'\r\n':raise ValueError('invalid cache payload trailer')
        return b'$',payload[:-2]


def probe_cache_test():
    root=Path('/private/cache-client')
    ca=read_private_text(root/'ca.crt',maximum=32768)
    credentials=json.loads(read_private_text(root/'credentials.json',maximum=512))
    if set(credentials)!={'username','password'} or credentials['username']!='nexloop_cache_test':raise PermissionError('test-only cache user required')
    context=_context(ca);deadline=time.monotonic()+15
    while True:
        try:connection=_connect(context);break
        except (ConnectionRefusedError,socket.timeout):
            if time.monotonic()>=deadline:raise
            time.sleep(.1)
    checks={};key='nexloop:test:cache:probe:'+secrets.token_hex(16)
    with connection:
        denial=_command(connection,'PING');checks['default_user_denied']=denial[0]==b'-' and denial[1].startswith(b'NOAUTH')
        checks['authenticated']=_command(connection,'AUTH',credentials['username'],credentials['password'])==(b'+',b'OK')
        checks['ping']=_command(connection,'PING')==(b'+',b'PONG')
        checks['cache_write']=_command(connection,'SET',key,'disposable-probe','PX','30000')==(b'+',b'OK')
        checks['cache_read']=_command(connection,'GET',key)==(b'$',b'disposable-probe')
        denial=_command(connection,'GET','formal-business-fact');checks['foreign_key_denied']=denial[0]==b'-' and denial[1].startswith(b'NOPERM')
        denial=_command(connection,'CONFIG','GET','*');checks['admin_command_denied']=denial[0]==b'-' and denial[1].startswith(b'NOPERM')
        # DEL reply is integer, deliberately use expiring probe rather than broaden decoder.
    with _connect(context) as connection:
        denial=_command(connection,'AUTH',credentials['username'],'incorrect-synthetic-password');checks['wrong_password_denied']=denial[0]==b'-' and denial[1].startswith(b'WRONGPASS')
    for name,ctx,server_name in [('unknown_ca_denied',_context(),'valkey'),('wrong_hostname_denied',context,'localhost')]:
        try:
            with _connect(ctx,server_name=server_name):checks[name]=False
        except ssl.SSLCertVerificationError:checks[name]=True
    return {'passed':all(checks.values()),'mode':'test','product_ready':False,'cache_authoritative':False,'checks':checks}
