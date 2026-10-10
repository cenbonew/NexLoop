"""Private standalone queue Worker + live EIOS activation guard.

This entry point never bootstraps/migrates a database or starts/manages a Host.
All secret material comes from explicitly named private service-owned files.
SIGTERM/SIGINT stop subsequent claims; a current bounded dispatch settles using
its original lease/fence, without blind cancellation or a new Run submission.
"""
import argparse
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import threading

from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.runtime_control import create_runtime_guard_server
from nexloop_eios.runtime_dispatch import RuntimeDispatcher


class _Parser(argparse.ArgumentParser):
    def error(self,message):
        # Never echo invalid arguments, file content, credentials or DSNs.
        self.exit(2,'Runtime Worker configuration unavailable\n')


class _FreshGuard:
    def __init__(self,backend,credential_file,world):
        self.backend,self.credential_file,self.world=backend,credential_file,world

    def service(self):
        token=read_private_text(self.credential_file,maximum=16384)
        return self.backend.authenticate(token,world=self.world)

    def runtime_effect_tool(self,**arguments):
        return self.service().runtime_effect_tool(**arguments)

    def record_plan_outcome(self,**arguments):
        # NX-024: run-outcome of a plan reevaluation Run, under a fresh current service session.
        return self.service().record_plan_outcome(**arguments)

    def authorize_runtime_activation(self,**arguments):
        # Each guard request constructs an actual current authenticated EIOS
        # service session; no persisted Run token or cached authority is used.
        return self.service().authorize_runtime_activation(**arguments)


def _arguments(argv):
    parser=_Parser(description=__doc__)
    for option in ('database-url-file','signing-key-file','service-credential-file','artifact-root',
        'host-control-key-file','host-ca-file','guard-key-file','guard-certificate-file','guard-tls-key-file'):
        parser.add_argument('--'+option,type=Path,required=True)
    parser.add_argument('--cache-configuration-file',type=Path)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--world',required=True);parser.add_argument('--queue',required=True)
    parser.add_argument('--host-origin',required=True);parser.add_argument('--guard-port',type=int,required=True)
    parser.add_argument('--lease-seconds',type=int,default=30)
    parser.add_argument('--total-timeout',type=float,default=30)
    parser.add_argument('--request-timeout',type=float,default=2)
    parser.add_argument('--poll-seconds',type=float,default=.05)
    parser.add_argument('--tick-seconds',type=float,default=.25)
    parser.add_argument('--once',action='store_true',help='one bounded dispatch tick; no provider selection or real Action success claim')
    parser.add_argument('--guard-workers',type=int,default=1,help='serve the guard from N child processes (ADR-024; default 1: in-process thread, stage deployment uses 4)')
    parser.add_argument('--dispatcher-pool-max',type=int,default=2,help='PG pool max of the dispatcher-only parent when --guard-workers > 1 (ADR-024)')
    args=parser.parse_args(argv)
    if (not 1024<=args.guard_port<=65535 or re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',args.queue) is None
        or not args.world or len(args.world)>255 or not args.signing_key_id or len(args.signing_key_id)>255
        or not math.isfinite(args.tick_seconds) or not 0<args.tick_seconds<=30 or not 1<=args.guard_workers<=16
        or not 1<=args.dispatcher_pool_max<=32):parser.error('configuration')
    try:args.guard_pool_max=guard_pool_max(os.environ)
    except ValueError:parser.error('configuration')
    return args


def guard_pool_max(environ):
    """PG pool max of every Backend that serves the guard (NEX_EIOS_DB_POOL_MAX, default 4).

    LifecycleLock admits pool_max // 2 guard requests per process, so at least 2."""
    value=environ.get('NEX_EIOS_DB_POOL_MAX','4').strip()
    if not value.isdigit() or not 2<=int(value)<=32:raise ValueError('NEX_EIOS_DB_POOL_MAX must be 2..32')
    return int(value)


def backend_pool_max(arguments):
    """N=1: the single Backend serves dispatcher and guard. N>1: the parent only dispatches."""
    return arguments.guard_pool_max if arguments.guard_workers==1 else arguments.dispatcher_pool_max


def run(arguments,stop):
    # Validate all private files and the fixed loopback destination before any
    # backend opens or guard listener claims work. No ambient env fallback.
    from nexloop_eios.valkey_wakeup import ValkeyWakeup
    cache=ValkeyWakeup.from_file(arguments.cache_configuration_file) if arguments.cache_configuration_file else None
    dsn=read_private_text(arguments.database_url_file,maximum=16384)
    read_private_text(arguments.service_credential_file,maximum=16384)
    key=read_private_text(arguments.host_control_key_file,maximum=64)
    if re.fullmatch('[0-9a-f]{64}',key) is None:raise ValueError('private transport key unavailable')
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_verify_locations(cadata=read_private_text(arguments.host_ca_file,maximum=32768))
    host=HostControlConfiguration(arguments.host_origin,arguments.host_control_key_file,arguments.host_ca_file)
    with open_backend(database_url=dsn,artifact_root=arguments.artifact_root,
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id,pool_max_size=backend_pool_max(arguments)) as backend:
        with backend._pool.connection() as connection:
            if verify_application_role(connection) not in {'nexloop_domain_worker','nexloop_scheduler'}:
                raise ValueError('restricted Worker role required')
        guard=_FreshGuard(backend,arguments.service_credential_file,arguments.world)
        # Preflight current authentication and dispatch config before binding.
        RuntimeDispatcher(guard.service(),host,queue=arguments.queue,lease_seconds=arguments.lease_seconds,
            total_timeout=arguments.total_timeout,request_timeout=arguments.request_timeout,poll_seconds=arguments.poll_seconds,cache_wakeup=cache)
        server=None;thread=None;thread_started=False;pool=None
        try:
            if arguments.guard_workers==1:
                server=create_runtime_guard_server(guard,port=arguments.guard_port,key_file=arguments.guard_key_file,
                    certificate_file=arguments.guard_certificate_file,tls_key_file=arguments.guard_tls_key_file)
                thread=threading.Thread(target=server.serve_forever,name='nexloop-runtime-guard',daemon=True)
                thread.start();thread_started=True
            else:
                from nexloop_eios.runtime_guard_worker import GuardFiles,GuardWorkerPool
                pool=GuardWorkerPool(GuardFiles(arguments.database_url_file,arguments.signing_key_file,arguments.signing_key_id,
                    arguments.service_credential_file,arguments.artifact_root,arguments.world,arguments.guard_key_file,
                    arguments.guard_certificate_file,arguments.guard_tls_key_file),port=arguments.guard_port,workers=arguments.guard_workers,
                    pool_max=arguments.guard_pool_max,on_failure=stop.set)
                pool.start()
            print('Runtime Worker ready',flush=True)
            while not stop.is_set():
                try:
                    # File rotation/current realm facts are read afresh each tick.
                    dispatcher=RuntimeDispatcher(guard.service(),host,queue=arguments.queue,lease_seconds=arguments.lease_seconds,
                        total_timeout=arguments.total_timeout,request_timeout=arguments.request_timeout,poll_seconds=arguments.poll_seconds,cache_wakeup=cache)
                    if stop.is_set():break
                    result=dispatcher.run_once()
                    if arguments.once:
                        # Strict allowlist; no task/Run/ref/payload/exception text.
                        status=result.get('status','idle')
                        if status not in {'idle','succeeded','failed','retry_wait','dead_lettered','lease_lost'}:status='unavailable'
                        summary={'claimed':result.get('claimed') is True,'status':status}
                        if cache is not None:summary['cache']=cache.health()
                        print(json.dumps(summary,separators=(',',':')),flush=True)
                        return 0
                except Exception:
                    if arguments.once:
                        print('Runtime Worker dispatch unavailable',file=sys.stderr,flush=True)
                        return 1
                    # Stay silent on repeated failures; every future tick rechecks
                    # actual credentials and authority without minting a Run.
                if arguments.once:break
                stop.wait(arguments.tick_seconds)
            if pool is not None and pool.failed:
                # Children kept exiting: stop claiming and leave restart to the service manager.
                print('Runtime guard workers unavailable',file=sys.stderr,flush=True)
                return 1
            return 0
        finally:
            if pool is not None:pool.stop()
            if server is not None:
                if thread_started:server.shutdown()
                server.server_close()
            if thread_started:
                thread.join(5)
                if thread.is_alive():raise RuntimeError('guard shutdown unavailable')


def main(argv=None):
    arguments=_arguments(argv);stop=threading.Event();previous={}
    # Pool retry diagnostics may contain connection host/user/error context.
    # This dedicated CLI exposes only its fixed summaries, even on bad DSNs.
    pool_logger=logging.getLogger('psycopg.pool');was_disabled=pool_logger.disabled
    pool_logger.disabled=True
    def stop_claiming(signum,frame):stop.set()
    try:
        for signum in (signal.SIGTERM,signal.SIGINT):
            previous[signum]=signal.signal(signum,stop_claiming)
        return run(arguments,stop)
    except Exception:
        print('Runtime Worker unavailable',file=sys.stderr,flush=True)
        return 1
    finally:
        pool_logger.disabled=was_disabled
        for signum,handler in previous.items():signal.signal(signum,handler)


if __name__=='__main__':raise SystemExit(main())
