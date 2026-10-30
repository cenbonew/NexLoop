"""Private configured continuous governed message relay; requires schema 0049."""
import argparse
import fcntl
import json
import time
import logging
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.message_relay import MessageRelay, validate_static_recipe, validate_run_profile
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.run_credential_vault import RunCredentialVault

# NX-031 D7 (deploy/stage/connection-budget.v1.json): measured smallest workable pool of this single-loop process (1 fails).
POOL_MAX=2


class _Parser(argparse.ArgumentParser):
    def error(self,unused): self.exit(2,'Message relay configuration unavailable\n')


def arguments(argv):
    parser=_Parser(description=__doc__)
    for name in ('database-url-file','signing-key-file','route-credential-file','source-credential-file',
        'planner-credential-file','executor-credential-file','artifact-root','vault-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    # Exactly one mode (relay multi-consumer D4): one consumer's recipe, or the deployment Run profile for every consumer
    # enrolled for this relay principal (0160).
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--recipe-file',type=Path)
    mode.add_argument('--run-profile-file',type=Path)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--tick-seconds',type=float,default=.25)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--pool-max',type=int,default=POOL_MAX)
    # NX-038 E3 wiring (L6 review 5.1): the semantic-read pool as the budget declares it (semantic_read_pool_max); 0 reads nothing.
    parser.add_argument('--semantic-read-pool-max',type=int,default=0)
    # NX-023: explicit published strategy selects Context v6; omitted keeps the v2 Context.
    parser.add_argument('--context-strategy')
    parser.add_argument('--max-backlog',type=int,help='stop claiming while the tenant has this many open message Run tasks (default: no bound)')
    result=parser.parse_args(argv)
    if not math.isfinite(result.tick_seconds) or not .01<=result.tick_seconds<=30 or not re.fullmatch('[A-Za-z0-9_-]{1,64}',result.signing_key_id) or not 1<=result.pool_max<=8 or not 0<=result.semantic_read_pool_max<=4: parser.error('configuration')
    if result.context_strategy is not None and not re.fullmatch('[a-z][a-z0-9_]{0,63}',result.context_strategy): parser.error('configuration')
    if result.max_backlog is not None and not 1<=result.max_backlog<=10000: parser.error('configuration')
    return result


def _strict_json(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError()
            result[key]=value
        return result
    def constant(unused): raise ValueError()
    return json.loads(read_private_text(path,maximum=32768),object_pairs_hook=pairs,parse_constant=constant)


def _recipe(path):return validate_static_recipe(_strict_json(path))
def _run_profile(path):return validate_run_profile(_strict_json(path))


def _configuration(config):
    return ('recipe',_recipe(config.recipe_file)) if config.recipe_file is not None else ('run_profile',_run_profile(config.run_profile_file))


class TickFailures:
    """Loop mode used to swallow every failed tick without a trace (stage 10-30: no claim for days, only "ready" in the log).
    One stderr line per change of failure code, again every `repeat` seconds while it persists, and one when ticks recover;
    codes only, never a value, path, token or body."""
    def __init__(self,stream=None,clock=time.monotonic,repeat=300):
        self.stream,self.clock,self.repeat=stream,clock,repeat;self.code=None;self.since=None;self.count=0;self.last=None

    def failed(self,code):
        now=self.clock();self.count+=1
        if code!=self.code:self.code,self.since,self.count,self.last=code,now,1,None
        if self.last is None or now-self.last>=self.repeat:
            self.last=now
            print('Message relay tick failed '+json.dumps({'code':code,'consecutive':self.count,'seconds':int(now-self.since)},separators=(',',':')),
                file=self.stream or sys.stderr,flush=True)

    def succeeded(self):
        if self.code is not None:
            print('Message relay tick recovered '+json.dumps({'code':self.code,'failed_ticks':self.count},separators=(',',':')),file=self.stream or sys.stderr,flush=True)
        self.code=self.since=self.last=None;self.count=0


def run(config,stop):
    dsn=read_private_text(config.database_url_file,maximum=16384)
    initial=_configuration(config)
    # Runtime Artifacts and bearer vault must have separate directory trees.
    artifact=config.artifact_root.resolve(); vault_path=config.vault_root.resolve()
    if artifact==vault_path or artifact in vault_path.parents or vault_path in artifact.parents: raise ValueError()
    with RunCredentialVault(config.vault_root) as vault:
        owner=-1
        try:
            owner=os.open('relay.owner',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=vault._fd)
            info=os.fstat(owner)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1: raise ValueError()
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            from nexloop_eios.context_artifacts import configure_semantic_reads
            configure_semantic_reads(config.semantic_read_pool_max)
            with open_backend(database_url=dsn,artifact_root=config.artifact_root,
                signing_key_file=config.signing_key_file,signing_key_id=config.signing_key_id,pool_max_size=config.pool_max) as backend:
                with backend._pool.connection() as db:
                    if verify_application_role(db)!='nexloop_api': raise ValueError()
                def services():
                    return [backend.authenticate(read_private_text(path,maximum=16384),world='real') for path in
                        (config.route_credential_file,config.source_credential_file,config.planner_credential_file)]
                services(); backend.authenticate(read_private_text(config.executor_credential_file,maximum=16384),world='real')
                # Relay step timings (dispatcher 2026-10-13): one sample per step and minute (kind relay_step), numbers only.
                from nexloop_eios.observability import RELAY,flush_relay,start_sampler
                start_sampler(stop,60,lambda:flush_relay(backend._pool))
                print('Message relay ready',flush=True)
                failures=TickFailures();stage='configuration'
                while not stop.is_set():
                    try:
                        stage='configuration'
                        current=_configuration(config)
                        # Rotation cannot alter stable business assignments in place.
                        if current!=initial: stage='configuration_changed';raise ValueError()
                        stage='authenticate'
                        authenticated=time.monotonic()
                        route,source,planner=services()
                        RELAY.observe('authenticate',(time.monotonic()-authenticated)*1000)
                        executor=read_private_text(config.executor_credential_file,maximum=16384)
                        stage='vault';vault._check()
                        if stop.is_set(): break
                        stage='relay'
                        status=MessageRelay(route=route,source=source,planner=planner,
                            executor_token=executor,vault=vault,**{current[0]:current[1]},context_strategy=config.context_strategy,max_backlog=config.max_backlog).run_once()
                        if config.once:
                            if status not in {'idle','queued','requires_governed_replan','handed_to_fallback'}: raise ValueError()
                            print(json.dumps({'status':status},separators=(',',':')),flush=True)
                            return 0
                        failures.succeeded()
                    except Exception:
                        if config.once:
                            print('Message relay unavailable',file=sys.stderr,flush=True);return 1
                        failures.failed(stage)
                    stop.wait(config.tick_seconds)
            return 0
        finally:
            if owner>=0: os.close(owner)


def main(argv=None):
    # NX-030 / AT-049: every log record leaves this process as one allowlisted structured line (IDs, codes, durations only).
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('message-relay')
    config=arguments(argv);stop=threading.Event();previous={}
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    def terminate(signum,frame): stop.set()
    try:
        for signum in (signal.SIGTERM,signal.SIGINT): previous[signum]=signal.signal(signum,terminate)
        return run(config,stop)
    except Exception:
        print('Message relay unavailable',file=sys.stderr,flush=True);return 1
    finally:
        logger.disabled=disabled
        for signum,handler in previous.items(): signal.signal(signum,handler)

if __name__=='__main__': raise SystemExit(main())
