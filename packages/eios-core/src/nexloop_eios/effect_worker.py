"""Restricted standalone effect Worker; private configuration, no migrations."""
import argparse
import json
import logging
import math
from pathlib import Path
import re
import signal
import ssl
import sys
import threading

from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import EffectProviderConfiguration,HttpEffectProvider
from nexloop_eios.private_configuration import read_private_text


class _Parser(argparse.ArgumentParser):
    def error(self,message):self.exit(2,'Effect Worker configuration unavailable\n')


def _arguments(argv):
    parser=_Parser(description=__doc__)
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root','provider-config-file'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--world',required=True,choices=['real'])
    parser.add_argument('--lease-seconds',type=int,default=30)
    parser.add_argument('--tick-seconds',type=float,default=.25)
    parser.add_argument('--once',action='store_true')
    arguments=parser.parse_args(argv)
    if (not 3<=arguments.lease_seconds<=300 or not math.isfinite(arguments.tick_seconds)
        or not 0<arguments.tick_seconds<=30 or re.fullmatch('[A-Za-z0-9_-]{1,64}',arguments.signing_key_id) is None):
        parser.error('configuration')
    return arguments


def _provider(path):
    body=json.loads(read_private_text(path,maximum=32768))
    required={'origin','credential_file','ca_file','timeout'}
    if type(body) is not dict or not required<=body.keys() or body.keys()-required-{'connect_address'}:raise ValueError()
    for name in ('origin','credential_file','ca_file'):
        if type(body[name]) is not str or not body[name]:raise ValueError()
    for name in ('credential_file','ca_file'):
        if not Path(body[name]).is_absolute():raise ValueError()
    credential=read_private_text(body['credential_file'],maximum=4096)
    if re.fullmatch('[A-Za-z0-9._~-]{16,4096}',credential) is None:raise ValueError()
    ca=read_private_text(body['ca_file'],maximum=32768)
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);tls.load_verify_locations(cadata=ca)
    return HttpEffectProvider(EffectProviderConfiguration(**body))


class _FreshExecutor:
    def __init__(self,backend,path,world):self.backend,self.path,self.world=backend,path,world
    def service(self):return self.backend.authenticate(read_private_text(self.path,maximum=16384),world=self.world)
    def __getattr__(self,name):
        if name not in {'claim_effect','prepare_effect_dispatch','authorize_effect_query','record_effect_observation','record_effect_query_observation','record_effect_unknown'}:
            raise AttributeError(name)
        def invoke(**arguments):return getattr(self.service(),name)(**arguments)
        return invoke


def run(arguments,stop):
    dsn=read_private_text(arguments.database_url_file,maximum=16384)
    read_private_text(arguments.service_credential_file,maximum=16384)
    provider=_provider(arguments.provider_config_file)
    # Reject transport bounds and missing private material before claiming.
    if provider.configuration.timeout>arguments.lease_seconds/3:raise ValueError()
    with open_backend(database_url=dsn,artifact_root=arguments.artifact_root,
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id) as backend:
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!='nexloop_action_worker':raise ValueError()
        executor=_FreshExecutor(backend,arguments.service_credential_file,arguments.world)
        executor.service()
        print('Effect Worker ready',flush=True)
        while not stop.is_set():
            try:
                # Re-read trusted transport files and service credentials every
                # tick and re-authenticate again at each protected ledger call.
                provider=_provider(arguments.provider_config_file)
                executor.service()
                if stop.is_set():break
                result=EffectDispatcher(executor,provider,lease_seconds=arguments.lease_seconds).run_once()
                if arguments.once:
                    status=result.get('status','idle')
                    if status not in {'idle','unknown','dispatching','observed_fulfilled','fulfilled','confirmed','query_unavailable','record_unavailable','admission_unavailable'}:
                        status='unavailable'
                    print(json.dumps({'claimed':result.get('claimed') is True,'status':status},separators=(',',':')),flush=True)
                    return 0
            except Exception:
                if arguments.once:
                    print('Effect Worker dispatch unavailable',file=sys.stderr,flush=True);return 1
            stop.wait(arguments.tick_seconds)
        return 0


def main(argv=None):
    # NX-030 / AT-049: every log record leaves this process as one allowlisted structured line (IDs, codes, durations only).
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('effect-worker')
    arguments=_arguments(argv);stop=threading.Event();previous={}
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    def terminate(signum,frame):stop.set()
    try:
        for signum in (signal.SIGTERM,signal.SIGINT):previous[signum]=signal.signal(signum,terminate)
        return run(arguments,stop)
    except Exception:
        print('Effect Worker unavailable',file=sys.stderr,flush=True);return 1
    finally:
        logger.disabled=disabled
        for signum,handler in previous.items():signal.signal(signum,handler)


if __name__=='__main__':raise SystemExit(main())
