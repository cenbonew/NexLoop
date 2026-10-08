"""Disposable advisory wakeups. Every work decision still uses governed PG.

No cache value contains facts, business input, credentials, lease or authority.
"""
from dataclasses import dataclass,field
from pathlib import Path
from datetime import UTC,datetime
import hashlib,ipaddress,json,re,socket,ssl,threading,time
from nexloop_eios.private_configuration import read_private_text

@dataclass(frozen=True)
class WakeupConfiguration:
    host:str
    port:int
    ca_file:Path
    credentials_file:Path
    server_name:str
    timeout:float=.3
    ttl_seconds:int=10
    def __post_init__(self):
        if type(self.host)is not str or '%' in self.host:raise ValueError('literal cache IP required')
        try:ipaddress.ip_address(self.host)
        except ValueError:raise ValueError('literal cache IP required') from None
        if type(self.port)is not int or type(self.ttl_seconds)is not int or type(self.timeout)not in (int,float) or not 1<=self.port<=65535 or not .05<=self.timeout<=2 or not 1<=self.ttl_seconds<=60:raise ValueError('bounded cache configuration required')
        if type(self.server_name)is not str or not self.server_name or len(self.server_name)>253:raise ValueError('cache TLS name required')

class ValkeyWakeup:
    def __init__(self,config):
        self.config=config;self._lock=threading.Lock();self._available=False;self._last_success=None
        credentials=json.loads(read_private_text(config.credentials_file,maximum=1024))
        if set(credentials)!={'username','password'} or not all(type(v)is str and 1<=len(v)<=256 for v in credentials.values()):raise ValueError('invalid private cache credentials')
        self._credentials=credentials
        self._tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._tls.hostname_checks_common_name=False
        self._tls.load_verify_locations(cadata=read_private_text(config.ca_file,maximum=32768))
    @classmethod
    def from_file(cls,path):
        data=json.loads(read_private_text(path,maximum=4096))
        expected={'host','port','ca_file','credentials_file','server_name','timeout','ttl_seconds'}
        if type(data)is not dict or set(data)!=expected:raise ValueError('invalid private cache configuration')
        for key in ('ca_file','credentials_file'):
            if type(data[key])is not str or not Path(data[key]).is_absolute():raise ValueError('absolute private cache files required')
            data[key]=Path(data[key])
        return cls(WakeupConfiguration(**data))
    @staticmethod
    def namespace(queue):
        # Identity comes from the authenticated server-side queue, never frontend.
        session=queue.session
        return hashlib.sha256(json.dumps([session.authentication.tenant_id,session.world,queue.queue],separators=(',',':')).encode()).hexdigest()
    def _command(self,*values):
        c=self.config
        deadline=time.monotonic()+c.timeout
        address=ipaddress.ip_address(c.host)
        family=socket.AF_INET if address.version==4 else socket.AF_INET6
        endpoint=(str(address),c.port) if address.version==4 else (str(address),c.port,0,0)
        # One literal endpoint, no resolver or per-address retry loop.
        with socket.socket(family,socket.SOCK_STREAM) as raw:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('cache deadline')
            raw.settimeout(remaining)
            raw.connect(endpoint)
            with self._tls.wrap_socket(raw,server_hostname=c.server_name,do_handshake_on_connect=False) as connection:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('cache deadline')
                def expire():
                    try:connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                timer=threading.Timer(remaining,expire);timer.daemon=True;timer.start()
                def command(args):
                    if time.monotonic()>=deadline:raise TimeoutError('cache deadline')
                    values=[x.encode() for x in args]
                    connection.sendall(b'*'+str(len(values)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in values))
                    with connection.makefile('rb',buffering=0) as stream:
                        line=stream.readline(513)
                        if len(line)>512 or not line.endswith(b'\r\n'):raise ValueError('invalid cache reply')
                        if line[:1]==b'+':return line[1:-2].decode()
                        if line==b'$-1\r\n':return None
                        if line[:1]==b'$':
                            size=int(line[1:-2]);data=b''
                            if not 0<=size<=128:raise ValueError('invalid cache reply')
                            while len(data)<size+2:
                                chunk=stream.read(size+2-len(data))
                                if not chunk:raise ValueError('truncated cache reply')
                                data+=chunk
                            if data[-2:]!=b'\r\n':raise ValueError('invalid cache reply')
                            return data[:-2].decode()
                        raise ValueError('cache command rejected')
                try:
                    connection.settimeout(max(.001,deadline-time.monotonic()));connection.do_handshake()
                    if command(('AUTH',self._credentials['username'],self._credentials['password']))!='OK':raise ValueError('cache auth rejected')
                    result=command(values)
                    if time.monotonic()>=deadline:raise TimeoutError('cache deadline')
                    return result
                finally:timer.cancel()
    def _attempt(self,operation):
        try:
            result=operation();ok=True
        except Exception:result=None;ok=False
        with self._lock:
            self._available=ok
            if ok:self._last_success=datetime.now(UTC).isoformat()
        return ok,result
    def _expected(self,expected,*args):
        result=self._command(*args)
        if result!=expected:raise ValueError('unexpected cache reply')
        return result
    def publish(self,queue,task_id):
        opaque=hashlib.sha256(str(task_id).encode()).hexdigest()
        return self._attempt(lambda:self._expected('OK','SET','nexloop:wakeup:'+self.namespace(queue),opaque,'EX',str(self.config.ttl_seconds)))[0]
    def peek(self,queue):
        return self._attempt(lambda:self._command('GET','nexloop:wakeup:'+self.namespace(queue)))
    def health(self,*,probe=False):
        if probe:self._attempt(lambda:self._expected('PONG','PING'))
        with self._lock:return {'configured':True,'available':self._available,'degraded':not self._available,'authoritative':False,'last_success_at':self._last_success}

class AdvisoryQueueScheduler:
    """Actual queue relay/poll: cache outages never govern ACK or task execution.

Bounded recent task IDs are an acceleration cache only. Restart always polls PG
and reconstructs hints from actual newly claimed work and pending outbox events.
"""
    def __init__(self,queue,wakeup):
        from nexloop_eios.durable_queue import PostgresDurableQueue
        if not isinstance(queue,PostgresDurableQueue):raise TypeError('authenticated PG queue required')
        self.queue,self.wakeup=queue,wakeup;self._recent={}
    def accept(self,**arguments):
        receipt=self.queue.accept(**arguments) # transaction committed first
        self.wakeup.publish(self.queue,receipt['task_id'])
        return receipt
    def run_once(self,*,lease_seconds=30):
        if type(lease_seconds)is not int or not 1<=lease_seconds<=300 or self.wakeup.config.timeout>lease_seconds/6:
            raise ValueError('cache deadline exceeds queue lease budget')
        # GET is advisory only: missing/unavailable never suppresses PG polling.
        self.wakeup.peek(self.queue)
        event=self.queue.claim_outbox(lease_seconds=lease_seconds)
        if event is not None:
            if self.wakeup.publish(self.queue,event['task_id']):
                self.queue.acknowledge_outbox(outbox_id=event['outbox_id'],fence=event['fence'])
        for task_id in tuple(self._recent):
            # Current authorization enforced by inspect; never use stale cache
            # as an authority. A revoked queue read propagates fail-closed.
            task=self.queue.inspect(task_id=task_id)
            if task['status'] in {'pending','running','retry_wait'}:self.wakeup.publish(self.queue,task_id)
            else:del self._recent[task_id]
        job=self.queue.claim(lease_seconds=lease_seconds)
        if job is not None:
            self._recent[job['task_id']]=None
            if len(self._recent)>64:self._recent.pop(next(iter(self._recent)))
            self.wakeup.publish(self.queue,job['task_id'])
        return job
