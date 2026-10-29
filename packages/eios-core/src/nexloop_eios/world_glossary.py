"""World-model glossary (owner 2026-10-29; docs/design/world-model-glossary.md, migration 0374).

Chinese glosses for the English identifiers of the enterprise world model, for display only. Three writers:

* publish_glossary: the owner's manual manifest (deploy/configuration/world-model-glossary.v<N>.json) through the configurator
  window, from the file's exact text (sha256 and expected revision checked in SQL).
* WorldGlossaryTranslator: the world-glossary-translator service. It finds identifiers with no gloss, reserves one call of the tenant's
  daily quota *before* calling the model (I5), sends only the identifier, its kind, owning type, description and display name (at most
  200 characters each) and the tenant's object type glosses as terms, and records the answer (never over a manual gloss), the tokens and
  the cost (configured_rate from the model tariff, unpriced without one). Through one signed service port only.
* nexloop.world_glossary.set: the owner's correction through the governed entry (goal_controls.set_world_glossary).
"""
from datetime import UTC,datetime
from decimal import Decimal,ROUND_HALF_UP
import hashlib
import hmac
import json
import re

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import authority_request_scoped,resolve_authority
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.signing import SIGNED_COMMAND_TTL

SCHEMA_VERSION='nexloop-world-model-glossary/1'
GROUPS={'sections':'section','object_types':'object_type','properties':'property','relations':'relation','event_types':'event_type',
        'functions':'function','actions':'action'}
TRANSLATE_ACTION='nexloop.world_glossary.translate'
PROTOCOL='nexloop-world-glossary-v1'
PER_ROUND=10
# One --once run starts no new model call after RUN_SECONDS (each call is bounded by the provider's 60 s deadline), so a run ends
# within about 5 minutes, half the 10-minute timer period.
RUN_SECONDS=240
CONTEXT_LIMIT=200
PROMPT_VERSION='world-glossary-1'
SYSTEM_PROMPT=('你为企业运营系统的数据模型标识写简短的中文名称，供界面在英文标识后用括号显示，例如 Consumer（客户）。'
    '只依据给出的标识名、种类、所属类型、说明和术语表；术语表里已有的词必须沿用。'
    '只输出 JSON：{"gloss": "中文名称"}。中文名称 1 到 24 个字，不含括号，不重复英文标识，不加解释。')
_CJK=re.compile(r'[一-鿿]')


class GlossaryRejected(ValueError):
    pass


class GlossaryPortDenied(PermissionError):
    pass


def valid_gloss(value):
    """The SQL rule (control.nexloop_world_glossary_valid): '' or 1-24 characters with Chinese, trimmed, no ASCII parenthesis or line break."""
    return type(value) is str and (value=='' or (len(value)<=24 and bool(_CJK.search(value)) and value==value.strip()
        and not re.search(r'[()\n\r]',value)))


def validate_manifest(manifest):
    if type(manifest) is not dict or manifest.get('schema_version')!=SCHEMA_VERSION:raise GlossaryRejected('schema_version')
    version=manifest.get('manifest_version')
    if type(version) is not int or not 1<=version<=999999999:raise GlossaryRejected('manifest_version')
    extra=set(manifest)-{'schema_version','manifest_version','decision','source',*GROUPS}
    if extra:raise GlossaryRejected('unknown keys '+', '.join(sorted(extra)))
    for group in GROUPS:
        entries=manifest.get(group,{})
        if type(entries) is not dict:raise GlossaryRejected(group)
        for identifier,gloss in entries.items():
            if not 1<=len(identifier)<=200 or not (gloss is None or valid_gloss(gloss)):raise GlossaryRejected(f'{group}: {identifier}')
    return manifest


def entry_count(manifest):
    return sum(len(manifest.get(group,{})) for group in GROUPS)


def publish_glossary(text,tenant,*,expected_revision,database_url_file):
    """Technical configurator only (SQL refuses any other session); the file's exact text, its sha256 and the expected revision."""
    from nexloop_eios.trusted_configuration import configurator_connection
    validate_manifest(json.loads(text))
    if type(expected_revision) is not int or expected_revision<0:raise GlossaryRejected('expected revision')
    db=configurator_connection(database_url_file)
    with db,db.transaction():
        db.execute("set local statement_timeout='30000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_publish_world_glossary(%s,%s,%s,%s)',
            (tenant,text,hashlib.sha256(text.encode('utf-8')).hexdigest(),expected_revision)).fetchone()[0]


def load_tariff(path,*,model):
    """deploy/configuration/model-tariffs/*.json (nexloop.model-tariff.v1): the applied period's input/output rate per million tokens.
    None without a file: the call is then recorded unpriced, tokens only (S6) - never estimated."""
    if path is None:return None
    value=json.loads(open(path,encoding='utf-8').read())
    if (type(value) is not dict or value.get('schema')!='nexloop.model-tariff.v1' or value.get('unit')!='per_million_tokens'
            or value.get('model')!=model or not re.fullmatch(r'[A-Z]{3}',str(value.get('currency','')))):
        raise GlossaryRejected('model tariff')
    rates=value.get('periods',{}).get(value.get('applied_period'),value.get('rates'))
    if type(rates) is not dict or not all(type(rates.get(k)) in (int,float) and rates[k]>=0 for k in ('input','output')):
        raise GlossaryRejected('model tariff')
    return {'currency':value['currency'],'input':Decimal(str(rates['input'])),'output':Decimal(str(rates['output']))}


def price(tariff,usage):
    if tariff is None or usage is None:return None
    amount=(tariff['input']*usage['prompt_tokens']+tariff['output']*usage['completion_tokens'])/Decimal(1000000)
    return {'amount':str(amount.quantize(Decimal('0.00000001'),rounding=ROUND_HALF_UP)),'currency':tariff['currency']}


class GlossaryPort:
    """The signed service port authz.nexloop_world_glossary_port under nexloop.world_glossary.translate (as the claim feed 0069)."""
    def __init__(self,pool,session,signer):
        if session.run_context is not None:raise GlossaryPortDenied()
        self.pool,self.session,self.signer=pool,session,signer

    def call(self,verb,**parameters):
        target=resource_id(ResourceType.ACTION,TRANSLATE_ACTION,1);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,query,entries))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise GlossaryPortDenied()
        body=canonical_payload({'verb':verb,**parameters});auth=self.session.authentication
        claims={'protocol':PROTOCOL,'key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,
            'credential_id':auth.credential_id,'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,
            'action_resource':target,'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+SIGNED_COMMAND_TTL).isoformat(),
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_world_glossary_port(%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]


def model_input(item,terms):
    """Exactly what the model sees: identifier metadata (cut to 200 characters) and the terms. Nothing of any instance."""
    cut=lambda value:None if value is None else str(value)[:CONTEXT_LIMIT]
    return canonical_payload({'identifier':item['identifier'],'kind':item['kind'],'owner_type':item.get('owner_type'),
        'description':cut(item.get('description')),'display_name':cut(item.get('display_name')),'terms':terms,'output':'JSON only'})


def parse_gloss(text,identifier):
    try:value=json.loads(text)
    except (TypeError,ValueError):return None
    gloss=value.get('gloss') if type(value) is dict else None
    if (not valid_gloss(gloss) or gloss=='' or re.search(r'[（）]',gloss) or identifier.lower() in gloss.lower()):return None
    return gloss


class WorldGlossaryTranslator:
    def __init__(self,pool,session,signer,provider,*,tariff=None,per_round=PER_ROUND,run_seconds=RUN_SECONDS,unavailable=Exception,clock=None):
        import time
        if not 1<=per_round<=PER_ROUND or not 0<run_seconds<=RUN_SECONDS:raise ValueError('per_round')
        self.run_seconds,self.clock=run_seconds,clock or time.monotonic
        self.port=GlossaryPort(pool,session,signer)
        self.provider,self.tariff,self.per_round,self.unavailable=provider,tariff,per_round,unavailable

    @authority_request_scoped
    def run_once(self):
        summary={'enqueued':self.port.call('scan')['enqueued'],'translated':0,'failed':0,'manual_kept':0,'quota':'open'}
        started=self.clock()
        due=self.port.call('due',limit=self.per_round)
        for item in due['items']:
            if self.clock()-started>=self.run_seconds:summary['stopped']='run_time';break  # the rest waits for the next run
            reserved=self.port.call('reserve',kind=item['kind'],identifier=item['identifier'])
            if not reserved['allowed']:
                if reserved.get('reason')=='full':summary['quota']='full';break
                continue
            payload=model_input(item,due['terms'])
            outcome={'provider':self.provider.provider,'model_id':self.provider.model_id,
                     'input_sha256':hashlib.sha256((PROMPT_VERSION+'\n'+SYSTEM_PROMPT+'\n'+payload).encode()).hexdigest()}
            usage=None
            try:
                text=self.provider.complete(SYSTEM_PROMPT,payload)
                usage=_usage(getattr(self.provider,'last_usage',None))
                gloss=parse_gloss(text,item['identifier'])
                outcome.update({'gloss':gloss} if gloss is not None else {'error_code':'response_parse'})
            except self.unavailable as error:
                code=getattr(error,'code','unavailable')
                outcome['error_code']=code if code in ('http_429','http_5xx','http_4xx','timeout','transport','response_parse','profile') else 'unavailable'
            if usage is not None:outcome.update({'prompt_tokens':usage['prompt_tokens'],'completion_tokens':usage['completion_tokens']})
            cost=price(self.tariff,usage)
            if cost is not None:outcome.update(cost)
            recorded=self.port.call('record',usage_id=reserved['usage_id'],**outcome)
            if 'gloss' in outcome:summary['translated' if recorded['written'] else 'manual_kept']+=1
            else:summary['failed']+=1
        return summary


def _usage(value):
    if type(value) is not dict:return None
    prompt,completion=value.get('prompt_tokens'),value.get('completion_tokens')
    if type(prompt) is not int or type(completion) is not int or prompt<0 or completion<0:return None
    return {'prompt_tokens':prompt,'completion_tokens':completion}
