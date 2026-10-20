"""Read-only semantic adapter: browse/resolve over EIOS definitions (docs/05 §3).

Output shape follows EvoOntology's SemanticLayer (version / coverage / ambiguity / source
refs) but nothing here uses or maintains an Evo store: definitions come only from EIOS
schema tables and the governed alias table, via NX-021 recall (gated by current READ)
and a signed definition read re-checked in SQL. Only definitions are returned; resolving
an instance still needs its own authorized object read.
"""
from dataclasses import dataclass
import hashlib
import hmac
import logging
import uuid

from eios.authz.resources import ResourceType
from nexloop_eios.context_engine.authority import ContextDenied,decision_ref,read_proof,signed_read
from nexloop_eios.postgres_artifacts import canonical_payload

KINDS=('object_type','property','vocabulary_value','any')
RECORD_PROTOCOL='nexloop-semantic-access-v1'
_log=logging.getLogger(__name__)


def term_hash(term):
    """sha256 of one query term (D2: the record keeps the hash, never the text)."""
    return hashlib.sha256(term.strip().encode('utf-8')).hexdigest()


class SemanticAccessRecorder:
    """NX-038 E3 (D2): appends one record per semantic access, and one per reported use, through the signed SQL port
    authz.nexloop_semantic_access_record (tenant and world from the authenticated service identity; the Run must be bound to
    them). Refs, revisions, hashes and counts only. A failed write never fails the caller's Run: it is counted in
    ``failures`` and logged without content, and the access returns its result as before."""
    def __init__(self,pool,session,signer,*,run_id=None,context_id=None):
        self.pool,self.session,self.signer=pool,session,signer
        self.run_id=None if run_id is None else str(run_id);self.context_id=context_id
        self.written=0;self.failures=0

    def _send(self,payload):
        from nexloop_eios.assembly import verify_application_role
        body=canonical_payload(payload)
        auth=self.session.authentication
        text=canonical_payload({'protocol':RECORD_PROTOCOL,'key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'world':self.session.world,
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest()})
        signature=hmac.new(self.signer.material,(RECORD_PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            db.execute('select authz.nexloop_semantic_access_record(%s,%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature,body))

    def _write(self,payload):
        try:self._send(payload)
        except Exception as error:  # the record is best effort: the Run goes on (design D2, NX-038 E3)
            self.failures+=1
            _log.warning('semantic access record not written: %s',type(error).__name__)
            return None
        self.written+=1
        return payload['access_id']

    def access(self,operation,query_kind,terms,*,coverage,ambiguity,refs,unresolved=0):
        return self._write({'kind':'access','access_id':str(uuid.uuid4()),'run_id':self.run_id,'context_id':self.context_id,'operation':operation,
            'query_kind':query_kind,'term_hashes':[term_hash(t) for t in terms],'coverage':coverage,'ambiguity':bool(ambiguity),
            'refs':refs,'unresolved_count':unresolved})

    def used(self,access_id,refs):
        """The caller used these refs of an earlier access (a second append-only row; the access row never changes)."""
        if access_id is None:return None
        return self._write({'kind':'use','access_id':str(uuid.uuid4()),'accessed_id':access_id,'run_id':self.run_id,'refs':refs})


def _refs(items):
    seen={}
    for item in items:seen.setdefault(item['ref'],{'ref':item['ref'],'revision':str(item['schema_version'])})
    return list(seen.values())


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


class OverlayRefused(PermissionError):
    """An evaluation overlay outside an evaluation session (AT-023): a real-world session never reads a candidate."""


@dataclass(frozen=True)
class EvaluationOverlay:
    """NX-038 E5 (design §12.3): one evolution candidate's patch, read as an overlay on the active definitions. Refs and term
    hashes only; the active definitions and aliases are never written."""
    candidate_id:str
    patch_digest:str
    change_kind:str
    target_refs:tuple
    term_hashes:frozenset

    def refs_for(self,terms,kind='any'):
        if self.change_kind!='alias' or not any(term_hash(t) in self.term_hashes for t in terms):return []
        return [r for r in self.target_refs if kind=='any' or ref_kind(r)==kind]


def load_overlay(pool,session,candidate_id,patch_digest):
    """Loads a candidate overlay through authz.nexloop_evo_overlay: world test with a registered evaluation credential (D18) and an
    `evaluating` candidate of the same tenant. A real-world session is refused here and again at the SQL entry."""
    if session.world=='real':raise OverlayRefused('evaluation overlay refused in world real')
    from nexloop_eios.assembly import verify_application_role
    with pool.connection() as db:
        verify_application_role(db)
        v=db.execute('select authz.nexloop_evo_overlay(%s,%s,%s,%s)',(session.token_digest,session.world,str(candidate_id),patch_digest)).fetchone()[0]
    return EvaluationOverlay(candidate_id=v['candidate_id'],patch_digest=v['patch_digest'],change_kind=v['change_kind'],
        target_refs=tuple(v['target_refs']),term_hashes=frozenset(v['term_hashes']))


class SemanticAdapter:
    def __init__(self,pool,session,signer,recall,*,config=SemanticConfig(),recorder=None,overlay=None):
        if overlay is not None and (session.world=='real' or not isinstance(overlay,EvaluationOverlay)):
            raise OverlayRefused('evaluation overlay refused in world real')  # AT-023 first line; the SQL entry is the second
        self.pool,self.session,self.signer,self.recall,self.config=pool,session,signer,recall,config
        self.recorder=recorder;self.overlay=overlay

    def _with_overlay(self,terms,kind,hits):
        """Overlay refs first (exact alias match, score 1.0), then the recall hits not already named; still gated by READ below."""
        extra=[] if self.overlay is None else self.overlay.refs_for(terms,kind)
        return [(r,1.0,'evaluation_overlay') for r in extra]+[(h.ref,h.score,h.method) for h in hits if h.ref not in extra]

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
        hits=self._with_overlay([query],kind,[h for h in result.definitions if h.score>=self.config.min_score and (kind=='any' or ref_kind(h.ref)==kind)])[:limit]
        details,proofs=self._definitions([ref for ref,_,_ in hits])
        items=[{**details[ref],'score':round(score,6),'method':method,'source_refs':[ref]} for ref,score,method in hits if ref in details]
        out={'version':self._version(items,result),'coverage':1.0 if items else 0.0,
            'ambiguity':self._ambiguous([i['score'] for i in items]),'items':items,
            'access_decision_refs':sorted(decision_ref(p) for p in proofs)}
        if self.overlay is not None:out['overlay']=self.overlay.candidate_id
        if self.recorder is not None:
            out['access_id']=self.recorder.access('browse',kind,[query],coverage=out['coverage'],ambiguity=out['ambiguity'],refs=_refs(items),
                unresolved=0 if items else 1)
        return out

    def resolve(self,mentions,*,type_hint=None):
        if type(mentions) not in (list,tuple) or not 1<=len(mentions)<=16 or any(type(m) is not str or not m.strip() for m in mentions):
            raise ValueError('resolve mentions')
        out=[];all_items=[];proofs=[];result=None
        for mention in mentions:
            result=self.recall.recall(mention,instances=False,type_names=None if type_hint is None else [type_hint])
            hits=self._with_overlay([mention],'any' if type_hint is None else 'object_type',
                [h for h in result.definitions if h.score>=self.config.min_score])[:self.config.top_k]
            details,mention_proofs=self._definitions([ref for ref,_,_ in hits]);proofs+=mention_proofs
            candidates=[{**details[ref],'score':round(score,6),'source_refs':[ref]} for ref,score,_ in hits if ref in details]
            all_items+=candidates
            scores=[c['score'] for c in candidates]
            status='unresolved' if not candidates else 'ambiguous' if self._ambiguous(scores) else 'resolved'
            # Ambiguity is reported, never resolved by picking the first candidate.
            out.append({'mention':mention,'status':status,'candidates':candidates})
        resolved=sum(1 for r in out if r['status']=='resolved')
        value={'version':self._version(all_items,result),'coverage':resolved/len(out),'ambiguity':any(r['status']=='ambiguous' for r in out),
            'resolutions':out,'access_decision_refs':sorted({decision_ref(p) for p in proofs})}
        if self.overlay is not None:value['overlay']=self.overlay.candidate_id
        if self.recorder is not None:
            value['access_id']=self.recorder.access('resolve','any' if type_hint is None else 'object_type',list(mentions),coverage=value['coverage'],
                ambiguity=value['ambiguity'],refs=_refs(all_items),unresolved=sum(1 for r in out if r['status']=='unresolved'))
        return value


class OpenWorkReader:
    """Unconfirmed execution for one Consumer under current Consumer READ (NX-047 aware)."""
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer
    def read(self,consumer_id):
        proof=read_proof(self.pool,self.session,ResourceType.OBJECT,'Consumer/'+consumer_id)
        rows=signed_read(self.pool,self.session,self.signer,'open_work',{'consumer_id':consumer_id},proofs=[proof])
        return rows,decision_ref(proof)
