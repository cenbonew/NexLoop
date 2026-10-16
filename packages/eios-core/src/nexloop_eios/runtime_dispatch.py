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
import math
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


# Transport ambiguity may recover only the same persisted Run later; a request the Host refuses for its size never will.
POLL_MAX_SECONDS=.5
_STAGE_CODES={'control':'runtime_control_unavailable','activation':'runtime_activation_unavailable','host':'runtime_dispatch_unavailable'}
ADMISSION_TIMEOUT_FACTOR=4


def exception_classes(error,limit=8):
    """The class names (no message, no value) of an exception and of what it was raised from or during, outermost first. A port that
    hides its cause (`raise X from None`) still keeps it as __context__; 2026-10-16 stage: AuthorizationUnavailable over a psycopg
    InsufficientPrivilege was visible only in the PostgreSQL log."""
    names=[];seen=set()
    while error is not None and len(names)<limit and id(error) not in seen:
        seen.add(id(error));names.append(type(error).__name__)
        error=error.__cause__ if error.__cause__ is not None else error.__context__
    return names


def next_poll_interval(interval,base):
    """Inspect polling backs off: double the last interval, at most POLL_MAX_SECONDS (never below the configured base)."""
    return min(interval*2,max(base,POLL_MAX_SECONDS))

_RETRYABLE=frozenset({'runtime_transport_unavailable','runtime_transport_timeout','runtime_dispatch_timeout','runtime_response_invalid'})
_FAILURE_CODES=frozenset({'runtime_authorization_unavailable','runtime_authorization_denied','runtime_tool_error','runtime_task_failed'})
_DETAIL=re.compile(r'[a-z][a-z_]{0,63}')
NO_REPLY_CODE='runtime_no_reply_submitted'


def reply_settle(worker,run_id):
    """0200: whether this Run answers a message and submitted its reply; when not, the message's reply-due item becomes due now.
    Best effort: the queue result never depends on it ({} when unavailable)."""
    try:
        session=worker._session
        with worker._backend._pool.connection() as c,c.transaction():
            value=c.execute('select authz.nexloop_message_run_reply_settle(%s,%s,%s)',(session.token_digest,session.world,run_id)).fetchone()[0]
        return value if type(value) is dict else {}
    except Exception:
        return {}


def model_timing(value):
    """NX-031 A2: the Host's model time of a Run, passed on only as exactly {calls, total_ms, max_ms} of non-negative numbers
    (no other key, no text); anything else is dropped (the Run's outcome never depends on it)."""
    if type(value) is not dict or set(value)!={'calls','total_ms','max_ms'}:return None
    if type(value['calls']) is not int or not 0<=value['calls']<=10000:return None
    if any(type(value[k]) not in (int,float) or isinstance(value[k],bool) or not math.isfinite(value[k]) or not 0<=value[k]<=86400000 for k in ('total_ms','max_ms')):return None
    if value['max_ms']>value['total_ms']:return None
    return {'calls':value['calls'],'total_ms':value['total_ms'],'max_ms':value['max_ms']}


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
        connection=None;timer=None;response=None
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeDispatchError('runtime_transport_timeout')
            key=read_private_text(self.configuration.key_file,maximum=64)
            if re.fullmatch('[0-9a-f]{64}',key) is None:raise ValueError()
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_verify_locations(cadata=read_private_text(self.configuration.ca_file,maximum=32768))
            payload=json.dumps(body,separators=(',',':')).encode()
            # The Host refuses a body above 262144 bytes (413); refused here before sending, as its own code (deterministic).
            if len(payload)>262144:raise RuntimeDispatchError('runtime_request_too_large')
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
                if response.status==413:raise RuntimeDispatchError('runtime_request_too_large')
                # NX-031: any other Host status keeps its number in the code (retried like before; no longer called transport).
                raise RuntimeDispatchError('runtime_host_status_%d'%response.status if 100<=response.status<=599 else 'runtime_response_invalid')
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
        except (TimeoutError,socket.timeout):raise RuntimeDispatchError('runtime_transport_timeout') from None
        # NX-031: a Host answer that fails validation (type, size, framing, JSON, receipt) is not a transport failure.
        except Exception:
            # The deadline timer shuts the socket down; whatever that breaks is the timeout, not a bad answer.
            if time.monotonic()>=deadline:raise RuntimeDispatchError('runtime_transport_timeout') from None
            raise RuntimeDispatchError('runtime_response_invalid' if response is not None else 'runtime_transport_unavailable') from None
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
        def expired():
            return command is not None and datetime.fromisoformat(command['not_after'].replace('Z','+00:00'))<=datetime.now(UTC)
        def authority():
            nonlocal renew_at
            # NX-031 ladder: a Run past its not_after (= its credential's expiry) can never run; that is terminal, not a timeout.
            if time.monotonic()>=deadline:raise RuntimeDispatchError('runtime_command_expired' if expired() else 'runtime_dispatch_timeout')
            self.worker.assert_task_lease(queue=self.queue,task_id=task_id,fence=fence)
            if time.monotonic()>=renew_at:
                self.worker.renew_task(queue=self.queue,task_id=task_id,fence=fence,lease_seconds=self.lease_seconds)
                renew_at=time.monotonic()+self.lease_seconds/3
        def request(operation):
            authority()
            body={'activation_ref':activation['activation_ref'],'command':command}
            if operation in {'start','resume'}:body['input']=input
            # NX-031 (lane-history-fix): a start/resume does two guard authorizations (each up to the 2 s limit of ADR-022 §4) and
            # creates the Run's durable storage, so it gets ADMISSION_TIMEOUT_FACTOR request timeouts (at most a third of the
            # lease); giving up earlier left the Run running on the Host while its task went back to the queue.
            timeout=self.request_timeout if operation not in {'start','resume'} else min(self.lease_seconds/3,self.request_timeout*ADMISSION_TIMEOUT_FACTOR)
            return self.client.request(operation,body,deadline=min(deadline,time.monotonic()+timeout))
        def ambiguous(error):
            return error.code in _RETRYABLE and error.code!='runtime_dispatch_timeout' or error.code.startswith('runtime_host_status_5')
        def observe():
            # NX-031 (lane-history-fix): while this loop holds the lease, an ambiguous inspect (timeout, transport, invalid answer,
            # Host 5xx) is asked again until the deadline. Giving the task back on the first one released the lease while the Run
            # kept running on the Host, and that Run's next guard authorization was then denied (activation lease denied).
            while True:
                try:return request('inspect')
                except RuntimeDispatchError as error:
                    if not ambiguous(error) or time.monotonic()>=deadline:raise
                    time.sleep(min(POLL_MAX_SECONDS,max(0,deadline-time.monotonic())))
        def submit(operation):
            # The Host may have admitted the Run although its answer did not arrive; the next inspect tells.
            try:request(operation)
            except RuntimeDispatchError as error:
                if not ambiguous(error):raise
                try:return observe()
                except RuntimeDispatchError as missing:
                    # Not admitted after all: the original ambiguity stands (the task is retried as before).
                    if missing.code=='runtime_state_missing':raise error from None
                    raise
            return observe()
        outcome='unknown';code=None;status='failed';receipt=None;timing=None;failure=None;failure_detail=None;stage='payload';error_classes=None
        try:
            payload=job['payload']
            if type(payload) is not dict or set(payload)!={'run_command','input'}:raise RuntimeDispatchError('runtime_task_payload_invalid')
            command=payload['run_command'];input=payload['input'];_command(command);_input_digest(input)
            ttl=(datetime.fromisoformat(command['not_after'].replace('Z','+00:00'))-datetime.now(UTC)).total_seconds()
            # A Run queued past its credential's lifetime is refused before any activation or Host request.
            if ttl<=0:raise RuntimeDispatchError('runtime_command_expired')
            deadline=min(deadline,time.monotonic()+ttl)
            authority()
            stage='control'
            # NX-022: owner controls are re-read before this queued Run is activated.
            from nexloop_eios.goal_controls import ControlDenied,ControlPlane
            try:ControlPlane(self.worker._backend._pool,self.worker._session).prepare_run_dispatch(task_id=task_id,command=command)
            except ControlDenied as denied:raise RuntimeDispatchError(denied.reason) from None
            stage='activation'
            activation=self.worker.create_runtime_activation(queue=self.queue,task_id=task_id,fence=fence,
                run_id=command['run_id'],command=command,input=input,owner_epoch=command['runtime_owner_epoch'])
            stage='host'
            try:inspection=observe()
            except RuntimeDispatchError as error:
                if error.code!='runtime_state_missing':raise
                if activation.get('ever_execution_authorized') is not False:raise RuntimeDispatchError('runtime_reclaimed_state_missing')
                authority()
                fresh=self.worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='start',input=input)
                if fresh.get('authorized') is not True or fresh.get('ever_execution_authorized') is not False:raise RuntimeDispatchError('runtime_reclaimed_state_missing')
                # Host performs another fresh guard check at its actual missing
                # SQLite create boundary. Neither cached snapshot permits reset.
                inspection=submit('start')
            else:
                if inspection['runtime_outcome'] in {'running','unknown'}:
                    inspection=submit('resume')
            # NX-031 (lane-history-fix): every inspect is a Host request that passes the Host's single serial queue and a full guard
            # authorization, so a fixed 50 ms poll from every dispatch loop kept that queue full and, as guard latency grows with the
            # tenant's data, pushed each Host request past request_timeout. The interval now doubles up to POLL_MAX_SECONDS.
            interval=self.poll_seconds
            while inspection['runtime_outcome']=='running':
                time.sleep(min(interval,max(0,deadline-time.monotonic())))
                interval=next_poll_interval(interval,self.poll_seconds)
                inspection=observe()
            outcome=inspection['runtime_outcome']
            if outcome in {'succeeded','failed','cancelled'}:
                # Only an actual validated Host inspection supplies these fields.
                # Persisted queue result remains readable after its lease ends.
                receipt={key:inspection[key] for key in ('request_id','conversation_id','submission_id')}
                receipt['persistence']={'journal_mode':'wal','synchronous':2}
                timing=model_timing(inspection.get('model_timing'))
            status='succeeded' if outcome=='succeeded' else 'retry_wait' if outcome=='unknown' else 'failed'
            if status!='succeeded':code='runtime_'+outcome
            # NX-031: the Host names an authorization failure with one allowlisted code. A guard that did not answer (after the
            # Host's bounded retries) is not a denial: the task code says so. The Pi Run is terminal, so the task ends failed and
            # the reply guarantee's fallback takes the message, as for any failed Run; a denial keeps runtime_failed.
            if outcome=='failed' and inspection.get('failure_code') in _FAILURE_CODES:
                failure=inspection['failure_code']
                # 10/16: a tool's own error code or a failed task's runtime code (codes only; anything else is dropped).
                detail=inspection.get('failure_detail')
                failure_detail=detail if type(detail) is str and _DETAIL.fullmatch(detail) else None
                if failure=='runtime_authorization_unavailable':code=failure
            # Reply fail-fast (10/16 stage smoke): a terminal message or fallback reply Run that submitted no reply is a definite
            # failure (the model answered in plain text, which is never delivered), and its message's reply guarantee is due now.
            if outcome in {'succeeded','failed','cancelled'}:
                settled=reply_settle(self.worker,command['run_id'])
                if settled.get('reply_run') is True and settled.get('replied') is False and status=='succeeded':
                    status='failed';code=NO_REPLY_CODE
        except StaleQueueLease:
            return {'claimed':True,'task_id':task_id,'fence':fence,'status':'lease_lost'}
        except Exception as error:
            # NX-031 ladder: an unexpected failure is named by where it happened (control recheck, activation, Host exchange),
            # and a Run that has meanwhile passed its not_after is expired (terminal) rather than unavailable.
            code=error.code if isinstance(error,RuntimeDispatchError) else 'runtime_command_expired' if expired() else _STAGE_CODES.get(stage,'runtime_dispatch_unavailable')
            # Transport ambiguity may recover only the same persisted Run later.
            # Missing reclaimed state/conflict is terminal, never a new start.
            status='retry_wait' if code in _RETRYABLE or code.startswith('runtime_host_status_') else 'failed'
            error_classes=exception_classes(error)
        result={'scope':'runtime_only','business_action_success':False,'runtime_outcome':outcome}
        if command is not None:result['run_id']=command.get('run_id')
        if receipt is not None:result['runtime_receipt']=receipt
        if receipt is not None and command is not None:
            # NX-031 D6: the Run's summed tool time (guard-side, numbers only) in the terminal result; best effort.
            try:
                with self.worker._backend._pool.connection() as db,db.transaction():
                    result['tool_timing']=db.execute('select authz.nexloop_run_tool_timing(%s::text,%s::text,%s::uuid)',
                        (self.worker._session.token_digest,self.worker._session.world,command['run_id'])).fetchone()[0]
            except Exception:pass
        if timing is not None:result['model_timing']=timing  # NX-031 A2: numbers only, beside tool_timing
        if failure is not None:result['failure_code']=failure
        if failure_detail is not None:result['failure_detail']=failure_detail
        if code is not None:result['code']=code
        if error_classes:result['error_class']=error_classes[0]
        if error_classes and len(error_classes)>1:result['error_cause_class']=error_classes[-1]
        try:
            self.worker.assert_task_lease(queue=self.queue,task_id=task_id,fence=fence)
            finished=self.worker.finish_task(queue=self.queue,task_id=task_id,fence=fence,status=status,result=result,retry_seconds=1 if status=='retry_wait' else 0)
        except StaleQueueLease:return {'claimed':True,'task_id':task_id,'fence':fence,'status':'lease_lost'}
        except Exception:raise RuntimeDispatchError('runtime_queue_unavailable') from None
        return {'claimed':True,**finished,'result':result}
