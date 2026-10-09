"""NX-021 / ADR-019 §3.2 hybrid recall over Schema definitions and object instances.

Tenant comes from the authenticated credential inside PostgreSQL; world from the
server session. Readable types/properties are decided through EIOS before any
score is computed (pre-filter) and every returned hit is re-checked (return
check); instance hits additionally need current object and property READ.
Results carry refs and scores only, never indexed text. The index is a
rebuildable derived layer; reflow functions are for NX-045/046 publish/merge.
"""
from dataclasses import dataclass,field
import re
from types import MappingProxyType
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from nexloop_eios.assembly import verify_application_role
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import authority_request_scoped,authorization_service,resolve_authority
from nexloop_eios.embedding_provider import EmbeddingDimensionMismatch,checked_vector

METHODS=('vector','fts','trgm')
TOKENIZER_VERSION='nexloop-cjk-bigram-v1'
_NAME=re.compile(r'[A-Za-z][A-Za-z0-9_]*')


def vocabulary_ref(type_name,property_name,value):
    """Same encoding as ontology.nexloop_recall_ref_part (whitespace and %)."""
    text=str(value)
    for char,code in (('%','%25'),(' ','%20'),('\t','%09'),('\n','%0A'),('\r','%0D'),('\f','%0C'),('\v','%0B')):
        text=text.replace(char,code)
    return f'nexloop:vocabulary:{type_name}/{property_name}/{text}'


@dataclass(frozen=True)
class RecallConfig:
    top_k:int=5
    candidate_limit:int=20
    weights:MappingProxyType=field(default_factory=lambda:MappingProxyType({'vector':0.5,'fts':0.2,'trgm':0.3}))
    min_score:float=0.0
    config_version:str='nx021-recall-v1'

    def __post_init__(self):
        if (type(self.top_k) is not int or not 1<=self.top_k<=50 or type(self.candidate_limit) is not int
                or not self.top_k<=self.candidate_limit<=200 or set(self.weights)!=set(METHODS)
                or any(type(w) not in (int,float) or w<0 for w in self.weights.values()) or not sum(self.weights.values())>0
                or not 0<=self.min_score<=1):
            raise ValueError('invalid recall configuration')
        object.__setattr__(self,'weights',MappingProxyType(dict(self.weights)))


@dataclass(frozen=True)
class RecallHit:
    ref:str
    method:str
    score:float
    kind:str
    method_scores:MappingProxyType
    fields:tuple

    def contract(self):
        """candidate-definition.recall item."""
        return {'ref':self.ref,'method':self.method,'score':round(self.score,6)}


@dataclass(frozen=True)
class RecallResult:
    definitions:tuple
    instances:tuple
    methods:tuple
    profile_id:str|None
    config_version:str
    tokenizer_version:str=TOKENIZER_VERSION

    def contract(self):
        return [hit.contract() for hit in self.definitions+self.instances]


class RecallUnavailable(RuntimeError):
    """Authority could not be evaluated (stale session, outage): never reported as an empty recall."""


class EiosRecallAuthorizer:
    """Each resource is a full EIOS READ decision.

    A negative or unresolvable decision (e.g. no grant facts) excludes that
    resource. The session itself is checked current before and after each
    batch: a credential made stale (e.g. by a Schema publish) aborts the recall
    so callers never mistake it for "no match".
    """

    def __init__(self,pool,session):
        self.pool,self.session=pool,session;self._service=authorization_service(pool,session)

    def _assert_current(self):
        try:
            with self.pool.connection() as connection,connection.transaction():
                verify_application_role(connection)
                live=connection.execute('select authz.nexloop_service_identity_snapshot(%s,%s)',(self.session.token_digest,self.session.world)).fetchone()[0]
        except Exception:
            raise RecallUnavailable('recall authority unavailable') from None
        if (not live or live.get('directory_hash')!=self.session.directory_hash
                or live.get('binding',{}).get('tenant_id')!=self.session.authentication.tenant_id):
            raise RecallUnavailable('recall session is stale; re-authenticate')

    def readable(self,kind,names):
        self._assert_current();allowed=set()
        for name in sorted(set(names)):
            try:
                decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,self.session.query(resource_id=resource_id(kind,name),resource_type=kind,operation=Operation.READ)))
                if decision.allowed and decision.authoritative and not decision.obligations:allowed.add(name)
            except Exception:
                continue
        self._assert_current()
        return frozenset(allowed)


def _provider_dimension(provider,expected_dimension):
    if provider is None:return None
    if type(expected_dimension) is not int or provider.dimension!=expected_dimension:
        raise EmbeddingDimensionMismatch('embedding provider dimension differs from configured EMBEDDING_DIMENSION')
    return expected_dimension


def _vector_literal(vector,dimension):
    return '['+','.join(repr(v) for v in checked_vector(vector,dimension))+']'


class _Database:
    def __init__(self,pool,session):self.pool,self.session=pool,session

    def call(self,function,*args):
        with self.pool.connection() as connection,connection.transaction():
            verify_application_role(connection)
            placeholders=','.join(['%s']*(len(args)+2))
            return connection.execute(f'select control.{function}({placeholders})',(self.session.token_digest,self.session.world,*args)).fetchone()[0]


class OntologyRecall:
    """Definition recall (types/properties/vocabulary/aliases) and instance recall."""

    def __init__(self,pool,session,*,authorizer,provider=None,expected_dimension=None,config=RecallConfig()):
        self._db=_Database(pool,session);self.session=session;self.authorizer=authorizer
        self.provider=provider;self.dimension=_provider_dimension(provider,expected_dimension);self.config=config

    def _gates(self,kind):
        gates=self._db.call('nexloop_recall_gates',kind)
        types=self.authorizer.readable(ResourceType.OBJECT_TYPE,gates['types'])
        props=self.authorizer.readable(ResourceType.PROPERTY,[f'{t}/{p}' for t,p in gates['properties'] if t in types])
        return types,props

    def _search(self,kind,method,text,types,props,*,vector=None,strong_ids=(),verified_refs=(),type_names=None):
        request={'index_kind':kind,'method':method,'limit':self.config.candidate_limit,'text':text,
            'readable_types':sorted(types),'readable_properties':sorted(props),
            'type_names':None if type_names is None else sorted(type_names)}
        if method=='vector':
            request.update(vector=vector,profile_id=self.provider.profile_id,dimension=self.dimension)
        if method=='strong_id':
            request.update(strong_ids=list(strong_ids),verified_refs=list(verified_refs))
        rows=self._db.call('nexloop_recall_search',Jsonb(request))
        tenant=self.session.authentication.tenant_id
        checked=[]
        for row in rows:
            # Return check: nothing outside the session tenant/world or readable gates.
            if (row['tenant_id']!=tenant or (row['world'] is not None and row['world']!=self.session.world)
                    or (kind=='instance' and row['world']!=self.session.world) or row['gate_type'] not in types
                    or (row['gate_property'] is not None and f"{row['gate_type']}/{row['gate_property']}" not in props)
                    or (type_names is not None and row['gate_type'] not in type_names)):
                raise PermissionError('recall return check failed')
            checked.append(row)
        return checked

    def _fuse(self,kind,per_method,methods):
        weights={m:self.config.weights[m] for m in methods};total=sum(weights.values()) or 1.0
        merged={}
        for method,rows in per_method.items():
            for row in rows:
                entry=merged.setdefault(row['ref'],{'scores':{},'fields':set(),'gates':set()})
                entry['scores'][method]=max(entry['scores'].get(method,0.0),float(row['score']))
                entry['fields'].add(row['field']);entry['gates'].add((row['gate_type'],row['gate_property'],row.get('object_id')))
        hits=[]
        for ref,entry in merged.items():
            scores=entry['scores']
            if 'strong_id' in scores:
                score,method=1.0,'strong_id'
            else:
                contributions={m:weights[m]*scores.get(m,0.0) for m in methods}
                score=min(1.0,sum(contributions.values())/total)
                method=max(contributions,key=lambda m:(contributions[m],m))
            if score<self.config.min_score and method!='strong_id':continue
            hit=RecallHit(ref,method,score,kind,MappingProxyType(dict(sorted(scores.items()))),tuple(sorted(entry['fields'])))
            hits.append((hit,frozenset(entry['gates'])))
        hits.sort(key=lambda pair:(pair[0].method!='strong_id',-pair[0].score,pair[0].ref))
        return hits

    def _instance_post_check(self,hits):
        """Current object READ and READ of every matched property, per instance."""
        visible=[]
        for hit,gates in hits:
            objects={(t,o) for t,_,o in gates}
            if len(objects)!=1:raise PermissionError('recall instance gate ambiguous')
            type_name,object_id=next(iter(objects))
            if not self.authorizer.readable(ResourceType.OBJECT,[f'{type_name}/{object_id}']):continue
            needed={f'{type_name}/{object_id}/{p}' for _,p,_ in gates}
            if self.authorizer.readable(ResourceType.PROPERTY,needed)!=frozenset(needed):continue
            visible.append(hit)
            if len(visible)==self.config.top_k:break
        return visible

    def _methods(self):
        return tuple(m for m in METHODS if m!='vector' or self.provider is not None)

    @authority_request_scoped
    def recall(self,text,*,strong_ids=(),verified_refs=(),type_names=None,definitions=True,instances=True):
        if type(text) is not str or not text.strip() or len(text)>4000:raise ValueError('recall text must be bounded nonempty text')
        if type_names is not None:
            type_names=frozenset(type_names)
            if not type_names or any(type(t) is not str or not _NAME.fullmatch(t) for t in type_names):raise ValueError('invalid type scope')
        methods=self._methods()
        vector=None if self.provider is None else _vector_literal(self.provider.embed(text),self.dimension)
        out={}
        for kind,enabled in (('definition',definitions),('instance',instances)):
            if not enabled:out[kind]=();continue
            types,props=self._gates(kind)
            if not types:out[kind]=();continue
            per_method={m:self._search(kind,m,text,types,props,vector=vector,type_names=type_names) for m in methods}
            if kind=='instance' and (strong_ids or verified_refs):
                per_method['strong_id']=self._search(kind,'strong_id',text,types,props,strong_ids=strong_ids,verified_refs=verified_refs,type_names=type_names)
            hits=self._fuse(kind,per_method,methods)
            hits=self._instance_post_check(hits) if kind=='instance' else [hit for hit,_ in hits]
            out[kind]=tuple(hits[:self.config.top_k])
        return RecallResult(out['definition'],out['instance'],methods,None if self.provider is None else self.provider.profile_id,self.config.config_version)


class RecallIndexer:
    """Index reflow after governed publish/merge (NX-045/046) and new instances.

    Texts are produced by PostgreSQL from the stored definition/object; this
    side only attaches vectors from the configured provider.
    """

    def __init__(self,pool,session,*,provider=None,expected_dimension=None):
        self._db=_Database(pool,session);self.provider=provider;self.dimension=_provider_dimension(provider,expected_dimension)

    def activate_profile(self,*,replacing=None):
        if self.provider is None:raise ValueError('no embedding provider configured')
        return self._db.call('nexloop_recall_activate_profile',self.provider.model,self.dimension,replacing)

    def _vectors(self,rows):
        if self.provider is None:return None,Jsonb([])
        cache={};vectors=[]
        for row in rows:
            if row['body'] not in cache:cache[row['body']]=_vector_literal(self.provider.embed(row['body']),self.dimension)
            vectors.append({'ref':row['ref'],'field':row['field'],'body':row['body'],'vector':cache[row['body']]})
        return self.provider.profile_id,Jsonb(vectors)

    def index_object_type(self,type_name):
        texts=self._db.call('nexloop_recall_definition_texts',type_name)
        profile,vectors=self._vectors(texts['rows'])
        return self._db.call('nexloop_recall_index_definition',type_name,texts['version'],profile,self.dimension if profile else None,vectors)

    def index_alias(self,alias_id,canonical_ref,alias_text):
        rows=[{'ref':canonical_ref,'field':'alias','body':alias_text.strip() if isinstance(alias_text,str) else alias_text}]
        profile,vectors=self._vectors(rows)
        return self._db.call('nexloop_recall_index_alias',alias_id,canonical_ref,alias_text,profile,self.dimension if profile else None,vectors)

    def index_instance(self,type_name,object_id,*,spec=None):
        spec_json=Jsonb(spec or {})
        texts=self._db.call('nexloop_recall_instance_texts',type_name,object_id,spec_json)
        profile,vectors=self._vectors(texts['rows'])
        return self._db.call('nexloop_recall_index_instance',type_name,object_id,spec_json,texts['revision'],profile,self.dimension if profile else None,vectors)

    def remove_source(self,source_key):
        return self._db.call('nexloop_recall_remove_source',source_key)
