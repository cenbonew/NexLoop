"""Fresh disposable Host control material; grants no business or Run authority."""
import http.client
import json
import os
from pathlib import Path
import ssl
import stat

from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.host_control import HostControlConfiguration,probe_host


def initialize_host_test_profile():
    if os.geteuid()!=0:raise PermissionError('privileged disposable Host initializer required')
    inputs={}
    for name in ['host-cert.pem','host-key.pem','host-control.key']:
        fd=os.open(Path('/run/secrets')/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno());raw=stream.read(32769)
        if not stat.S_ISREG(info.st_mode) or not raw or len(raw)>32768:raise PermissionError('bounded regular Host input required')
        inputs[name]=raw
    key=inputs['host-control.key']
    if len(key)!=64 or any(c not in b'0123456789abcdef' for c in key):raise PermissionError('private control key required')
    roots={Path('/private/host-server'):{'server.crt':inputs['host-cert.pem'],'server.key':inputs['host-key.pem'],'control.key':key},
           Path('/private/host-client'):{'ca.crt':inputs['host-cert.pem'],'control.key':key},Path('/private/runtime'):{}}
    for root in roots:
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid!=0 or any(root.iterdir()):raise PermissionError('fresh empty owned Host volumes required')
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
    return {'passed':True,'mode':'test','product_ready':False,'business_authority':False}


def test_control_configuration():
    root=Path('/private/host-client')
    return HostControlConfiguration('https://127.0.0.1:8100',root/'control.key',root/'ca.crt')


def probe_host_test():
    config=test_control_configuration();report=probe_host(config)
    checks={'reachable':report['reachable'],'authenticated':report['authenticated'],'owner_lock':report['owner_lock'],
            'product_unready':report['product_ready'] is False}
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=read_private_text(config.ca_file,maximum=32768))
    key=read_private_text(config.key_file,maximum=64)
    def request(headers):
        connection=http.client.HTTPSConnection('127.0.0.1',8100,context=context,timeout=3)
        try:
            connection.request('GET','/internal/v1/health/ready',headers=headers)
            response=connection.getresponse();response.read(4097);return response.status
        finally:connection.close()
    checks['anonymous_denied']=request({})==401
    checks['wrong_key_denied']=request({'Authorization':'Bearer '+'0'*64})==401
    checks['browser_origin_denied']=request({'Authorization':'Bearer '+key,'Origin':'https://localhost'})==401
    return {'passed':all(checks.values()),'mode':'test','product_ready':False,'business_authority':False,'checks':checks}
