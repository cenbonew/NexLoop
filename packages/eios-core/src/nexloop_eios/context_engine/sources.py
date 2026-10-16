"""Read the v6 item sources for one message Run under the Source's own current authority.

Each source keeps the exact READ proof it was read under; the bind (0091) re-verifies the
proof, recomputes the content from the source row and compares hashes. A source the
Source may not read is not guessed: its section is declared ``required_source_unreadable``.
"""
from dataclasses import dataclass,field
import hmac

from eios.authz.resources import ResourceType
from nexloop_eios.context_engine.authority import ContextDenied,decision_ref,read_proof,signed_read
from nexloop_eios.context_engine.sections import claim_items,conversation_items,open_work_items
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


def _turns_view(pool,session,signer,conversation_id,proof,limit):
    """0201: the latest turns of one conversation under the same Conversation READ decision as its Claims."""
    from nexloop_eios.assembly import verify_application_role
    claims={**proof,'protocol':'nexloop-turn-read-v1','key_id':signer.key_id,'conversation_id':conversation_id,'limit':limit}
    text=canonical_payload(claims);signature=hmac.new(signer.material,('nexloop-turn-read-v1:'+text).encode(),'sha256').hexdigest()
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_read_conversation_turns(%s,%s,%s,%s)',(session.token_digest,session.world,text,signature)).fetchone()[0]


def _turn_limit(strategy):
    """Twice the strategy's protected recent turns (consumer and Agent), at least 2, at most 64."""
    recent=((strategy.get('sections') or {}).get('evidence') or {}).get('recent_turns',6)
    return max(2,min(64,2*int(recent)))


def consumer_conversations(pool,session,signer,consumer_id):
    """Conversation ids of one Consumer under current Consumer READ (Role Runs, 0093)."""
    proof=read_proof(pool,session,ResourceType.OBJECT,'Consumer/'+consumer_id)
    rows=signed_read(pool,session,signer,'conversations',{'consumer_id':consumer_id},proofs=[proof],function='authz.nexloop_context_consumer_conversations')
    return rows['conversations']


def consumer_read_proof(pool,session,signer,consumer_id):
    """Consumer READ for the Context: configured, else the relay Source's derived READ (0142, the stage Source has no
    per-object grant)."""
    try:return read_proof(pool,session,ResourceType.OBJECT,'Consumer/'+consumer_id)
    except ContextDenied:
        from nexloop_eios.object_reads import AuthorizedObjectReader
        try:return AuthorizedObjectReader(pool,session,signer)._authority(ResourceType.OBJECT,'Consumer/'+consumer_id)
        except Exception:raise ContextDenied('eios:object:Consumer/'+consumer_id) from None


def collect(pool,session,signer,*,consumer_id,conversation_ids,strategy,current_message_id=None,current_sequence=None):
    import psycopg
    from nexloop_eios.goal_controls import ControlDenied,ControlPlane
    from nexloop_eios.object_reads import AuthorizedObjectReader
    out=Collected()
    # Unconfirmed execution (intents, outbound not accepted by the channel): Consumer READ.
    try:
        proof=consumer_read_proof(pool,session,signer,consumer_id)
        rows=signed_read(pool,session,signer,'open_work',{'consumer_id':consumer_id},proofs=[proof])
        decision=out.keep(proof)
        out.items+=open_work_items(rows,decision=decision)
        # The Consumer itself in consumer_state (formal, object level; a property is shown only with its own READ decision,
        # which the relay Source's derived READ does not give: none here).
        # Its decision is the object reader's (request-memoized, as the Conversation decisions below), so one preparation and a
        # preparation without proof reuse (ADR-029a) cite the same decision.
        from nexloop_eios.context_engine.sections import formal_object_item
        reader=AuthorizedObjectReader(pool,session,signer)
        current=reader.get('Consumer',consumer_id)
        state=reader._authority(ResourceType.OBJECT,'Consumer/'+consumer_id)
        out.items.append(formal_object_item('consumer_state',{'type_name':'Consumer','object_id':consumer_id,'revision':current['revision'],'properties':{}},
            decision=out.keep(state)))
    except (ContextDenied,psycopg.errors.InsufficientPrivilege):
        out.insufficient.append({'code':'required_source_unreadable','section':'open_work','refs':['eios:object:Consumer/'+consumer_id]})
    # Claim evidence (source text only; hypotheses never in a real-world Context): Conversation READ each.
    unreadable=[]
    for conversation_id in conversation_ids:
        try:
            proof=AuthorizedObjectReader(pool,session,signer)._authority(ResourceType.OBJECT,'Conversation/'+conversation_id)
            view=_claims_view(pool,session,signer,conversation_id,proof)
            decision=out.keep(proof)
            # 0201: the conversation itself (earlier turns; the current statement is the pack's user_statement).
            # Earlier turns only: never the current statement or anything accepted after it.
            turns=[t for t in _turns_view(pool,session,signer,conversation_id,proof,_turn_limit(strategy))['turns']
                   if t['id']!=current_message_id and (current_sequence is None or t['sequence']<current_sequence)]
            # 0202: turns first, so the model reads what was said before the extracted Claims; the customer's latest turn before
            # the current statement is tagged (only in a message Context, where the current statement is known).
            inbound=[t for t in turns if t.get('direction')!='outbound']
            latest=max(inbound,key=lambda t:t['sequence'])['id'] if inbound and current_sequence is not None else None
            out.items+=conversation_items(turns,decision=decision,latest_consumer=latest)
            out.items+=claim_items(view,strategy,decision=decision)
        except Exception as error:
            if not isinstance(error,(ContextDenied,psycopg.errors.InsufficientPrivilege)) and type(error).__name__ not in ('ActionAuthorizationDenied','AuthorizationFactDenied','AuthorizationUnavailable'):raise
            unreadable.append('eios:object:Conversation/'+conversation_id)
    if unreadable:out.insufficient.append({'code':'required_source_unreadable','section':'evidence','refs':unreadable})
    # Control snapshot now (NX-022); a pause is reported, never hidden.
    try:
        out.control=ControlPlane(pool,session).snapshot(scopes=[('consumer','consumer:'+consumer_id)],objects=[('Consumer',consumer_id)],budgets=['model'])
    except ControlDenied as denied:
        out.insufficient.append({'code':'control_paused' if denied.reason=='control_paused' else 'goal_not_current','section':'goal','refs':[]})
    return out

