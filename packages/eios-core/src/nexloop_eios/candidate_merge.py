"""NX-045 / ADR-019 §3.4: candidate glue (merge), similar-candidate dedupe and the review queue.

A staged candidate is compared with existing definitions of the same kind
(type↔type, property↔properties of the same owner type, vocabulary value↔the
same property's vocabulary, plus their aliases) using four deterministic
features in [0,1] (ADR-019 decision 6):

* lexical_similarity    – Dice coefficient over CJK character bigrams / words
* core_term_containment – containment of the core term after generic affixes
* vector_cluster        – embedding cosine (the recall embedding profile)
* rule_whitelist        – tenant business whitelist hit

weighted_total = Σ w·feature with tenant weights/threshold from the active,
versioned merge configuration. At or above the merge threshold the candidate is
merged: an alias is recorded, NX-021 recall is reflowed, and the dependent
Claims are re-matched through the NX-020 guards and applied automatically.
Below it the candidate waits in pending_review for a human (NX-044/046).
Object instances never merge by similarity (docs/04 §5): always reviewed.
Other features (entity recognition, structure, co-occurrence, intent,
behaviour feedback) are reserved and not implemented.
"""
from dataclasses import dataclass
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import itertools
import math
import re

from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider,resolve_authority,authority_request_scoped
from nexloop_eios.claim_matching import MATCHER_VERSION,ScriptedMatchProvider,_Port
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.recall import vocabulary_ref

FEATURES=('lexical_similarity','core_term_containment','vector_cluster','rule_whitelist')
REVIEW_ACTION='ontology.schema.review'
_CJK=re.compile('[㐀-䶿一-鿿豈-﫿]+')
_WORD=re.compile(r'[a-z0-9]+')
# Generic affixes that do not carry the concept (synthetic, versioned with the feature code).
GENERIC_AFFIXES=('顾客','用户','客户','是否','常用','主要','的','偏好','方式','情况','程度','信息','类型','状态','需求','意向','习惯')
GENERIC_WORDS=frozenset({'type','preference','info','status','level','mode','method','pref','kind'})
FEATURE_VERSION='nx045-glue-features/1'


def _norm(text):return re.sub(r'\s+','',str(text)).lower()


def grams(text):
    text=str(text).lower();out=set(_WORD.findall(_CJK.sub(' ',text.replace('_',' '))))
    for run in _CJK.findall(text):out|={run} if len(run)==1 else {run[i:i+2] for i in range(len(run)-1)}
    return out


def lexical_similarity(a,b):
    x,y=grams(a),grams(b)
    return 0.0 if not x or not y else 2*len(x&y)/(len(x)+len(y))


def core_term(text):
    text=_norm(text)
    if re.fullmatch(r'[a-z0-9_]+',text):
        return '_'.join(w for w in text.split('_') if w and w not in GENERIC_WORDS)
    changed=True
    while changed and text:
        changed=False
        for affix in GENERIC_AFFIXES:
            if text.startswith(affix) and len(text)>len(affix):text=text[len(affix):];changed=True
            if text.endswith(affix) and len(text)>len(affix):text=text[:-len(affix)];changed=True
    return text


def core_term_containment(a,b):
    x,y=core_term(a),core_term(b)
    if not x or not y:return 0.0
    if x in y or y in x:return 1.0
    best=0
    for i in range(len(x)):
        for j in range(i+best+1,len(x)+1):
            if x[i:j] in y:best=j-i
            else:break
    # One shared character (订单/退货单, 线上/线下) is not a shared core term.
    return 0.0 if best<2 else best/min(len(x),len(y))


def whitelist_hit(whitelist,a,b,target_ref=None):
    a,b=_norm(a),_norm(b)
    for entry in whitelist:
        texts={_norm(t) for t in entry.get('texts',())}
        if entry.get('target_ref') is not None:
            if target_ref==entry['target_ref'] and a in texts:return 1.0
        elif a in texts and b in texts:return 1.0
    return 0.0


def _cos(x,y):
    nx=math.sqrt(sum(v*v for v in x));ny=math.sqrt(sum(v*v for v in y))
    return 0.0 if not nx or not ny else max(0.0,min(1.0,sum(p*q for p,q in zip(x,y))/(nx*ny)))


class FeatureScorer:
    def __init__(self,provider,whitelist=()):
        self.provider,self.whitelist=provider,tuple(whitelist);self._vectors={}

    def _vector(self,text):
        if text not in self._vectors:self._vectors[text]=self.provider.embed(text)
        return self._vectors[text]

    def features(self,candidate_texts,target_texts,target_ref=None):
        pairs=[(a,b) for a in candidate_texts for b in target_texts if str(a).strip() and str(b).strip()]
        if not pairs:return dict.fromkeys(FEATURES,0.0)
        return {'lexical_similarity':max(lexical_similarity(a,b) for a,b in pairs),
            'core_term_containment':max(core_term_containment(a,b) for a,b in pairs),
            'vector_cluster':max(_cos(self._vector(str(a)),self._vector(str(b))) for a,b in pairs),
            'rule_whitelist':max(whitelist_hit(self.whitelist,a,b,target_ref) for a,b in pairs)}


def weighted(features,weights):
    return round(min(1.0,sum(weights[k]*features[k] for k in FEATURES)),6)


# --------------------------------------------------------------- calibration

def calibrate(pairs,provider,*,step=0.1,thresholds=None):
    """Grid search over the weight simplex and thresholds on labelled synthetic pairs.

    Constraint: the whitelist weight alone reaches the threshold (explicit rule).
    Objective, in order: no false merge (precision 1.0) → highest recall →
    largest margin between the lowest true and highest false score → stable
    tie-break by weights. A false merge silently rewrites meaning; a missed
    merge only costs a human review, so precision dominates.
    """
    thresholds=thresholds or [round(0.30+0.025*i,3) for i in range(25)]
    whitelist=[entry for pair in pairs for entry in pair.get('whitelist',())]
    scorer=FeatureScorer(provider,whitelist)
    rows=[(scorer.features([p['candidate']],[p['target']],p.get('target_ref')),bool(p['same'])) for p in pairs]
    units=int(round(1/step));best=None
    for combo in itertools.product(range(units+1),repeat=3):
        if sum(combo)>units:continue
        w=dict(zip(FEATURES,[c/units for c in combo]+[(units-sum(combo))/units]))
        scored=[(weighted(f,w),same) for f,same in rows]
        for threshold in thresholds:
            # A business whitelist entry is an explicit rule: alone it must reach the threshold.
            if w['rule_whitelist']+1e-9<threshold:continue
            tp=sum(1 for s,y in scored if s>=threshold and y);fp=sum(1 for s,y in scored if s>=threshold and not y)
            positives=sum(1 for _,y in scored if y)
            precision=tp/(tp+fp) if tp+fp else 1.0;recall=tp/positives if positives else 0.0
            true_low=min((s for s,y in scored if y and s>=threshold),default=threshold);false_high=max((s for s,y in scored if not y),default=0.0)
            key=(precision>=0.999,recall,round(true_low-false_high,6),-threshold,tuple(-w[k] for k in FEATURES))
            if best is None or key>best[0]:
                best=(key,{'weights':{k:round(v,3) for k,v in w.items()},'merge_threshold':threshold,'precision':round(precision,4),'recall':round(recall,4),
                    'true_positive':tp,'false_positive':fp,'positives':positives,'pairs':len(pairs),'margin':round(true_low-false_high,4)})
    return best[1]


# ------------------------------------------------------------------- service

@dataclass(frozen=True)
class GlueOutcome:
    candidate_id:str
    status:str
    merge_scores:dict|None=None
    superseded_by:str|None=None
    alias_id:str|None=None
    rematched:dict|None=None


def _decode_vocabulary(target_ref,enum,type_name,property_name):
    return next((m for m in enum if vocabulary_ref(type_name,property_name,m)==target_ref),None)


class CandidateGluer:
    """staged → superseded (similar open candidate) | merged | pending_review."""

    def __init__(self,pool,session,signer,*,indexer,matcher,provider):
        self.pool,self.session,self.signer=pool,session,signer
        self.port=_Port(pool,session,signer);self.indexer=indexer;self.matcher=matcher;self.provider=provider

    def _read(self,payload):return self.port.call('nexloop_read_candidate_glue',payload)
    def _record(self,payload):return self.port.call('nexloop_record_candidate_glue',payload)

    def process_staged(self):
        """Oldest first, so a later similar candidate folds into an earlier one; then re-point late Claims of merged ones."""
        outcomes=[self.process(c['candidate_id']) for c in self._read({'verb':'candidates','status':['staged']})]
        return outcomes+self.repoint_merged()

    def repoint_merged(self):
        outcomes=[]
        for candidate in self._read({'verb':'candidates','status':['merged']}):
            result=self._record({'candidate_id':candidate['candidate_id'],'to':'repoint'})
            if result['claims']:
                outcomes.append(GlueOutcome(candidate['candidate_id'],'merged',candidate['merge_scores'],
                    rematched=self._rematch(candidate,candidate['merge_target_ref'],result['claims'])))
        return outcomes

    # ---------------------------------------------------------------- texts
    @staticmethod
    def _texts(candidate):
        p=candidate['candidate']['proposed']
        if candidate['kind']=='vocabulary_value':return [str(p['value'])]
        return [t for t in (p.get('display_name'),p.get('name')) if t]

    def _definition_texts(self,type_name):
        try:rows=self.indexer._db.call('nexloop_recall_definition_texts',type_name)['rows']
        except Exception:return {}
        by_ref={}
        for row in rows:
            if row['field']!='description':by_ref.setdefault(row['ref'],[]).append(row['body'])
        return by_ref

    def _targets(self,candidate):
        """Same-kind existing definitions (+ their aliases) as {ref: texts}."""
        p=candidate['candidate']['proposed'];kind=candidate['kind']
        if kind=='property':
            owner=p['owner_type_ref'].rsplit(':',1)[1]
            targets={r:t for r,t in self._definition_texts(owner).items() if r.startswith(f'eios:property:{owner}/')}
        elif kind=='vocabulary_value':
            owner,prop=p['property_ref'].split(':',2)[2].split('/',1)
            targets={r:t for r,t in self._definition_texts(owner).items() if r.startswith(f'nexloop:vocabulary:{owner}/{prop}/')}
        elif kind=='object_type':
            recalled=self.matcher.recall.recall(' '.join(self._texts(candidate)),instances=False).definitions
            targets={}
            for type_name in sorted({h.ref.rsplit(':',1)[1] for h in recalled if h.ref.startswith('eios:object_type:')}):
                targets.update({r:t for r,t in self._definition_texts(type_name).items() if r=='eios:object_type:'+type_name})
        else:return {}
        for alias in self._read({'verb':'aliases'}):
            if alias['canonical_ref'] in targets:targets[alias['canonical_ref']].append(alias['alias_text'])
        return targets

    @staticmethod
    def _scope(candidate):
        p=candidate['candidate']['proposed']
        return {'property':p.get('owner_type_ref'),'vocabulary_value':p.get('property_ref'),'object_type':'*','object_instance':p.get('type_ref')}[candidate['kind']]

    def _scores(self,features,config,best_ref):
        return {**{k:round(v,6) for k,v in features.items()},'weighted_total':weighted(features,config['weights']),
            **({'best_match_ref':best_ref} if best_ref else {}),'threshold':float(config['merge_threshold']),'config_version':config['config_version']}

    # -------------------------------------------------------------- process
    @authority_request_scoped
    def process(self,candidate_id):
        config=self._read({'verb':'configuration'})
        if not config:raise LookupError('no active merge configuration')
        config['weights']={k:float(v) for k,v in config['weights'].items()}
        candidates={c['candidate_id']:c for c in self._read({'verb':'candidates','status':['staged','pending_review']})}
        candidate=candidates.get(candidate_id)
        if candidate is None or candidate['status']!='staged':
            return GlueOutcome(candidate_id,candidate['status'] if candidate else 'unavailable')
        scorer=FeatureScorer(self.provider,config['whitelist'])
        base={'candidate_id':candidate_id,'config_version':config['config_version']}
        # 1. Similar open candidate of the same kind and scope (beyond exact-text dedupe).
        mine=self._texts(candidate)
        peers=[c for c in candidates.values() if c['candidate_id']!=candidate_id and c['kind']==candidate['kind']
               and self._scope(c)==self._scope(candidate) and c['created_at']<=candidate['created_at']]
        best_peer=None
        for peer in peers:
            f=scorer.features(mine,self._texts(peer));total=weighted(f,config['weights'])
            if best_peer is None or total>best_peer[0]:best_peer=(total,f,peer)
        if best_peer and best_peer[0]>=float(config['dedupe_threshold']):
            scores=self._scores(best_peer[1],config,None)
            self._record({**base,'to':'superseded','superseded_by':best_peer[2]['candidate_id'],'merge_scores':scores})
            return GlueOutcome(candidate_id,'superseded',scores,superseded_by=best_peer[2]['candidate_id'])
        # 2. Existing definitions of the same kind.
        if candidate['kind']=='object_instance':
            scores=self._scores(dict.fromkeys(FEATURES,0.0),config,None)
            self._record({**base,'to':'pending_review','merge_scores':scores,'reason':'instance_identity_requires_review'})
            return GlueOutcome(candidate_id,'pending_review',scores)
        best=None
        for ref,texts in sorted(self._targets(candidate).items()):
            f=scorer.features(mine,texts,ref);total=weighted(f,config['weights'])
            if best is None or total>best[0]:best=(total,f,ref)
        if best is None or best[0]<float(config['merge_threshold']):
            scores=self._scores(best[1] if best else dict.fromkeys(FEATURES,0.0),config,best[2] if best else None)
            self._record({**base,'to':'pending_review','merge_scores':scores})
            return GlueOutcome(candidate_id,'pending_review',scores)
        scores=self._scores(best[1],config,best[2])
        result=self._record({**base,'to':'merged','merge_target_ref':best[2],'merge_scores':scores,'alias_text':mine[0]})
        if result.get('replay'):return GlueOutcome(candidate_id,result['status'],scores)
        # 3. Reflow the alias into recall, then re-point and re-apply the dependent Claims.
        self.indexer.index_alias(result['alias_id'],best[2],mine[0])
        rematched=self._rematch(candidate,best[2],result['claims'])
        return GlueOutcome(candidate_id,'merged',scores,alias_id=result['alias_id'],rematched=rematched)

    def _rematch(self,candidate,target_ref,claim_ids,*,awaiting=False):
        version=f'{MATCHER_VERSION};merge:{candidate["candidate_id"]}'
        claims={}
        for conversation in sorted({row['conversation_id'] for row in self._claim_rows(claim_ids)}):
            for claim in self.matcher.claims.read(conversation_id=conversation)['statements']:
                # Only re-pointed Claims; resolved/superseded/rejected ones are left as they are.
                # needs_resolution included: a reflow that waited for grants resumes its own proposals idempotently.
                if claim['claim_id'] in claim_ids and claim['resolution_state'] in ('unresolved','needs_resolution')+(('awaiting_definition',) if awaiting else ()):
                    claims[claim['claim_id']]=claim
        decisions={cid:self._decision(candidate,target_ref,claim) for cid,claim in claims.items()}
        provider=ScriptedMatchProvider({k:v for k,v in decisions.items() if v is not None})
        out={}
        for claim_id,claim in sorted(claims.items()):
            if decisions[claim_id] is None:
                out[claim_id]={'outcome':'unresolved','applied':None};continue  # left for the normal matcher
            match=self.matcher.match_claim(claim,matcher_version=version,provider=provider,allow_awaiting=awaiting)
            out[claim_id]={'outcome':match['outcome'],'applied':self.matcher.apply(match['proposal_id']) if match.get('proposal_id') else None}
        return out

    # ------------------------------------------------- NX-044 reflow entry points
    def reflow_merged(self,candidate_id):
        """After a merge (service glue or NX-044 merge_into + ontology.nexloop_candidate_merge_effects):
        reflow the alias into recall, then re-match and apply the re-pointed Claims."""
        candidate=next((c for c in self._read({'verb':'candidates','status':['merged']}) if c['candidate_id']==candidate_id),None)
        if candidate is None:raise LookupError('merged candidate required')
        texts={_norm(t) for t in self._texts(candidate)}
        alias=next((a for a in self._read({'verb':'aliases'}) if a['canonical_ref']==candidate['merge_target_ref'] and _norm(a['alias_text']) in texts),None)
        if alias is None:raise LookupError('alias for merged candidate missing')
        self.indexer.index_alias(alias['alias_id'],alias['canonical_ref'],alias['alias_text'])
        claims=[x[6:] for x in candidate['dependent_claims'] if x.startswith('claim:')]
        return GlueOutcome(candidate_id,'merged',candidate['merge_scores'],alias_id=alias['alias_id'],
            rematched=self._rematch(candidate,candidate['merge_target_ref'],claims))

    def reflow_published(self,candidate_id):
        """After NX-044 publishes the definition and ontology.nexloop_candidate_publish_effects re-points the
        Claims: reindex the owner type, then re-match. Applying still requires the published edit Action
        bundle to include the new definition (NX-044); otherwise the NX-020 guards keep the Claim unresolved."""
        candidate=next((c for c in self._read({'verb':'candidates','status':['published']}) if c['candidate_id']==candidate_id),None)
        if candidate is None:raise LookupError('published candidate required')
        p=candidate['candidate']['proposed'];kind=candidate['kind']
        if kind=='property':owner=p['owner_type_ref'].rsplit(':',1)[1];target=f"eios:property:{owner}/{p['name']}"
        elif kind=='vocabulary_value':
            owner,prop=p['property_ref'].split(':',2)[2].split('/',1);target=vocabulary_ref(owner,prop,p['value'])
        else:raise LookupError('reflow after publication supports property and vocabulary value candidates')
        self.matcher._schemas.pop(owner,None)
        self.indexer.index_object_type(owner)
        claims=[x[6:] for x in candidate['dependent_claims'] if x.startswith('claim:')]
        # Published: Claims stay awaiting_definition until they are actually applied (no silent reset).
        return GlueOutcome(candidate_id,'published',candidate['merge_scores'],rematched=self._rematch(candidate,target,claims,awaiting=True))

    def _claim_rows(self,claim_ids):
        # Only ids → conversations; Claim content is then read under current Conversation READ (NX-019).
        return self._read({'verb':'claims','claim_ids':sorted(claim_ids)})

    def _decision(self,candidate,target_ref,claim):
        """Deterministic re-pointed decision; the NX-020 guards still apply in full."""
        p=candidate['candidate']['proposed'];value=claim['value'].get('value')
        def d(prop,val,type_ref=None):
            return {'type':{'ref':type_ref,'new':None},'instance':{'strong_id':None,'name':None},'property':{'ref':prop,'new':None},'value':val,
                'rationale':f'merged candidate {candidate["candidate_id"]} into {target_ref}'}
        consumer=claim['subject']['kind']=='consumer'
        if candidate['kind']=='property':return d(target_ref,value,None if consumer else p['owner_type_ref'])
        if candidate['kind']=='vocabulary_value' and candidate['status']=='published':
            return d(p['property_ref'],p['value'],None if consumer else 'eios:object_type:'+p['property_ref'].split(':',2)[2].split('/',1)[0])
        if candidate['kind']=='vocabulary_value':
            type_name,prop=p['property_ref'].split(':',2)[2].split('/',1)
            schema=self.matcher._schema(type_name)
            definition=next((x for x in schema.properties if x.property_name==prop),None) if schema else None
            member=_decode_vocabulary(target_ref,tuple(definition.type_descriptor.enum) if definition and definition.type_descriptor else (),type_name,prop)
            return None if member is None else d(p['property_ref'],member,None if consumer else 'eios:object_type:'+type_name)
        return None


class ReviewQueueReader:
    """NX-046 reads: pending_review of the caller's own tenant/world, review permission (ontology.schema.review EXECUTE) required.

    The caller has already re-authenticated the session (human browser or service).
    A review authorization that cannot be granted is reported as PermissionError,
    so the workbench shows "no review permission" rather than an empty queue.
    """

    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    PROTOCOL='nexloop-review-queue-v1'

    def _claims(self,body,protocol=None):
        protocol=protocol or self.PROTOCOL
        target=resource_id(ResourceType.ACTION,REVIEW_ACTION,1);entries=[]
        try:
            query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
            decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,query,entries))
        except Exception:raise PermissionError('review permission unavailable') from None
        if not decision.allowed or not decision.authoritative or decision.obligations:raise PermissionError('review queue denied')
        auth=self.session.authentication
        claims={'protocol':protocol,'key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,
            'credential_id':auth.credential_id,'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,'action_resource':target,
            'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),'parameters_digest':hashlib.sha256(body.encode()).hexdigest()}
        text=canonical_payload(claims)
        return text,hmac.new(self.signer.material,(protocol+':'+text).encode(),'sha256').hexdigest()

    def _call(self,function,payload,protocol=None):
        body=canonical_payload(payload);text,signature=self._claims(body,protocol)
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute(f'select authz.{function}(%s,%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]

    def pending(self,*,limit=50):
        if type(limit) is not int or not 1<=limit<=200:raise ValueError('limit')
        return self._call('nexloop_read_review_queue',{'limit':limit})

    def candidate(self,candidate_id):
        if type(candidate_id) is not str or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',candidate_id):raise ValueError('candidate id')
        return self._call('nexloop_read_review_candidate',{'candidate_id':candidate_id})
