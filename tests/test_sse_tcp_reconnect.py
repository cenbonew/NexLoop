"""AT-015 actual independent API process/TLS disconnect and PG replay."""
import json,secrets,socket,ssl,time,os
import httpx
from pathlib import Path
from psycopg.conninfo import make_conninfo
from local_message_assembly_fixture import assembled_message,business_plan,configured
from test_precise_pg_admission_outage import process,wait_live
from test_trusted_configuration_pg import private
from test_agent_host import files,free_port


def exact(stream,length):
    data=b''
    while len(data)<length:
        chunk=stream.recv(length-len(data))
        if not chunk:raise AssertionError('owned TLS connection closed before complete frame')
        data+=chunk
    return data


def connect_stream(port,certificate,cookie,path,after):
    stream=ssl.create_default_context(cafile=str(certificate)).wrap_socket(socket.create_connection(('127.0.0.1',port),timeout=5),server_hostname='127.0.0.1')
    stream.settimeout(5)
    stream.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\nLast-Event-ID: {after}\r\nConnection: close\r\n\r\n').encode())
    head=b''
    while not head.endswith(b'\r\n\r\n'):
        assert len(head)<16384,'HTTP headers exceed bounded parser limit'
        head+=exact(stream,1)
    assert head.startswith(b'HTTP/1.1 200') and b'text/event-stream' in head
    return stream


def event(stream):
    # uvicorn HTTP/1.1 chunk framing; read bytewise to leave subsequent frames
    # unread and close a genuinely open independent TCP/TLS transport.
    line=b''
    while not line.endswith(b'\r\n'):
        assert len(line)<128,'chunk length line exceeds bounded parser limit'
        line+=exact(stream,1)
    length=int(line.strip().split(b';')[0],16)
    assert 0<length<=65536
    payload=exact(stream,length)
    assert exact(stream,2)==b'\r\n'
    data=[x[6:] for x in payload.decode().splitlines() if x.startswith('data: ')]
    return json.loads(data[0]) if data else None


def test_actual_tls_socket_disconnect_last_event_id_replays_all_committed(assembled_message,admin,tmp_path):
    f=assembled_message;o=f['original'];p=o['paths'];port=free_port();origin=f'https://127.0.0.1:{port}'
    dsn=private(tmp_path,'api-dsn',make_conninfo(o['pg'],user='nexloop_api'));rate=private(tmp_path,'rate-key',secrets.token_hex(32))
    tls=tmp_path/'tls';tls.mkdir(mode=0o700);files(tls);cert=tls/'host-cert.pem'
    args=['--database-url-file',str(dsn),'--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration','--artifact-root',str(tmp_path/'artifacts'),'--mode','test','--execution-profile','deterministic-test','--port',str(port),'--tls-certificate-file',str(cert),'--tls-key-file',str(tls/'host-key.pem'),'--identity-database-url-file',str(p['identity']),'--browser-rate-key-file',str(rate),'--browser-tenant-id',o['tenant'],'--browser-application-id',o['application'],'--browser-origin',origin]
    with process('nexloop_eios.http_api',args,tuple(f['tokens'].values())+(p['password'].read_text(),)) as child:
        with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(cert)),trust_env=False,timeout=5) as client:
            wait_live(child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()});assert login.status_code==200
            headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'tcp-conversation-unique-key'}
            created=client.post('/api/v1/conversations',headers=headers,json={});assert created.status_code==200
            cid=created.json()['id'];base='/api/v1/conversations/'+cid
            receipts=[]
            def accept(index):
                r=client.post(base+'/messages',headers={**headers,'Idempotency-Key':f'tcp-sse-message-stable-{index}'},json={'body':f'committed TCP replay {index}'})
                assert r.status_code==202;receipts.append(r.json()['message'])
            accept(1);accept(2)
            cookie='; '.join(f'{k}={v}' for k,v in client.cookies.items())
            first=connect_stream(port,cert,cookie,base+'/events','0')
            try:
                original=event(first);assert original['id']=='1' and original['data']==receipts[0]
            finally:first.close()
            # Original socket closed while server's 180s stream is active.
            accept(3);accept(4)
            second=connect_stream(port,cert,cookie,base+'/events',original['id'])
            try:
                resumed=[event(second) for unused in range(3)]
                assert [x['id'] for x in resumed]==['2','3','4']
                assert [x['data'] for x in resumed]==receipts[1:]
            finally:second.close()
            third=connect_stream(port,cert,cookie,base+'/events','4')
            try:assert event(third) is None  # heartbeat: no committed duplicate
            finally:third.close()
            read=client.get(base+'/messages');assert read.status_code==200 and read.json()['items']==receipts
            assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone()==(4,)
            output=tmp_path/'actual-sse-frames.json'
            supplied=os.environ.get('NEXLOOP_OWNED_SSE_EVIDENCE_FILE')
            if supplied:
                output=Path(supplied);assert output.is_absolute() and output.parent.is_dir()
            output.write_text(json.dumps({'conversation_id':cid,'first':[original],'resumed':resumed,'messages':receipts},indent=2)+'\n')
