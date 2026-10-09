"""Read-only semantic adapter: browse/resolve over EIOS definitions (docs/05 §3).

Output shape follows EvoOntology's SemanticLayer (version / coverage / ambiguity / source
refs) but nothing here uses or maintains an Evo store: definitions come only from EIOS
schema tables and the governed alias table, via NX-021 recall (gated by current READ)
and a signed definition read re-checked in SQL. Only definitions are returned; resolving
an instance still needs its own authorized object read.
"""
from dataclasses import dataclass
import hashlib

from eios.authz.resources import ResourceType
from nexloop_eios.context_engine.authority import ContextDenied,decision_ref,read_proof,signed_read
from nexloop_eios.postgres_artifacts import canonical_payload

KINDS=('object_type','property','vocabulary_value','any')


def ref_kind(ref):
    if ref.startswith('eios:object_type:'):return 'object_type'
    if ref.startswith('eios:property:'):return 'property'
    if ref.startswith('nexloop:vocabulary:'):return 'vocabulary_value'
    return None


def gates(ref):
    """Same gates as NX-021 recall: the type always, the property for property/vocabulary refs."""
    kind=ref_kind(ref)
    if kind=='object_type':return [(ResourceType.OBJECT_TYPE,ref.removeprefix('eios:object_type:'))]
    body=ref.removeprefix('eios:property:') if kind=='property' else ref.removeprefix('nexloop:vocabulary:')
    type_name,prop=body.split('/')[:2]
    return [(ResourceType.OBJECT_TYPE,type_name),(ResourceType.PROPERTY,type_name+'/'+prop)]


@dataclass(frozen=True)
class SemanticConfig:
    top_k:int=5
    ambiguity_margin:float=0.05
    min_score:float=0.0


class SemanticAdapter:
    def __init__(self,pool,session,signer,recall,*,config=SemanticConfig()):
        self.pool,self.session,self.signer,self.recall,self.config=pool,session,signer,recall,config

    def _definitions(self,refs):
        refs=list(dict.fromkeys(refs))
        if not refs:return {},[]
        wanted={}
        for ref in refs:
            for kind,name in gates(ref):wanted[(kind,name)]=None
        proofs=[];readable=set()
        for kind,name in wanted:
            try:proofs.append(read_proof(self.pool,self.session,kind,name));readable.add((kind,name))
            except ContextDenied:pass
        allowed=[r for r in refs if all(g in readable for g in gates(r))]
        if not allowed:return {},[]
        rows=signed_read(self.pool,self.session,self.signer,'definitions',{'refs':allowed},proofs=proofs)['items']
        return {row['ref']:row for row in rows},proofs

    def _version(self,items,result):
        basis=sorted((i['ref'],i['schema_version'],tuple(i['aliases'])) for i in items)
        digest=hashlib.sha256(canonical_payload([list(b[:2])+[list(b[2])] for b in basis]).encode()).hexdigest()
        return f"semantic:{digest}@{result.config_version}@{result.profile_id or 'fts'}"

    def _ambiguous(self,scores):
        return len(scores)>1 and scores[0]-scores[1]<self.config.ambiguity_margin

    def browse(self,query,*,kind='any',limit=10):
        if kind not in KINDS or type(limit) is not int or not 1<=limit<=20:raise ValueError('browse parameters')
        result=self.recall.recall(query,instances=False)
        hits=[h for h in result.definitions if h.score>=self.config.min_score and (kind=='any' or ref_kind(h.ref)==kind)][:limit]
        details,proofs=self._definitions([h.ref for h in hits])
        items=[{**details[h.ref],'score':round(h.score,6),'method':h.method,'source_refs':[h.ref]} for h in hits if h.ref in details]
        return {'version':self._version(items,result),'coverage':1.0 if items else 0.0,
            'ambiguity':self._ambiguous([i['score'] for i in items]),'items':items,
            'access_decision_refs':sorted(decision_ref(p) for p in proofs)}

    def resolve(self,mentions,*,type_hint=None):
        if type(mentions) not in (list,tuple) or not 1<=len(mentions)<=16 or any(type(m) is not str or not m.strip() for m in mentions):
            raise ValueError('resolve mentions')
        out=[];all_items=[];proofs=[];result=None
        for mention in mentions:
            result=self.recall.recall(mention,instances=False,type_names=None if type_hint is None else [type_hint])
            hits=[h for h in result.definitions if h.score>=self.config.min_score][:self.config.top_k]
            details,mention_proofs=self._definitions([h.ref for h in hits]);proofs+=mention_proofs
            candidates=[{**details[h.ref],'score':round(h.score,6),'source_refs':[h.ref]} for h in hits if h.ref in details]
            all_items+=candidates
            scores=[c['score'] for c in candidates]
            status='unresolved' if not candidates else 'ambiguous' if self._ambiguous(scores) else 'resolved'
            # Ambiguity is reported, never resolved by picking the first candidate.
            out.append({'mention':mention,'status':status,'candidates':candidates})
        resolved=sum(1 for r in out if r['status']=='resolved')
        return {'version':self._version(all_items,result),'coverage':resolved/len(out),'ambiguity':any(r['status']=='ambiguous' for r in out),
            'resolutions':out,'access_decision_refs':sorted({decision_ref(p) for p in proofs})}


class OpenWorkReader:
    """Unconfirmed execution for one Consumer under current Consumer READ (NX-047 aware)."""
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer
    def read(self,consumer_id):
        proof=read_proof(self.pool,self.session,ResourceType.OBJECT,'Consumer/'+consumer_id)
        rows=signed_read(self.pool,self.session,self.signer,'open_work',{'consumer_id':consumer_id},proofs=[proof])
        return rows,decision_ref(proof)
