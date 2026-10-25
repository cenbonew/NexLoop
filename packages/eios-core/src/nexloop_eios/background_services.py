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
``nexloop-retention-keeper``          (nexloop_domain_worker) due retention classes → bounded SQL sweep passes (redaction /
                                       deletion with tombstones, NX-029); no table privilege, no content
``nexloop-alert-evaluator``          (nexloop_domain_worker) alert rules on the metrics snapshot → deduplicated alert state/events
                                       (NX-030; world real only), sample rollup and retention
``nexloop-incentive-issuer``         (nexloop_domain_worker) accepted incentive intents → admitted at dispatch (controls, contact
                                       restriction, catalog, per-Consumer limit, tenant budget) → loopback ledger once; unknown
                                       answers queried, never re-sent (AT-043; D9: no real incentive channel)

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
    'retention-keeper':('Retention Keeper','nexloop_domain_worker'),
    'alert-evaluator':('Alert Evaluator','nexloop_domain_worker'),
    'incentive-issuer':('Incentive Issuer','nexloop_domain_worker'),
}
RELAY_CREDENTIALS=('route','source','planner','executor')
# NX-024/025: the reevaluator's Role launch runs as these API-side service identities (each its own credential file).
LAUNCH_CREDENTIALS=('source','planner','queue','executor')
# NX-031 D7 (deploy/stage/connection-budget.v1.json): each background service is one single-loop process; its pool holds at
# most POOL_MAX connections, and the Role / fallback launcher's second (nexloop_api) pool at most API_POOL_MAX. Measured: every
# service's functional tests pass with one connection; a launch needs two connections of its pool at once (with one, no Run is
# issued), so API_POOL_MAX is 2.
POOL_MAX=1
API_POOL_MAX=2
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
    parser.add_argument('--pool-max',type=int,default=POOL_MAX)
    if service in ('plan-reevaluator','reply-guarantor'):
        parser.add_argument('--api-pool-max',type=int,default=API_POOL_MAX)
    if service=='claim-extraction-scheduler':
        parser.add_argument('--quiet-seconds',type=int,default=5);parser.add_argument('--window',type=int,default=50)
        parser.add_argument('--max-attempts',type=int,default=3)
    if service=='claim-extraction-worker':
        parser.add_argument('--model-env-file',type=Path,required=True);parser.add_argument('--timezone',default='Asia/Shanghai')
        # NX-031: `test` admits the load lane's load-rules profile variant; the default refuses it at startup.
        parser.add_argument('--mode',choices=['stage','test'],default='stage')
    if service=='claim-matcher':
        parser.add_argument('--model-env-file',type=Path,required=True);parser.add_argument('--match-config-file',type=Path,required=True)
        parser.add_argument('--embedding-env-file',type=Path);parser.add_argument('--embedding-tariff-file',type=Path)
    if service=='recall-indexer':
        parser.add_argument('--types-file',type=Path,required=True);parser.add_argument('--embedding-env-file',type=Path)
        parser.add_argument('--embedding-tariff-file',type=Path)
    if service=='plan-reevaluator':
        parser.add_argument('--settings-file',type=Path,required=True);parser.add_argument('--api-database-url-file',type=Path,required=True)
        for name in LAUNCH_CREDENTIALS:parser.add_argument(f'--{name}-credential-file',type=Path,required=True)
        parser.add_argument('--effect-action',required=True)
    if service in ('commitment-keeper','commercial-recorder'):
        parser.add_argument('--settings-file',type=Path,required=True)
    if service=='retention-keeper':
        # NX-029 D8: the Agent Host's loopback runs/purge (all three or none; without them Run files stay queued).
        parser.add_argument('--host-port',type=int);parser.add_argument('--host-key-file',type=Path);parser.add_argument('--host-ca-file',type=Path)
    if service=='incentive-issuer':
        # AT-043 D9: the loopback ledger is the only provider (a private, writable directory of fsynced records).
        parser.add_argument('--ledger-directory',type=Path,required=True)
    if service=='reply-guarantor':
        parser.add_argument('--policy-file',type=Path,required=True);parser.add_argument('--api-database-url-file',type=Path,required=True)
        # Relay multi-consumer D4: one consumer's recipe, or the deployment Run profile with enrolled consumers (0160).
        mode=parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--recipe-file',type=Path)
        mode.add_argument('--run-profile-file',type=Path)
        for name in RELAY_CREDENTIALS:parser.add_argument(f'--{name}-credential-file',type=Path,required=True)
    return parser


def _arguments(service,argv):
    parser=_parser(service);arguments=parser.parse_args(argv)
    if (not math.isfinite(arguments.tick_seconds) or not 0<arguments.tick_seconds<=60
        or re.fullmatch('[A-Za-z0-9_.:-]{1,64}',arguments.signing_key_id) is None
        or not 1<=arguments.pool_max<=8 or not 1<=getattr(arguments,'api_pool_max',1)<=8):parser.error('configuration')
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


def embedding_budget(pool,session,arguments,embedding,service):
    """0219: the tenant model budget for this service's embedding calls; None without embedding (FTS + trgm mode)."""
    if embedding is None:return None
    from nexloop_eios.embedding_budget import EmbeddingBudget,load_tariff
    if arguments.embedding_tariff_file is None:raise ValueError('embedding requires --embedding-tariff-file')
    return EmbeddingBudget(pool,session,tariff=load_tariff(arguments.embedding_tariff_file,model=embedding.model),service=service)


class LazyModelProvider:
    """Built from the private model env file at first use. A missing/test profile fails the task
    with the given provider error (retried, visible in backlog), never invents output."""

    def __init__(self,path,unavailable,*,allow_load_rules=False):
        self.path,self.unavailable,self._provider,self.allow_load_rules=path,unavailable,None,allow_load_rules

    def _resolve(self):
        if self._provider is None:
            from nexloop_eios.conversation_extraction import OpenAICompatibleExtractionProvider
            from nexloop_eios.model_profile import load_model_profile
            try:
                profile=load_model_profile({},env_file=self.path,allow_load_rules=self.allow_load_rules)
                if profile.model_id=='load-rules':
                    from nexloop_eios.load_rules import LoadRulesExtractionProvider
                    self._provider=LoadRulesExtractionProvider()
                else:self._provider=OpenAICompatibleExtractionProvider(profile)
            except Exception:raise self.unavailable('model profile unavailable') from None
        return self._provider

    # The extractor records which provider and model produced a window (input digest): resolved on first use too.
    @property
    def provider(self):return self._resolve().provider

    @property
    def model_id(self):return self._resolve().model_id

    def complete(self,system_prompt,user_payload):
        provider=self._resolve()
        try:return provider.complete(system_prompt,user_payload)
        except Exception as error:
            if isinstance(error,self.unavailable):raise
            raise self.unavailable('model call failed') from None


def _tick(service,arguments,pool,session,signer,launcher=None):
    if service=='retention-keeper':
        from nexloop_eios.retention import RetentionKeeper
        return RetentionKeeper(pool,session,signer,**(launcher or {})).run_once()
    if service=='incentive-issuer':
        from nexloop_eios.incentives import IncentiveIssuer,LocalIncentiveLedger
        return IncentiveIssuer(pool,session,signer,LocalIncentiveLedger(arguments.ledger_directory)).run_once()
    if service=='alert-evaluator':
        # NX-030: alert rules on the SQL metrics snapshot (world real only, D6); also rolls up and expires process samples (D5).
        from nexloop_eios.observability import AlertEvaluator
        return AlertEvaluator(pool,session,signer).run_once()
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
        summary=PlanReevaluationWorker(pool,session,signer,settings=load_settings(arguments.settings_file),launcher=launcher).run_once()
        # NX-057 R1: the same process proposes remediations of final effect failures (once a minute; REMEDIATION_ENABLED gates it).
        from nexloop_eios.remediation import RemediationPlanner
        summary['remediation']=RemediationPlanner(pool,session,signer).run_once()
        return summary
    if service=='claim-extraction-scheduler':
        from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler
        return {'enqueued':len(ClaimExtractionScheduler(pool,session,signer,quiet_seconds=arguments.quiet_seconds,window=arguments.window,
            max_attempts=arguments.max_attempts).run_once())}
    if service=='claim-extraction-worker':
        from nexloop_eios.claim_extraction_jobs import ClaimExtractionWorker
        from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable
        status=ClaimExtractionWorker(pool,session,signer,LazyModelProvider(arguments.model_env_file,ExtractionProviderUnavailable,
            allow_load_rules=arguments.mode=='test'),
            timezone=arguments.timezone).run_once()
        return {'status':status if status in {'idle','succeeded','failed','retry_wait','dead_lettered'} else 'unavailable'}
    if service=='claim-matcher':
        from nexloop_eios.candidate_merge import CandidateGluer
        from nexloop_eios.claim_match_worker import ClaimMatchWorker
        from nexloop_eios.claim_matching import ClaimMatcher,MatchProviderUnavailable
        from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallIndexer
        embedding,dimension=load_embedding(arguments.embedding_env_file)
        budget=embedding_budget(pool,session,arguments,embedding,'claim-matcher')
        if budget is not None:
            if not budget.allowed():return {'status':'embedding_budget_closed'}
            embedding=budget.wrap(embedding)
        recall=OntologyRecall(pool,session,authorizer=EiosRecallAuthorizer(pool,session),provider=embedding,expected_dimension=dimension)
        matcher=ClaimMatcher(pool,session,signer,recall=recall,provider=LazyModelProvider(arguments.model_env_file,MatchProviderUnavailable),
            configuration=load_match_configuration(arguments.match_config_file))
        gluer=CandidateGluer(pool,session,signer,indexer=RecallIndexer(pool,session,provider=embedding,expected_dimension=dimension),
            matcher=matcher,provider=embedding)
        return ClaimMatchWorker(pool,session,signer,matcher=matcher,gluer=gluer).run_once()
    from nexloop_eios.recall_index_worker import RecallInstanceIndexWorker
    embedding,dimension=load_embedding(arguments.embedding_env_file)
    budget=embedding_budget(pool,session,arguments,embedding,'recall-indexer')
    if budget is not None:
        # PR #8 review: a model budget outside its period or used up claims no work at all (no item is consumed).
        if not budget.allowed():return {'status':'embedding_budget_closed'}
        embedding=budget.wrap(embedding)
    return RecallInstanceIndexWorker(pool,session,signer,types=load_types(arguments.types_file),provider=embedding,expected_dimension=dimension).run_once()


class TickFailures:
    """A failed tick used to be swallowed without a trace: a service whose every tick failed (authentication, authority, SQL)
    stayed `running` with nothing in its log (2026-10-18 stage: the alert evaluator's state stopped at 2026-10-17 14:33Z). Now
    the first failure, every `every`-th consecutive one and the recovery are one structured line each: the exception class
    and SQLSTATE (structured_log.failure_detail) and the count, never a message or a value (AT-049)."""

    def __init__(self,service,*,every=30,logger=None):
        self.service,self.every,self.count=service,every,0
        self.logger=logger or logging.getLogger('nexloop.background')

    def failed(self,error):
        from nexloop_eios.structured_log import failure_detail
        self.count+=1
        if self.count==1 or self.count%self.every==0:
            self.logger.warning('background tick failed %s',{**failure_detail(error),'stage':'tick','count':self.count})

    def succeeded(self):
        if self.count:
            self.logger.warning('background tick recovered %s',{'stage':'tick','count':self.count})
        self.count=0


def run(service,arguments,stop):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text
    label,role=SERVICES[service]
    if service in ('claim-extraction-worker','claim-matcher','recall-indexer'):
        # D2 (eios-capability-gap): their model and embedding calls run in forkserver children (nexloop_eios.deadline_http); the
        # forkserver starts now, before this process reads a credential or opens a connection, so no child can inherit one.
        from nexloop_eios.deadline_http import ensure_request_runtime_ready
        ensure_request_runtime_ready()
    if service in ('claim-extraction-worker','claim-matcher'):
        # NX-031: the load-rules profile variant starts only for the extraction worker in --mode test; anywhere else it is
        # refused here, before any connection (a stage/real service never runs on it).
        from nexloop_eios.provider_credentials import configuration_values
        if configuration_values({},env_file=arguments.model_env_file,prefix='MODEL').get('MODEL_TEST_VARIANT','').strip() and \
                (service!='claim-extraction-worker' or arguments.mode!='test'):
            raise ValueError('load-rules profile is test mode only')
    dsn=read_private_text(arguments.database_url_file,maximum=16384)
    read_private_text(arguments.service_credential_file,maximum=16384)
    if service=='recall-indexer':load_types(arguments.types_file)
    if service in ('recall-indexer','claim-matcher') and getattr(arguments,'embedding_env_file',None) is not None:
        # 0219: an enabled embedding needs its tariff (budget) before the first tick; an empty (disabled) env file needs none.
        embedding,_=load_embedding(arguments.embedding_env_file)
        if embedding is not None:
            from nexloop_eios.embedding_budget import load_tariff
            if arguments.embedding_tariff_file is None:raise ValueError('embedding requires --embedding-tariff-file')
            load_tariff(arguments.embedding_tariff_file,model=embedding.model)
    if service=='claim-matcher':load_match_configuration(arguments.match_config_file)
    if service=='commitment-keeper':
        from nexloop_eios.commitments import load_settings
        load_settings(arguments.settings_file)
    if service=='commercial-recorder':
        from nexloop_eios.commercial import load_settings
        load_settings(arguments.settings_file)
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=dsn,artifact_root=arguments.artifact_root,
            signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id,pool_max_size=arguments.pool_max))
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!=role:raise ValueError('restricted service role required')
        launcher=_launcher(stack,arguments) if service=='plan-reevaluator' else _reply_launcher(stack,arguments) if service=='reply-guarantor' else None
        if service=='retention-keeper':
            from nexloop_eios.retention import HostPurger
            host_args=(arguments.host_port,arguments.host_key_file,arguments.host_ca_file)
            if any(a is not None for a in host_args) and not all(a is not None for a in host_args):raise ValueError('host purge configuration')
            launcher={'artifact_store':backend._store,
                'host':HostPurger(port=arguments.host_port,key_file=arguments.host_key_file,ca_file=arguments.host_ca_file) if all(a is not None for a in host_args) else None}
        def services():
            # Re-read the credential and re-authenticate every tick: revocation applies at once.
            current=backend.authenticate(read_private_text(arguments.service_credential_file,maximum=16384),world=arguments.world)
            return current._backend._pool,current._session,current._backend._signer
        services()
        print(label+' ready',flush=True)
        failures=TickFailures(service)
        while not stop.is_set():
            try:
                summary=_tick(service,arguments,*services(),launcher=launcher)
                failures.succeeded()
                if arguments.once:
                    print(json.dumps({k:v for k,v in summary.items() if type(v) in (int,str)},separators=(',',':'),sort_keys=True),flush=True)
                    return 0
            except Exception as error:
                if arguments.once:
                    print(label+' unavailable',file=sys.stderr,flush=True);return 1
                failures.failed(error)
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
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id,pool_max_size=arguments.api_pool_max))
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
    if arguments.recipe_file is not None:mode={'recipe':json.loads(read_private_text(arguments.recipe_file,maximum=65536))}
    else:mode={'run_profile':json.loads(read_private_text(arguments.run_profile_file,maximum=65536))}
    api=stack.enter_context(open_backend(database_url=read_private_text(arguments.api_database_url_file,maximum=16384),artifact_root=arguments.artifact_root,
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id,pool_max_size=arguments.api_pool_max))
    with api._pool.connection() as connection:
        if verify_application_role(connection)!='nexloop_api':raise ValueError('restricted service role required')
    def session(name):
        return lambda:api.authenticate(read_private_text(getattr(arguments,name+'_credential_file'),maximum=16384),world=arguments.world)
    return FallbackReplyLauncher(route=session('route'),source=session('source'),planner=session('planner'),
        executor_token=lambda:read_private_text(arguments.executor_credential_file,maximum=16384),policy=policy,**mode)


def main_for(service,argv=None):
    # NX-030 / AT-049: every log record leaves this process as one allowlisted structured line (IDs, codes, durations only).
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging(service)
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
def retention_keeper(argv=None):return main_for('retention-keeper',argv)
def alert_evaluator(argv=None):return main_for('alert-evaluator',argv)
def incentive_issuer(argv=None):return main_for('incentive-issuer',argv)


if __name__=='__main__':
    if len(sys.argv)<2 or sys.argv[1] not in SERVICES:
        print('background service unavailable',file=sys.stderr);raise SystemExit(2)
    raise SystemExit(main_for(sys.argv[1],sys.argv[2:]))
