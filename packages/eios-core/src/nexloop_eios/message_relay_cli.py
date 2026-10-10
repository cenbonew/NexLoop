"""Private configured continuous governed message relay; requires schema 0049."""
import argparse
import fcntl
import json
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
from nexloop_eios.message_relay import MessageRelay, validate_recipe
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.run_credential_vault import RunCredentialVault

class _Parser(argparse.ArgumentParser):
    def error(self,unused): self.exit(2,'Message relay configuration unavailable\n')


def arguments(argv):
    parser=_Parser(description=__doc__)
    for name in ('database-url-file','signing-key-file','route-credential-file','source-credential-file',
        'planner-credential-file','executor-credential-file','artifact-root','vault-root','recipe-file'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--tick-seconds',type=float,default=.25)
    parser.add_argument('--once',action='store_true')
    # NX-023: explicit published strategy selects Context v6; omitted keeps the v2 Context.
    parser.add_argument('--context-strategy')
    result=parser.parse_args(argv)
    if not math.isfinite(result.tick_seconds) or not .01<=result.tick_seconds<=30 or not re.fullmatch('[A-Za-z0-9_-]{1,64}',result.signing_key_id): parser.error('configuration')
    if result.context_strategy is not None and not re.fullmatch('[a-z][a-z0-9_]{0,63}',result.context_strategy): parser.error('configuration')
    return result


def _recipe(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError()
            result[key]=value
        return result
    def constant(unused): raise ValueError()
    return validate_recipe(json.loads(read_private_text(path,maximum=32768),object_pairs_hook=pairs,parse_constant=constant))


def run(config,stop):
    dsn=read_private_text(config.database_url_file,maximum=16384)
    initial_recipe=_recipe(config.recipe_file)
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
            with open_backend(database_url=dsn,artifact_root=config.artifact_root,
                signing_key_file=config.signing_key_file,signing_key_id=config.signing_key_id) as backend:
                with backend._pool.connection() as db:
                    if verify_application_role(db)!='nexloop_api': raise ValueError()
                def services():
                    return [backend.authenticate(read_private_text(path,maximum=16384),world='real') for path in
                        (config.route_credential_file,config.source_credential_file,config.planner_credential_file)]
                services(); backend.authenticate(read_private_text(config.executor_credential_file,maximum=16384),world='real')
                print('Message relay ready',flush=True)
                while not stop.is_set():
                    try:
                        recipe=_recipe(config.recipe_file)
                        # Rotation cannot alter stable business assignments in place.
                        if recipe!=initial_recipe: raise ValueError()
                        route,source,planner=services()
                        executor=read_private_text(config.executor_credential_file,maximum=16384)
                        vault._check()
                        if stop.is_set(): break
                        status=MessageRelay(route=route,source=source,planner=planner,
                            executor_token=executor,vault=vault,recipe=recipe,context_strategy=config.context_strategy).run_once()
                        if config.once:
                            if status not in {'idle','queued','requires_governed_replan'}: raise ValueError()
                            print(json.dumps({'status':status},separators=(',',':')),flush=True)
                            return 0
                    except Exception:
                        if config.once:
                            print('Message relay unavailable',file=sys.stderr,flush=True);return 1
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
