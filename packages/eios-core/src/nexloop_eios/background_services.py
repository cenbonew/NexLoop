"""Deployable process entries for the NX-019/020/021/045 background services.

``nexloop-claim-extraction-scheduler`` (nexloop_api)      feed windows → durable queue
``nexloop-claim-extraction-worker``    (nexloop_domain_worker) queue → model extraction → Claims
``nexloop-claim-matcher``              (nexloop_domain_worker) claim-match feed → match/apply → glue
``nexloop-recall-indexer``             (nexloop_domain_worker) recall-instance feed → instance index
``nexloop-plan-reevaluator``           (nexloop_domain_worker) plan-reevaluate feed → precheck → bounded Role Run
                                       (Role issuance and activation through a second, nexloop_api backend)
``nexloop-reply-guarantor``            (nexloop_domain_worker) reply-due feed → settled / one fallback reply Run / escalation;
                                       takeover-expiry feed → expired takeovers ended and escalated (NX-028)
                                       (fallback issuance with the consumer's message relay identities on a nexloop_api backend)
``nexloop-commitment-keeper``         (nexloop_domain_worker) commitment-register / commitment-monitor feeds → governed
                                       Commitment create / ledger-derived transitions, due stages, plan marking, exceptions
``nexloop-commercial-recorder``       (nexloop_domain_worker) commercial-record feed → governed CommercialRecord create /
                                       derived edits, metric observations, plan marking, commitment evidence (NX-027);
                                       --world real or test (a test connector only writes the test world)

Each process never migrates a database. Secrets come only from explicitly named private
files (DSN, signing key, service credential, optional model/embedding env files); the
process environment is never consulted for them. The service credential is re-read and
re-authenticated every tick, so revocation and authority changes apply at once. Errors
print a fixed line without arguments, DSNs, credentials or exception text; ``--once`` runs
one tick and prints a counts-only JSON summary.
"""
import argparse
from contextlib import ExitStack
import json
import logging
import math
from pathlib import Path
import re
import signal
import sys
import threading

SERVICES={
    'claim-extraction-scheduler':('Claim Extraction Scheduler','nexloop_api'),
    'claim-extraction-worker':('Claim Extraction Worker','nexloop_domain_worker'),
    'claim-matcher':('Claim Matcher','nexloop_domain_worker'),
    'recall-indexer':('Recall Indexer','nexloop_domain_worker'),
    'plan-reevaluator':('Plan Reevaluator','nexloop_domain_worker'),
    'reply-guarantor':('Reply Guarantor','nexloop_domain_worker'),
    'commitment-keeper':('Commitment Keeper','nexloop_domain_worker'),
    'commercial-recorder':('Commercial Recorder','nexloop_domain_worker'),
}
RELAY_CREDENTIALS=('route','source','planner','executor')
# NX-024/025: the reevaluator's Role launch runs as these API-side service identities (each its own credential file).
LAUNCH_CREDENTIALS=('source','planner','queue','executor')
_TYPE=re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')


def _parser(service):
    label=SERVICES[service][0]

    class _Parser(argparse.ArgumentParser):
        def error(self,message):self.exit(2,label+' configuration unavailable\n')

    parser=_Parser(description=f'Restricted standalone {label}; private configuration, no migrations')
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--world',required=True,choices=['real','test'] if service=='commercial-recorder' else ['real'])
    parser.add_argument('--tick-seconds',type=float,default=2.0)
    parser.add_argument('--once',action='store_true')
    if service=='claim-extraction-scheduler':
        parser.add_argument('--quiet-seconds',type=int,default=5);parser.add_argument('--window',type=int,default=50)
        parser.add_argument('--max-attempts',type=int,default=3)
    if service=='claim-extraction-worker':
        parser.add_argument('--model-env-file',type=Path,required=True);parser.add_argument('--timezone',default='Asia/Shanghai')
    if service=='claim-matcher':
        parser.add_argument('--model-env-file',type=Path,required=True);parser.add_argument('--match-config-file',type=Path,required=True)
        parser.add_argument('--embedding-env-file',type=Path)
    if service=='recall-indexer':
        parser.add_argument('--types-file',type=Path,required=True);parser.add_argument('--embedding-env-file',type=Path)
    if service=='plan-reevaluator':
        parser.add_argument('--settings-file',type=Path,required=True);parser.add_argument('--api-database-url-file',type=Path,required=True)
        for name in LAUNCH_CREDENTIALS:parser.add_argument(f'--{name}-credential-file',type=Path,required=True)
        parser.add_argument('--effect-action',required=True)
    if service in ('commitment-keeper','commercial-recorder'):
        parser.add_argument('--settings-file',type=Path,required=True)
    if service=='reply-guarantor':
        parser.add_argument('--policy-file',type=Path,required=True);parser.add_argument('--api-database-url-file',type=Path,required=True)
        parser.add_argument('--recipe-file',type=Path,required=True)
        for name in RELAY_CREDENTIALS:parser.add_argument(f'--{name}-credential-file',type=Path,required=True)
    return parser


def _arguments(service,argv):
    parser=_parser(service);arguments=parser.parse_args(argv)
    if (not math.isfinite(arguments.tick_seconds) or not 0<arguments.tick_seconds<=60
        or re.fullmatch('[A-Za-z0-9_.:-]{1,64}',arguments.signing_key_id) is None):parser.error('configuration')
    if service=='claim-extraction-scheduler' and (not 0<=arguments.quiet_seconds<=3600 or not 1<=arguments.window<=200 or not 1<=arguments.max_attempts<=10):
        parser.error('configuration')
    if service=='plan-reevaluator' and re.fullmatch(r'eios:action:[A-Za-z][A-Za-z0-9_.-]{0,127}:[1-9][0-9]{0,5}',arguments.effect_action) is None:
        parser.error('configuration')
    return arguments


def _json_file(path,maximum=65536):
    from nexloop_eios.private_configuration import read_private_text
    from nexloop_eios.trusted_configuration import strict_json
    return strict_json(read_private_text(path,maximum=maximum))


def load_types(path):
    """{type name: instance spec object or null}; specs are re-validated by SQL at indexing."""
    value=_json_file(path)
    if type(value) is not dict or not 1<=len(value)<=64:raise ValueError('types')
    for name,spec in value.items():
        if not _TYPE.fullmatch(name) or not (spec is None or type(spec) is dict):raise ValueError('types')
    return value


def load_match_configuration(path):
    from nexloop_eios.claim_matching import MatchConfiguration
    value=_json_file(path)
    if type(value) is not dict or set(value)-{'edit_actions','create_actions'}:raise ValueError('match configuration')
    actions={}
    for key in ('edit_actions','create_actions'):
        raw=value.get(key,{})
        if type(raw) is not dict or any(not _TYPE.fullmatch(t) or type(a) is not list or len(a)!=2 or type(a[0]) is not str or type(a[1]) is not int for t,a in raw.items()):
            raise ValueError('match configuration')
        actions[key]={t:(a[0],a[1]) for t,a in raw.items()}
    return MatchConfiguration(**actions)


def load_embedding(path):
    """Configured Ark embedding provider, or None (explicit FTS + trgm mode) when absent/disabled."""
    if path is None:return None,None
    from nexloop_eios.embedding_profile import load_embedding_profile
    from nexloop_eios.embedding_provider import ArkMultimodalEmbeddingProvider
    profile=load_embedding_profile({},env_file=path)
    if profile.validation_mode=='disabled':return None,None
    provider=ArkMultimodalEmbeddingProvider(profile)
    return provider,provider.dimension


class LazyModelProvider:
    """Built from the private model env file at first use. A missing/test profile fails the task
    with the given provider error (retried, visible in backlog), never invents output."""

    def __init__(self,path,unavailable):self.path,self.unavailable,self._provider=path,unavailable,None

    def complete(self,system_prompt,user_payload):
        if self._provider is None:
            from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable,OpenAICompatibleExtractionProvider
            from nexloop_eios.model_profile import load_model_profile
            try:self._provider=OpenAICompatibleExtractionProvider(load_model_profile({},env_file=self.path))
            except Exception:raise self.unavailable('model profile unavailable') from None
        try:return self._provider.complete(system_prompt,user_payload)
        except Exception as error:
            if isinstance(error,self.unavailable):raise
            raise self.unavailable('model call failed') from None


def _tick(service,arguments,pool,session,signer,launcher=None):
    if service=='commercial-recorder':
        from nexloop_eios.commercial import CommercialRecorder,load_settings
        return CommercialRecorder(pool,session,signer,settings=load_settings(arguments.settings_file)).run_once()
    if service=='commitment-keeper':
        from nexloop_eios.commitments import CommitmentKeeper,load_settings
        return CommitmentKeeper(pool,session,signer,settings=load_settings(arguments.settings_file)).run_once()
    if service=='reply-guarantor':
        from nexloop_eios.contact_restrictions import ReplyGuaranteeWorker,load_reply_policy
        summary=ReplyGuaranteeWorker(pool,session,signer,policy=load_reply_policy(arguments.policy_file),launcher=launcher).run_once()
        # NX-028 D3: the same process ends expired takeovers (D5 settlement, hand-back event, owner escalation).
        from nexloop_eios.takeovers import TakeoverExpiryWorker
        summary.update({'takeover_'+k:v for k,v in TakeoverExpiryWorker(pool,session,signer).run_once().items()})
        return summary
    if service=='plan-reevaluator':
        from nexloop_eios.plan_reevaluation import PlanReevaluationWorker,load_settings
        return PlanReevaluationWorker(pool,session,signer,settings=load_settings(arguments.settings_file),launcher=launcher).run_once()
    if service=='claim-extraction-scheduler':
        from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler
        return {'enqueued':len(ClaimExtractionScheduler(pool,session,signer,quiet_seconds=arguments.quiet_seconds,window=arguments.window,
            max_attempts=arguments.max_attempts).run_once())}
    if service=='claim-extraction-worker':
        from nexloop_eios.claim_extraction_jobs import ClaimExtractionWorker
        from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable
        status=ClaimExtractionWorker(pool,session,signer,LazyModelProvider(arguments.model_env_file,ExtractionProviderUnavailable),
            timezone=arguments.timezone).run_once()
        return {'status':status if status in {'idle','succeeded','failed','retry_wait','dead_lettered'} else 'unavailable'}
    if service=='claim-matcher':
        from nexloop_eios.candidate_merge import CandidateGluer
        from nexloop_eios.claim_match_worker import ClaimMatchWorker
        from nexloop_eios.claim_matching import ClaimMatcher,MatchProviderUnavailable
        from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallIndexer
        embedding,dimension=load_embedding(arguments.embedding_env_file)
        recall=OntologyRecall(pool,session,authorizer=EiosRecallAuthorizer(pool,session),provider=embedding,expected_dimension=dimension)
        matcher=ClaimMatcher(pool,session,signer,recall=recall,provider=LazyModelProvider(arguments.model_env_file,MatchProviderUnavailable),
            configuration=load_match_configuration(arguments.match_config_file))
        gluer=CandidateGluer(pool,session,signer,indexer=RecallIndexer(pool,session,provider=embedding,expected_dimension=dimension),
            matcher=matcher,provider=embedding)
        return ClaimMatchWorker(pool,session,signer,matcher=matcher,gluer=gluer).run_once()
    from nexloop_eios.recall_index_worker import RecallInstanceIndexWorker
    embedding,dimension=load_embedding(arguments.embedding_env_file)
    return RecallInstanceIndexWorker(pool,session,signer,types=load_types(arguments.types_file),provider=embedding,expected_dimension=dimension).run_once()


def run(service,arguments,stop):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text
    label,role=SERVICES[service]
    dsn=read_private_text(arguments.database_url_file,maximum=16384)
    read_private_text(arguments.service_credential_file,maximum=16384)
    if service=='recall-indexer':load_types(arguments.types_file)
    if service=='claim-matcher':load_match_configuration(arguments.match_config_file)
    if service=='commitment-keeper':
        from nexloop_eios.commitments import load_settings
        load_settings(arguments.settings_file)
    if service=='commercial-recorder':
        from nexloop_eios.commercial import load_settings
        load_settings(arguments.settings_file)
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=dsn,artifact_root=arguments.artifact_root,
            signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id))
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!=role:raise ValueError('restricted service role required')
        launcher=_launcher(stack,arguments) if service=='plan-reevaluator' else _reply_launcher(stack,arguments) if service=='reply-guarantor' else None
        def services():
            # Re-read the credential and re-authenticate every tick: revocation applies at once.
            current=backend.authenticate(read_private_text(arguments.service_credential_file,maximum=16384),world=arguments.world)
            return current._backend._pool,current._session,current._backend._signer
        services()
        print(label+' ready',flush=True)
        while not stop.is_set():
            try:
                summary=_tick(service,arguments,*services(),launcher=launcher)
                if arguments.once:
                    print(json.dumps({k:v for k,v in summary.items() if type(v) in (int,str)},separators=(',',':'),sort_keys=True),flush=True)
                    return 0
            except Exception:
                if arguments.once:
                    print(label+' unavailable',file=sys.stderr,flush=True);return 1
            stop.wait(arguments.tick_seconds)
        return 0


def _launcher(stack,arguments):
    """Role launch identities on a second, nexloop_api backend; every use re-reads and re-authenticates its credential."""
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.plan_reevaluation import RoleRunLauncher,load_settings
    from nexloop_eios.private_configuration import read_private_text
    settings=load_settings(arguments.settings_file)
    for name in LAUNCH_CREDENTIALS:read_private_text(getattr(arguments,name+'_credential_file'),maximum=16384)
    api=stack.enter_context(open_backend(database_url=read_private_text(arguments.api_database_url_file,maximum=16384),artifact_root=arguments.artifact_root,
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id))
    with api._pool.connection() as connection:
        if verify_application_role(connection)!='nexloop_api':raise ValueError('restricted service role required')
    def session(name):
        return lambda:api.authenticate(read_private_text(getattr(arguments,name+'_credential_file'),maximum=16384),world=arguments.world)
    return RoleRunLauncher(source=session('source'),queue_service=session('queue'),planner=session('planner'),
        executor_token=lambda:read_private_text(arguments.executor_credential_file,maximum=16384),settings=settings,effect_action=arguments.effect_action)


def _reply_launcher(stack,arguments):
    """Fallback reply issuance with the consumer's message relay identities on a nexloop_api backend. The reply policy is
    validated first: a fallback that could not start and finish inside the reply window refuses to start the process."""
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.contact_restrictions import load_reply_policy
    from nexloop_eios.private_configuration import read_private_text
    from nexloop_eios.reply_fallback import FallbackReplyLauncher
    policy=load_reply_policy(arguments.policy_file)
    for name in RELAY_CREDENTIALS:read_private_text(getattr(arguments,name+'_credential_file'),maximum=16384)
    recipe=json.loads(read_private_text(arguments.recipe_file,maximum=65536))
    api=stack.enter_context(open_backend(database_url=read_private_text(arguments.api_database_url_file,maximum=16384),artifact_root=arguments.artifact_root,
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id))
    with api._pool.connection() as connection:
        if verify_application_role(connection)!='nexloop_api':raise ValueError('restricted service role required')
    def session(name):
        return lambda:api.authenticate(read_private_text(getattr(arguments,name+'_credential_file'),maximum=16384),world=arguments.world)
    return FallbackReplyLauncher(route=session('route'),source=session('source'),planner=session('planner'),
        executor_token=lambda:read_private_text(arguments.executor_credential_file,maximum=16384),recipe=recipe,policy=policy)


def main_for(service,argv=None):
    arguments=_arguments(service,argv);stop=threading.Event();previous={}
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    def terminate(signum,frame):stop.set()
    try:
        for signum in (signal.SIGTERM,signal.SIGINT):previous[signum]=signal.signal(signum,terminate)
        return run(service,arguments,stop)
    except Exception:
        print(SERVICES[service][0]+' unavailable',file=sys.stderr,flush=True);return 1
    finally:
        logger.disabled=disabled
        for signum,handler in previous.items():signal.signal(signum,handler)


def claim_extraction_scheduler(argv=None):return main_for('claim-extraction-scheduler',argv)
def claim_extraction_worker(argv=None):return main_for('claim-extraction-worker',argv)
def claim_matcher(argv=None):return main_for('claim-matcher',argv)
def recall_indexer(argv=None):return main_for('recall-indexer',argv)
def plan_reevaluator(argv=None):return main_for('plan-reevaluator',argv)
def reply_guarantor(argv=None):return main_for('reply-guarantor',argv)
def commitment_keeper(argv=None):return main_for('commitment-keeper',argv)
def commercial_recorder(argv=None):return main_for('commercial-recorder',argv)


if __name__=='__main__':
    if len(sys.argv)<2 or sys.argv[1] not in SERVICES:
        print('background service unavailable',file=sys.stderr);raise SystemExit(2)
    raise SystemExit(main_for(sys.argv[1],sys.argv[2:]))
