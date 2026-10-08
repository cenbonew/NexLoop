"""Synthetic loopback HTTP fault service, never a real delivery channel.

Its independent FULL SQLite ledger survives client/Worker death. Control flows
use a private multiprocessing Pipe; no production credential or DB is used.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import json
import multiprocessing
import re
import sqlite3
import threading
import uuid
from pathlib import Path


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def payload_digest(parameters):return hashlib.sha256(canonical(parameters)).hexdigest()


def _database(path):
    database=sqlite3.connect(path,timeout=5)
    database.execute('pragma journal_mode=wal');database.execute('pragma synchronous=FULL')
    assert database.execute('pragma synchronous').fetchone()[0]==2
    return database


def _serve(path,pipe):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with _database(path) as db:
        db.execute('create table if not exists effects(intent_id text primary key,payload_digest text not null,parameters text not null,state text not null,provider_reference text not null unique)')
        db.execute('create table if not exists requests(sequence integer primary key,method text not null,intent_id text not null,status integer not null)')
    path.chmod(0o600)
    paused=threading.Event();released=threading.Event();released.set();send_lock=threading.Lock()
    def send(value):
        with send_lock:pipe.send(value)
    def receipt(row):
        return dict(zip(('intent_id','payload_digest','state','provider_reference'),row))
    def read(db,intent):return db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?',(intent,)).fetchone()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,value):
            content=canonical(value);self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers()
            try:self.wfile.write(content)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_POST(self):
            try:
                size=int(self.headers.get('Content-Length','0'))
                if self.path!='/v1/effects' or not 0<size<=16384:raise ValueError()
                body=json.loads(self.rfile.read(size))
                if type(body) is not dict or set(body)!={'intent_id','payload_digest','parameters'}:raise ValueError()
                intent=body['intent_id'];digest=body['payload_digest'];parameters=body['parameters']
                if type(intent) is not str or not re.fullmatch('[A-Za-z0-9:_-]{1,128}',intent) or self.headers.get('Idempotency-Key')!=intent:raise ValueError()
                if type(parameters) is not dict or type(digest) is not str or digest!=payload_digest(parameters):raise ValueError()
                with _database(path) as db:
                    db.execute('begin immediate');row=read(db,intent);created=row is None
                    if created:
                        db.execute('insert into effects values(?,?,?,?,?)',(intent,digest,canonical(parameters).decode(),'accepted','synthetic:'+str(uuid.uuid4())))
                        row=read(db,intent)
                    status=202 if row[1]==digest else 409
                    db.execute('insert into requests(method,intent_id,status) values(?,?,?)',('POST',intent,status))
                # This barrier is after a real independent provider commit and
                # before the HTTP response; client death cannot undo the effect.
                if status==202 and created:
                    send({'event':'accepted_committed','intent_id':intent})
                    if paused.is_set():released.wait()
                self.respond(status,receipt(row))
            except (ValueError,TypeError,json.JSONDecodeError):self.respond(400,{'error':'synthetic_request_invalid'})
        def do_GET(self):
            prefix='/v1/effects/';intent=self.path[len(prefix):] if self.path.startswith(prefix) else ''
            if not re.fullmatch('[A-Za-z0-9:_-]{1,128}',intent):return self.respond(400,{'error':'synthetic_request_invalid'})
            with _database(path) as db:
                row=read(db,intent);status=200 if row else 404
                db.execute('insert into requests(method,intent_id,status) values(?,?,?)',('GET',intent,status))
            self.respond(status,receipt(row) if row else {'intent_id':intent,'payload_digest':None,'state':'not_found','provider_reference':None})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    send({'event':'ready','port':server.server_port})
    try:
        while True:
            command=pipe.recv();operation=command['operation']
            if operation=='pause':paused.set();released.clear();send({'event':'paused'})
            elif operation=='release':released.set();paused.clear();send({'event':'released'})
            elif operation=='fulfill':
                with _database(path) as db:db.execute("update effects set state='fulfilled' where intent_id=?",(command['intent_id'],))
                send({'event':'fulfilled'})
            elif operation=='snapshot':
                with _database(path) as db:
                    send({'event':'snapshot','effects':db.execute('select count(*) from effects').fetchone()[0],
                          'requests':db.execute('select method,intent_id,status from requests order by sequence').fetchall(),
                          'persistence':{'journal_mode':db.execute('pragma journal_mode').fetchone()[0],'synchronous':db.execute('pragma synchronous').fetchone()[0]}})
            elif operation=='stop':break
            else:raise ValueError('unsupported synthetic control')
    finally:
        released.set();server.shutdown();server.server_close();thread.join(5);pipe.close()


class SyntheticEffectProvider:
    def __init__(self,process,pipe):
        self.process,self.pipe,self.pending=process,pipe,[]
        self.origin='http://127.0.0.1:'+str(self.wait('ready')['port'])
    def wait(self,event,timeout=10):
        for index,value in enumerate(self.pending):
            if value['event']==event:return self.pending.pop(index)
        while self.pipe.poll(timeout):
            value=self.pipe.recv()
            if value['event']==event:return value
            self.pending.append(value)
        raise AssertionError('synthetic provider barrier unavailable: '+event)
    def control(self,operation,**kwargs):
        self.pipe.send({'operation':operation,**kwargs})
        return self.wait({'pause':'paused','release':'released','fulfill':'fulfilled','snapshot':'snapshot'}[operation])


@contextmanager
def effect_provider(path):
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_serve,args=(str(path),child));process.start();child.close()
    provider=SyntheticEffectProvider(process,parent)
    try:yield provider
    finally:
        if process.is_alive():parent.send({'operation':'stop'})
        process.join(10)
        if process.is_alive():process.kill();process.join(5)
        parent.close()
        assert process.exitcode==0,'synthetic provider did not stop cleanly'
