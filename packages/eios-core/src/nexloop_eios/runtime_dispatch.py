"""Independent restricted queue Worker; Host has no PG or Run bearer secret.

Queue completion describes only the observed durable Pi runtime outcome. It
never asserts business Action success. An unknown transport result is recovered
by the same Run/request, and missing storage is never recreated after model/tool execution authorization.

total_timeout bounds the dispatch loop budget; each HTTP call has an absolute
wall-clock deadline. PG operations retain the restricted pool statement/lock
timeouts and may extend run_once beyond its loop budget; final PG clock/fence
checks remain authoritative. This module does not claim whole-function timing.
"""
from datetime import UTC,datetime
import http.client
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from nexloop_eios.durable_queue import StaleQueueLease
from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.runtime_activation import _command,_input_digest


def _receipt_id(value):
    if type(value) is int:return 1<=value<=9007199254740991
    return type(value) is str and re.fullmatch('[1-9][0-9]{0,18}',value) is not None and int(value)<=9223372036854775807


class RuntimeDispatchError(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


class RuntimeHostClient:
    """Fixed HTTPS loopback routes, private key/CA, bounded absolute request time.

    http.client uses no proxy configuration and never follows redirects. The
    timer shuts down the socket so trickled headers/body cannot reset a deadline.
    """
    def __init__(self,configuration):
        if not isinstance(configuration,HostControlConfiguration):raise ValueError('Host configuration required')
        self.configuration=configuration

    def request(self,operation,body,*,deadline):
        if operation not in {'start','resume','inspect','cancel'}:raise ValueError('Host operation required')
        connection=None;timer=None
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeDispatchError('runtime_transport_timeout')
            key=read_private_text(self.configuration.key_file,maximum=64)
            if re.fullmatch('[0-9a-f]{64}',key) is None:raise ValueError()
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_verify_locations(cadata=read_private_text(self.configuration.ca_file,maximum=32768))
            payload=json.dumps(body,separators=(',',':')).encode()
            if len(payload)>262144:raise ValueError()
            origin=urlsplit(self.configuration.origin)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeDispatchError('runtime_transport_timeout')
            connection=http.client.HTTPSConnection('127.0.0.1',origin.port,timeout=remaining,context=context)
            connection.connect()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeDispatchError('runtime_transport_timeout')
            connection.sock.settimeout(remaining)
            def expire():
                if connection.sock is not None:
                    try:connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                connection.close()
            timer=threading.Timer(max(0,deadline-time.monotonic()),expire);timer.daemon=True;timer.start()
            connection.request('POST','/internal/v1/runs/'+operation,body=payload,
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Content-Length':str(len(payload))})
            response=connection.getresponse()
            if response.getheader('Content-Type','').split(';')[0]!='application/json':raise ValueError()
            headers=response.getheaders()
            if any(name.lower()=='content-encoding' for name,value in headers):raise ValueError()
            if sum(name.lower()=='content-length' for name,value in headers)>1 or sum(name.lower()=='transfer-encoding' for name,value in headers)>1:raise ValueError()
            length=response.getheader('Content-Length');encoding=response.getheader('Transfer-Encoding')
            if encoding is not None and (encoding.lower()!='chunked' or length is not None):raise ValueError()
            if length is not None and (not length.isdecimal() or not 1<=int(length)<=65536):raise ValueError()
            if length is None and encoding is None:raise ValueError()
            raw=response.read(65537)
            if len(raw)>65536:raise ValueError()
            if time.monotonic()>=deadline:raise RuntimeDispatchError('runtime_transport_timeout')
            if length is not None and len(raw)!=int(length):raise ValueError()
            value=json.loads(raw)
            if type(value) is not dict:raise ValueError()
            if response.status not in ({202} if operation in {'start','resume'} else {200}):
                if response.status==503 and value.get('code')=='runtime_state_missing':raise RuntimeDispatchError('runtime_state_missing')
                if response.status==409 and value.get('code')=='runtime_request_conflict':raise RuntimeDispatchError('runtime_request_conflict')
                raise RuntimeDispatchError('runtime_transport_unavailable')
            command=body['command']
            if value.get('run_id')!=command['run_id']:raise ValueError()
            if operation!='cancel' and (value.get('request_id')!=command['request_id']
                or any(not _receipt_id(value.get(key)) for key in ('conversation_id','submission_id'))):raise ValueError()
            if operation=='inspect':
                if value.get('runtime_outcome') not in {'running','succeeded','failed','cancelled','unknown'}:raise ValueError()
                persistence=value.get('persistence')
                if (type(persistence) is not dict or set(persistence)!={'journal_mode','synchronous'}
                    or persistence['journal_mode']!='wal' or type(persistence['synchronous']) is not int or persistence['synchronous']!=2):raise ValueError()
            return value
        except RuntimeDispatchError:raise
        except Exception:raise RuntimeDispatchError('runtime_transport_unavailable') from None
        finally:
            if timer is not None:timer.cancel()
            if connection is not None:connection.close()


class RuntimeDispatcher:
    def __init__(self,worker,configuration,*,queue,lease_seconds=30,total_timeout=30,request_timeout=2,poll_seconds=.05,cache_wakeup=None):
        if (type(lease_seconds) is not int or not 3<=lease_seconds<=300
            or not isinstance(total_timeout,(int,float)) or not 0<total_timeout<=300
            or not isinstance(request_timeout,(int,float)) or not 0<request_timeout<=lease_seconds/3
            or not isinstance(poll_seconds,(int,float)) or not 0<poll_seconds<=1):raise ValueError('bounded dispatch configuration required')
        if not isinstance(queue,str) or re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',queue) is None:raise ValueError('queue required')
        if cache_wakeup is not None and cache_wakeup.config.timeout>min(lease_seconds,total_timeout)/6:
            raise ValueError('cache deadline exceeds dispatch lease budget')
        self.worker,self.queue=worker,queue
        self.cache_wakeup=cache_wakeup
        self.client=RuntimeHostClient(configuration)
        self.lease_seconds,self.total_timeout,self.request_timeout,self.poll_seconds=lease_seconds,total_timeout,request_timeout,poll_seconds

    def run_once(self):
        started=time.monotonic();deadline=started+self.total_timeout
        cache_queue=None
        if self.cache_wakeup is not None:
            from nexloop_eios.durable_queue import PostgresDurableQueue
            # Actual authenticated server context; no client-supplied identities.
            cache_queue=PostgresDurableQueue(self.worker._backend._pool,self.worker._session,self.worker._backend._signer,queue=self.queue)
            self.cache_wakeup.peek(cache_queue) # advisory; always poll PG
        try:job=self.worker.claim_task(queue=self.queue,lease_seconds=self.lease_seconds)
        except Exception:raise RuntimeDispatchError('runtime_queue_unavailable') from None
        if job is None:return {'claimed':False}
        if cache_queue is not None:self.cache_wakeup.publish(cache_queue,job['task_id'])
        task_id,fence=job['task_id'],job['fence'];renew_at=time.monotonic()+self.lease_seconds/3
        command=None
        def authority():
            nonlocal renew_at
            if time.monotonic()>=deadline:raise RuntimeDispatchError('runtime_dispatch_timeout')
            self.worker.assert_task_lease(queue=self.queue,task_id=task_id,fence=fence)
            if time.monotonic()>=renew_at:
                self.worker.renew_task(queue=self.queue,task_id=task_id,fence=fence,lease_seconds=self.lease_seconds)
                renew_at=time.monotonic()+self.lease_seconds/3
        def request(operation):
            authority()
            body={'activation_ref':activation['activation_ref'],'command':command}
            if operation in {'start','resume'}:body['input']=input
            return self.client.request(operation,body,deadline=min(deadline,time.monotonic()+self.request_timeout))
        outcome='unknown';code=None;status='failed';receipt=None
        try:
            payload=job['payload']
            if type(payload) is not dict or set(payload)!={'run_command','input'}:raise RuntimeDispatchError('runtime_task_payload_invalid')
            command=payload['run_command'];input=payload['input'];_command(command);_input_digest(input)
            ttl=(datetime.fromisoformat(command['not_after'].replace('Z','+00:00'))-datetime.now(UTC)).total_seconds()
            deadline=min(deadline,time.monotonic()+ttl)
            authority()
            activation=self.worker.create_runtime_activation(queue=self.queue,task_id=task_id,fence=fence,
                run_id=command['run_id'],command=command,input=input,owner_epoch=command['runtime_owner_epoch'])
            try:inspection=request('inspect')
            except RuntimeDispatchError as error:
                if error.code!='runtime_state_missing':raise
                if activation.get('ever_execution_authorized') is not False:raise RuntimeDispatchError('runtime_reclaimed_state_missing')
                authority()
                fresh=self.worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='start',input=input)
                if fresh.get('authorized') is not True or fresh.get('ever_execution_authorized') is not False:raise RuntimeDispatchError('runtime_reclaimed_state_missing')
                # Host performs another fresh guard check at its actual missing
                # SQLite create boundary. Neither cached snapshot permits reset.
                request('start');inspection=request('inspect')
            else:
                if inspection['runtime_outcome'] in {'running','unknown'}:
                    request('resume');inspection=request('inspect')
            while inspection['runtime_outcome']=='running':
                time.sleep(min(self.poll_seconds,max(0,deadline-time.monotonic())))
                inspection=request('inspect')
            outcome=inspection['runtime_outcome']
            if outcome in {'succeeded','failed','cancelled'}:
                # Only an actual validated Host inspection supplies these fields.
                # Persisted queue result remains readable after its lease ends.
                receipt={key:inspection[key] for key in ('request_id','conversation_id','submission_id')}
                receipt['persistence']={'journal_mode':'wal','synchronous':2}
            status='succeeded' if outcome=='succeeded' else 'retry_wait' if outcome=='unknown' else 'failed'
            if status!='succeeded':code='runtime_'+outcome
        except StaleQueueLease:
            return {'claimed':True,'task_id':task_id,'fence':fence,'status':'lease_lost'}
        except Exception as error:
            code=error.code if isinstance(error,RuntimeDispatchError) else 'runtime_dispatch_unavailable'
            # Transport ambiguity may recover only the same persisted Run later.
            # Missing reclaimed state/conflict is terminal, never a new start.
            status='retry_wait' if code in {'runtime_transport_unavailable','runtime_transport_timeout','runtime_dispatch_timeout'} else 'failed'
        result={'scope':'runtime_only','business_action_success':False,'runtime_outcome':outcome}
        if command is not None:result['run_id']=command.get('run_id')
        if receipt is not None:result['runtime_receipt']=receipt
        if code is not None:result['code']=code
        try:
            self.worker.assert_task_lease(queue=self.queue,task_id=task_id,fence=fence)
            finished=self.worker.finish_task(queue=self.queue,task_id=task_id,fence=fence,status=status,result=result,retry_seconds=1 if status=='retry_wait' else 0)
        except StaleQueueLease:return {'claimed':True,'task_id':task_id,'fence':fence,'status':'lease_lost'}
        except Exception:raise RuntimeDispatchError('runtime_queue_unavailable') from None
        return {'claimed':True,**finished,'result':result}
