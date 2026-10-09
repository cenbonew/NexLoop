"""Read the v6 item sources for one message Run under the Source's own current authority.

Each source keeps the exact READ proof it was read under; the bind (0091) re-verifies the
proof, recomputes the content from the source row and compares hashes. A source the
Source may not read is not guessed: its section is declared ``required_source_unreadable``.
"""
from dataclasses import dataclass,field
import hmac

from eios.authz.resources import ResourceType
from nexloop_eios.context_engine.authority import ContextDenied,decision_ref,read_proof,signed_read
from nexloop_eios.context_engine.sections import claim_items,open_work_items
from nexloop_eios.postgres_artifacts import canonical_payload


@dataclass
class Collected:
    items:list=field(default_factory=list)
    proofs:dict=field(default_factory=dict)
    insufficient:list=field(default_factory=list)
    control:dict|None=None

    def keep(self,proof):
        ref=decision_ref(proof);self.proofs[ref]=proof
        return ref


def _claims_view(pool,session,signer,conversation_id,proof):
    from nexloop_eios.assembly import verify_application_role
    claims={**proof,'protocol':'nexloop-claim-read-v1','key_id':signer.key_id,'conversation_id':conversation_id}
    text=canonical_payload(claims);signature=hmac.new(signer.material,('nexloop-claim-read-v1:'+text).encode(),'sha256').hexdigest()
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_read_conversation_claims(%s,%s,%s,%s)',(session.token_digest,session.world,text,signature)).fetchone()[0]


def collect(pool,session,signer,*,consumer_id,conversation_id,strategy):
    import psycopg
    from nexloop_eios.goal_controls import ControlDenied,ControlPlane
    from nexloop_eios.object_reads import AuthorizedObjectReader
    out=Collected()
    # Unconfirmed execution (intents, outbound not accepted by the channel): Consumer READ.
    try:
        proof=read_proof(pool,session,ResourceType.OBJECT,'Consumer/'+consumer_id)
        rows=signed_read(pool,session,signer,'open_work',{'consumer_id':consumer_id},proofs=[proof])
        out.items+=open_work_items(rows,decision=out.keep(proof))
    except (ContextDenied,psycopg.errors.InsufficientPrivilege):
        out.insufficient.append({'code':'required_source_unreadable','section':'open_work','refs':['eios:object:Consumer/'+consumer_id]})
    # Claim evidence (source text only; hypotheses never in a real-world Context): Conversation READ.
    try:
        proof=AuthorizedObjectReader(pool,session,signer)._authority(ResourceType.OBJECT,'Conversation/'+conversation_id)
        view=_claims_view(pool,session,signer,conversation_id,proof)
        out.items+=claim_items(view,strategy,decision=out.keep(proof))
    except Exception as error:
        if not isinstance(error,(ContextDenied,psycopg.errors.InsufficientPrivilege)) and type(error).__name__ not in ('ActionAuthorizationDenied','AuthorizationFactDenied','AuthorizationUnavailable'):raise
        out.insufficient.append({'code':'required_source_unreadable','section':'evidence','refs':['eios:object:Conversation/'+conversation_id]})
    # Control snapshot now (NX-022); a pause is reported, never hidden.
    try:
        out.control=ControlPlane(pool,session).snapshot(scopes=[('consumer','consumer:'+consumer_id)],objects=[('Consumer',consumer_id)],budgets=['model'])
    except ControlDenied as denied:
        out.insufficient.append({'code':'control_paused' if denied.reason=='control_paused' else 'goal_not_current','section':'goal','refs':[]})
    return out

