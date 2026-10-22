"""NX-028 slice 2: the owner workbench's write entry (same-origin, real Human browser session).

``POST /api/v1/workbench/actions/{operation}`` turns one form submission into the governed human Action registered for
that operation (0150 capability registry): pause / resume a scope, release a contact restriction (ADR-023), the five
human commitment Actions (NX-026), a manual plan reevaluation and a query of an unknown effect result (D7). The
browser names an operation, never an Action or a tenant: tenant, world and principal come from the inspected session;
the tenant's Action for the capability is looked up in SQL (0151); the governed entry re-checks EXECUTE, the human
subject, the registry and every fence. Only the workbench login realm is accepted (its own browser application and the
``__Host-nexloop_workbench`` cookie, slice 1); a WebChat customer cookie is never a workbench session (401), and a member
acts only within the grants of their role (an operator calling an owner-only operation is 403). The Idempotency-Key header is the governed request id (replays are answered from
the terminal outcome). Fixed error codes: unauthenticated 401, forbidden 403, not_found 404 (no Action published for it),
invalid_request 422, not_allowed_in_state 409, conflict 409, unavailable 503 (authority or a dependency cannot be verified).
"""
import asyncio
from datetime import datetime,timedelta
import json
import re
import uuid
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService
from nexloop_eios import consumer_twins
from nexloop_eios.browser_http import WORKBENCH_COOKIE
from nexloop_eios.goal_controls import CAPABILITIES

_HEX=re.compile(r'[0-9a-f]{64}')
_UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_KEY=re.compile(r'[A-Za-z0-9._~-]{16,160}')


def _reason(value):
    if type(value) is not str or not 1<=len(value)<=500:raise ValueError('reason')
    return value


def _utc(value):
    if type(value) is not str:raise ValueError('time')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset()!=timedelta(0):raise ValueError('UTC time required')
    return parsed


def _id(pattern):
    def check(value):
        if type(value) is not str or not pattern.fullmatch(value):raise ValueError('identifier')
        return value
    return check


def _bool(value):
    if type(value) is not bool:raise ValueError('boolean')
    return value


def _scope(value):
    if value not in ('tenant','role','consumer','strategy','action_type'):raise ValueError('scope')
    return value


def _text(value):
    if type(value) is not str or not 1<=len(value)<=255:raise ValueError('text')
    return value


# operation → its fields (each with a validator); every operation is a registered human capability (0150).
def _calibration_windows(v):
    """NX-042 T5: 1-3 windows {split_at YYYY-MM-DD, span_days 28|56|84}; overlap and dates are checked in SQL."""
    return (type(v) is list and 1<=len(v)<=3 and all(type(w) is dict and set(w)=={'split_at','span_days'} and type(w['split_at']) is str
        and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',w['split_at']) and type(w['span_days']) is int and w['span_days'] in (28,56,84) for w in v))



def _utc_text(value):
    """A UTC instant passed on as the client's text (SQL casts it)."""
    _utc(value)
    return value


def _kind(name):
    return lambda v:v if v==name else (_ for _ in ()).throw(ValueError('kind'))


_EXPERIMENT_ID=re.compile(r'[a-z][a-z0-9_.-]{0,63}')
# NX-043 report kinds and their fields, as control.nexloop_governed_read_experiment_report (0298) accepts them.
_REPORT_KINDS={
    'shadow':{'kind':_kind('shadow'),'world':_id(re.compile(r'shadow:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),
        'strategy_id':_id(_EXPERIMENT_ID),
        'strategy_version':lambda v:v if type(v) is int and 0<=v<=999999999 else (_ for _ in ()).throw(ValueError('version')),
        'since':_utc_text,'until':_utc_text},
    'experiment':{'kind':_kind('experiment'),'experiment_id':_id(_EXPERIMENT_ID),'as_of':_utc_text},
    'pooled':{'kind':_kind('pooled'),
        'experiment_ids':lambda v:v if type(v) is list and 2<=len(v)<=50 and all(type(x) is str and _EXPERIMENT_ID.fullmatch(x) for x in v)
            else (_ for _ in ()).throw(ValueError('experiment ids')),
        'as_of':_utc_text},
}

OPERATIONS={
    'set_control':{'scope_kind':_scope,'scope_ref':_text,'paused':_bool,'reason':_reason},
    'release_contact_restriction':{'consumer_id':_id(_HEX),'reason':_reason},
    'cancel_commitment':{'commitment_id':_id(_HEX),'reason':_reason},
    'extend_commitment':{'commitment_id':_id(_HEX),'due_at':_utc,'reason':_reason},
    'attest_commitment':{'commitment_id':_id(_HEX),'occurred_at':_utc,'reason':_reason},
    'commitment_condition_met':{'commitment_id':_id(_HEX),'reason':_reason},
    'mark_commitment_communication':{'commitment_id':_id(_HEX),'reason':_reason},
    'request_plan_reevaluation':{'plan_id':_id(_UUID),'reason':_reason},
    'request_effect_query':{'intent_id':_id(_UUID),'reason':_reason},
    # Slice 3 (D3): the duration is optional in the governed Action; the page always sends it (default from policy).
    'take_over_conversation':{'scope_kind':lambda v:v if v in ('conversation','consumer') else (_ for _ in ()).throw(ValueError('scope')),
        'scope_ref':_id(_HEX),'duration_seconds':lambda v:v if type(v) is int and 60<=v<=604800 else (_ for _ in ()).throw(ValueError('duration')),'reason':_reason},
    'hand_back_conversation':{'takeover_id':_id(_UUID),'reason':_reason},
    # Ruling B (0153): native WebChat only, bound to one inbound message.
    'send_staff_reply':{'conversation_id':_id(_HEX),'reply_to':_id(_HEX),'text':lambda v:v if type(v) is str and 1<=len(v)<=8192 else (_ for _ in ()).throw(ValueError('text'))},
    # NX-030 (0150): owner-only; selector null silences every selector of the rule; at most 7 days (SQL).
    'silence_alert':{'rule_id':_id(re.compile(r'[a-z][a-z0-9_]{0,63}')),'selector':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',v)) else (_ for _ in ()).throw(ValueError('selector')),
        'until':_utc,'reason':_reason},
    # NX-029 (0142): owner-only; erase one Consumer (blocking at once, purge by the keeper), with a reason.
    'erase_consumer':{'consumer_id':_id(_HEX),'reason':_reason},
    # NX-035 (0147): owner-only; the restore reference names the restore being opened.
    'open_restore_scope':{'restore_ref':_id(re.compile(r'[A-Za-z0-9._:-]{1,64}'))},
    # NX-053 (0154): owner-only; one of the key digest or the erasure request (the other null), with a reason.
    'revoke_contact_key':{'key_digest':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[0-9a-f]{64}',v)) else (_ for _ in ()).throw(ValueError('key')),
        'erasure_request_id':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,190}',v)) else (_ for _ in ()).throw(ValueError('request')),
        'reason':lambda v:v if type(v) is str and 1<=len(v.strip())<=500 else (_ for _ in ()).throw(ValueError('reason'))},
    # 0220 (ADR-034 first step): owner-only persona marking, and the erasure of a Consumer whose persona is not real.
    'set_consumer_persona':{'consumer_id':_id(_HEX),'persona':lambda v:v if v in ('real','simulated_agent','synthetic') else (_ for _ in ()).throw(ValueError('persona')),'reason':_reason},
    'erase_synthetic_consumer':{'consumer_id':_id(_HEX),'reason':_reason},
    # 0227 operator persona slice 1 (owner only): versions, assignment (persona_id null clears it), rules, AI disclosure.
    'publish_operator_persona':{'persona_id':_id(re.compile(r'[a-z][a-z0-9_-]{0,63}')),'version':lambda v:v if type(v) is int and 1<=v<=999999 else (_ for _ in ()).throw(ValueError('version')),
        'status':lambda v:v if v in ('published','retired') else (_ for _ in ()).throw(ValueError('status')),
        'document':lambda v:v if v is None or type(v) is dict else (_ for _ in ()).throw(ValueError('document')),'reason':_reason},
    'assign_operator_persona':{'consumer_id':_id(_HEX),'persona_id':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',v)) else (_ for _ in ()).throw(ValueError('persona')),
        'reason':_reason},
    'publish_persona_rules':{'version':lambda v:v if type(v) is int and 1<=v<=999999 else (_ for _ in ()).throw(ValueError('version')),
        'default_persona_id':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',v)) else (_ for _ in ()).throw(ValueError('persona')),
        'rules':lambda v:v if type(v) is list and len(v)<=50 else (_ for _ in ()).throw(ValueError('rules')),'reason':_reason},
    'set_ai_disclosure':{'mode':lambda v:v if v in ('none','first_contact','always') else (_ for _ in ()).throw(ValueError('mode')),
        'when_asked':lambda v:v if v in ('deflect','handoff') else (_ for _ in ()).throw(ValueError('when_asked')),
        'block_human_claims':_bool,'content_label':_bool,'reason':_reason},
    # 0228 (owner only): decline or reinstate one of a consumer's communication preferences.
    'dismiss_contact_preference':{'claim_id':_id(re.compile(r'[0-9a-f]{64}')),'dismissed':_bool,'reason':_reason},
    # 0232 (owner only): tenant outreach controls; confirm_consent_mode is null unless marketing is being switched on (O5).
    'set_outreach_controls':{'settings':lambda v:v if type(v) is dict else (_ for _ in ()).throw(ValueError('settings')),
        'confirm_consent_mode':lambda v:v if v in (None,'opt_out','opt_in') else (_ for _ in ()).throw(ValueError('consent')),'reason':_reason},
    # Outreach slice 3 (owner only): enable or disable a purpose of the current purpose list.
    'set_outreach_purpose':{'purpose_id':_id(re.compile(r'[a-z][a-z0-9_.]{0,63}')),'enabled':_bool,'reason':_reason},
    # Outreach slice 4 (owner only): one proactive message; accepted here, every gate rechecked at dispatch.
    'send_outreach':{'consumer_id':_id(_HEX),'purpose':_id(re.compile(r'[a-z][a-z0-9_.]{0,63}')),
        'origin_ref':_id(re.compile(r'(?:commitment|intent|plan|owner):[A-Za-z0-9._:-]{1,190}')),
        'body':lambda v:v if type(v) is str and 1<=len(v)<=2000 else (_ for _ in ()).throw(ValueError('body')),
        'channel':lambda v:v if v=='native.webchat' else (_ for _ in ()).throw(ValueError('channel'))},
    # Outreach slice 6: approve or reject one pending send (four eyes and the window checked in SQL).
    'decide_outreach_approval':{'intent_id':_id(re.compile(r'outreach:[0-9a-f]{64}')),
        'decision':lambda v:v if v in ('approve','reject') else (_ for _ in ()).throw(ValueError('decision')),'reason':_reason},
    # NX-056 A1: decide one waiting unified Approval, or withdraw one's own (every approver check in SQL).
    'decide_approval':{'approval_id':_id(re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),
        'decision':lambda v:v if v in ('approve','reject') else (_ for _ in ()).throw(ValueError('decision')),'reason':_reason},
    'cancel_approval':{'approval_id':_id(re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),'reason':_reason},
    # Outreach slice 7 (owner only): marketing consent granted / withdrawn, or marketing resumed after an unsubscribe, with evidence.
    'record_marketing_consent':{'consumer_id':_id(_HEX),'kind':lambda v:v if v in ('consent_granted','consent_withdrawn','marketing_resumed') else (_ for _ in ()).throw(ValueError('kind')),
        'source':lambda v:v if v in ('consumer_message','offline') else (_ for _ in ()).throw(ValueError('source')),
        'message_id':lambda v:v if v is None or (type(v) is str and _HEX.fullmatch(v)) else (_ for _ in ()).throw(ValueError('message')),
        'evidence_ref':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',v)) else (_ for _ in ()).throw(ValueError('evidence')),
        'reason':_reason},
    # NX-039 V4 (owner only): publish a semantic release from one evaluated candidate, or roll back to an earlier version.
    'publish_semantic_release':{'candidate_id':_id(_UUID),
        'items':lambda v:v if type(v) is list and 1<=len(v)<=20 and all(type(x) is dict for x in v) else (_ for _ in ()).throw(ValueError('items')),'reason':_reason},
    'rollback_semantic_release':{'target_version':_id(re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}')),'reason':_reason},
    # NX-039 V6 (owner only): stop or resume evolution for one dimension.
    # NX-041 S2 (owner only): create a simulation or shadow world, or delete one as a whole.
    'create_sim_world':{'kind':lambda v:v if v in ('simulation','shadow') else (_ for _ in ()).throw(ValueError('kind')),'purpose':_reason,
        'expires_in_days':lambda v:v if type(v) is int and 1<=v<=30 else (_ for _ in ()).throw(ValueError('expires_in_days'))},
    'delete_sim_world':{'world':_id(re.compile(r'(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),'reason':_reason},
    # NX-041 S3 (owner only): synthetic (segments) or structural (max_objects) data in a simulation world; the other field is null.
    # NX-041 D10 follow-up (owner only): the simulation world's Action catalog (the stand-in whitelist), definitions only.
    'publish_sim_catalog':{'world':_id(re.compile(r'(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),'reason':_reason},
    'generate_sim_world_data':{'world':_id(re.compile(r'sim:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),
        'kind':lambda v:v if v in ('synthetic','structural') else (_ for _ in ()).throw(ValueError('kind')),
        'seed':_id(re.compile(r'[A-Za-z0-9_.-]{1,64}')),
        'consumers':lambda v:v if type(v) is int and 1<=v<=2000 else (_ for _ in ()).throw(ValueError('consumers')),
        'events_per_consumer':lambda v:v if type(v) is dict and set(v)=={'min','max'} and all(type(x) is int and 0<=x<=50 for x in v.values()) else (_ for _ in ()).throw(ValueError('events')),
        'segments':lambda v:v if v is None or (type(v) is dict and 1<=len(v)<=10) else (_ for _ in ()).throw(ValueError('segments')),
        'max_objects':lambda v:v if v is None or (type(v) is int and 0<=v<=10000) else (_ for _ in ()).throw(ValueError('max_objects'))},
    # NX-041 S5 (owner only): move a simulation world's clock forward; seconds and rate are null unless their action uses them.
    'control_sim_clock':{'world':_id(re.compile(r'(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),
        'action':lambda v:v if v in ('advance','pause','resume','set_rate') else (_ for _ in ()).throw(ValueError('action')),
        'seconds':lambda v:v if v is None or (type(v) is int and 1<=v<=315360000) else (_ for _ in ()).throw(ValueError('seconds')),
        'rate':lambda v:v if v is None or (type(v) in (int,float) and 0<v<=100000) else (_ for _ in ()).throw(ValueError('rate')),'reason':_reason},
    # NX-042 T1 (owner only): a consumer twin version (definition checked in SQL) or its retirement.
    'publish_consumer_twin':{'definition':lambda v:v if type(v) is dict and v.get('schema')=='nexloop-consumer-twin/1' and not consumer_twins.definition_problems(v)
        else (_ for _ in ()).throw(ValueError('definition')),
        'calibration_report_ref':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'twin-calibration:[0-9a-f]{64}',v)) else (_ for _ in ()).throw(ValueError('report'))},
    'retire_consumer_twin':{'twin_id':_id(re.compile(r'[a-z][a-z0-9_.-]{0,63}')),
        'version':lambda v:v if type(v) is int and 1<=v<=999999999 else (_ for _ in ()).throw(ValueError('version')),'reason':_reason},
    'erase_consumer_twin_text':{'twin_id':_id(re.compile(r'[a-z][a-z0-9_.-]{0,63}')),
        'version':lambda v:v if type(v) is int and 1<=v<=999999999 else (_ for _ in ()).throw(ValueError('version')),'reason':_reason},
    # NX-042 T5 (owner only): calibrate a twin version over 1-3 non-overlapping calibration windows [split_at, split_at + span_days) (UTC days).
    'calibrate_consumer_twin':{'twin_id':_id(re.compile(r'[a-z][a-z0-9_.-]{0,63}')),
        'version':lambda v:v if type(v) is int and 1<=v<=999999999 else (_ for _ in ()).throw(ValueError('version')),
        'windows':lambda v:v if _calibration_windows(v) else (_ for _ in ()).throw(ValueError('windows'))},
    # NX-043 X1 (owner only): a strategy version (levers checked in SQL, D1), its retirement, and a preregistered experiment (D3).
    'publish_strategy':{'definition':lambda v:v if type(v) is dict and v.get('schema')=='nexloop-strategy-version/1' else (_ for _ in ()).throw(ValueError('definition'))},
    'retire_strategy':{'strategy_id':_id(re.compile(r'[a-z][a-z0-9_.-]{0,63}')),
        'version':lambda v:v if type(v) is int and 1<=v<=999999999 else (_ for _ in ()).throw(ValueError('version')),'reason':_reason},
    'register_experiment':{'registration':lambda v:v if type(v) is dict and v.get('schema')=='nexloop-experiment-registration/1'
        else (_ for _ in ()).throw(ValueError('registration'))},
    'start_experiment':{'experiment_id':_id(re.compile(r'[a-z][a-z0-9_.-]{0,63}'))},
    'stop_experiment':{'experiment_id':_id(re.compile(r'[a-z][a-z0-9_.-]{0,63}')),'reason':_reason},
    # NX-043 X3 (owner only): the shadow copy switch of the tenant (D7, off by default).
    'set_shadow_copy':{'world':_id(re.compile(r'shadow:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')),'enabled':_bool,'reason':_reason},
    # NX-043 (owner only): the frozen aggregate reports (shapes checked in SQL; the tenant and the actor are the server's).
    # The fields depend on the kind (0298): the spec is chosen by the body's kind, then its key set must match exactly as for any operation.
    'read_experiment_report':lambda body:_REPORT_KINDS[body.get('kind') if type(body.get('kind')) is str else None],
    'set_evolution_stop':{'dimension':lambda v:v if v in ('semantic_content','semantic_tool','ontology_schema') else (_ for _ in ()).throw(ValueError('dimension')),
        'stopped':_bool,'reason':_reason},
    # ADR-036 (owner only): the next version of a read-only Object Set; shapes and properties are checked in SQL.
    # Capability gap P2 (owner only): the next version of a read-only Object View; sections, properties and Links are checked in SQL.
    'publish_object_view':{'stable_name':_id(re.compile(r'[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}')),'object_type':_id(re.compile(r'[A-Z][A-Za-z0-9_]{0,63}')),
        'sections':lambda v:v if type(v) is list and 1<=len(v)<=8 and all(type(x) is dict for x in v) else (_ for _ in ()).throw(ValueError('sections')),
        'relations':lambda v:v if type(v) is list and len(v)<=8 and all(type(x) is str for x in v) else (_ for _ in ()).throw(ValueError('relations')),
        'title_property':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',v)) else (_ for _ in ()).throw(ValueError('title')),
        'reason':_reason},
    'publish_object_set':{'stable_name':_id(re.compile(r'[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}')),'object_type':_id(re.compile(r'[A-Z][A-Za-z0-9_]{0,63}')),
        'filters':lambda v:v if type(v) is list and len(v)<=8 and all(type(x) is dict for x in v) else (_ for _ in ()).throw(ValueError('filters')),
        'sort':lambda v:v if type(v) is list and len(v)<=3 and all(type(x) is dict for x in v) else (_ for _ in ()).throw(ValueError('sort')),
        'projection':lambda v:v if type(v) is list and 1<=len(v)<=12 and all(type(x) is str for x in v) else (_ for _ in ()).throw(ValueError('projection')),
        'reason':_reason},
    # 0210: owner-only; the owner confirms an unknown effect did not happen (evidence and wait checked in SQL).
    'confirm_effect_not_delivered':{'intent_id':_id(_UUID),'confirmation_ref':_id(re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')),'reason':_reason,
        'provider_retention_checked':_bool,'reply_followup':lambda v:v if v in ('owner_follow_up','fallback_reply') else (_ for _ in ()).throw(ValueError('follow-up'))},
    # 0220: owner-only real dispatch switch; the tenant scope names its data mode, the action scope one resource id.
    'set_real_dispatch':{'scope_kind':lambda v:v if v in ('tenant','action_type') else (_ for _ in ()).throw(ValueError('scope')),
        'enabled':lambda v:v if type(v) is bool else (_ for _ in ()).throw(ValueError('enabled')),'reason':_reason,
        'action_type':lambda v:v if v is None or (type(v) is str and re.fullmatch(r'eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}',v)) else (_ for _ in ()).throw(ValueError('action type')),
        'data_mode':lambda v:v if v in (None,'real','test') else (_ for _ in ()).throw(ValueError('data mode'))},
    # AT-043 (0145): owner-only; a catalog incentive for one Consumer, whole minor units; accepted is not issued (the
    # executor admits it at dispatch under the catalog, the per-Consumer limit and the tenant budget).
    'issue_incentive':{'consumer_id':_id(_HEX),'incentive_offering_id':_id(_HEX),
        'offering_revision':lambda v:v if type(v) is int and 1<=v<=9007199254740991 else (_ for _ in ()).throw(ValueError('revision')),
        'binding_id':_id(_HEX),'amount_minor':_id(re.compile(r'[1-9][0-9]{0,15}')),'currency':_id(re.compile(r'[A-Z]{3}')),
        'notification_text':lambda v:v if type(v) is str and len(v)<=2000 else (_ for _ in ()).throw(ValueError('notification'))},
}


def validate(operation,body):
    """The request body of an operation → its checked fields (ValueError/TypeError/LookupError when refused): the key set equals the
    operation's spec exactly and every field passes its check. A spec may be chosen by the body (read_experiment_report: by its kind)."""
    spec=OPERATIONS[operation]
    if type(body) is not dict:raise ValueError('body')
    if callable(spec):spec=spec(body)
    if set(body)!=set(spec):raise ValueError('fields')
    return {name:check(body[name]) for name,check in spec.items()}

class WorkbenchActions:
    """Governed human writes for one inspected workbench session (re-authenticated on every call)."""

    def __init__(self,backend,inspected_session):
        self.backend,self.inspected_session=backend,inspected_session

    def run(self,operation,request_id,fields):
        from nexloop_eios.authorization import authority_request_scope
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.goal_controls import GoalGovernedActions
        self.backend.authenticate_workbench(self.inspected_session)  # open backend + current Human authentication (workbench realm)
        with authority_request_scope():
            pool=self.backend._pool;human=authenticate_browser_business(pool,self.inspected_session,world='real')
            with pool.connection() as db:
                action=db.execute('select authz.nexloop_workbench_action_for(%s,%s,%s)',(human.token_digest,human.world,CAPABILITIES[operation])).fetchone()[0]
            if action is None:raise LookupError('Action not published')
            if action.get('granted') is not True:raise PermissionError('no grant on the Action')
            method=getattr(GoalGovernedActions(pool,human,self.backend._signer),operation)
            return method(action_name=action['stable_name'],action_version=action['version'],request_id=request_id,**fields)


def router(config,*,ports_for_browser):
    """config: the workbench BrowserConfiguration; ports_for_browser(request, inspected_session) → WorkbenchActions."""
    if not callable(ports_for_browser):raise ValueError('governed browser ports required')
    routes=APIRouter(prefix='/api/v1/workbench/actions')
    origin=urlsplit(config.origin)

    def error(code,status,details=None):
        return JSONResponse({'code':code,'message':'请求未完成','trace_id':str(uuid.uuid4()),'retryable':status==503,'details':details or {}},
            status_code=status,headers={'Cache-Control':'no-store'})

    def bound(request):
        if request.headers.get('host')!=origin.netloc or request.headers.get('origin')!=config.origin:raise PermissionError()
        store=getattr(request.app.state,'workbench_store',None)
        if store is None:raise RuntimeError()
        op=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
        sessions=BrowserSessionService(store,store,application_id=config.application_id,operator=op)
        session=sessions.inspect(request.cookies.get(WORKBENCH_COOKIE,''))
        sessions.verify_csrf(session,request.headers.get('x-csrf-token',''))
        if session.restricted:raise PermissionError()
        return ports_for_browser(request,session)

    @routes.post('/{operation}')
    async def act(operation:str,request:Request):
        try:
            if operation not in OPERATIONS or request.headers.get('content-type','').split(';')[0]!='application/json':raise ValueError()
            key=request.headers.get('idempotency-key','')
            if not _KEY.fullmatch(key):raise ValueError()
            data=b''
            async with asyncio.timeout(5):
                async for part in request.stream():
                    data+=part
                    if len(data)>8192:raise ValueError()
            def pairs(items):
                result={}
                for name,value in items:
                    if name in result:raise ValueError()
                    result[name]=value
                return result
            body=json.loads(data,object_pairs_hook=pairs)
            fields=validate(operation,body)
        except TimeoutError:
            return error('request_timeout',408)
        except (ValueError,UnicodeError,TypeError,KeyError):
            return error('invalid_request',422)
        try:
            result=await run_in_threadpool(lambda:bound(request).run(operation,'workbench-'+key,fields))
            return JSONResponse(json.loads(json.dumps(result,default=str)),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except CredentialInvalid:
            return error('unauthenticated',401)
        except LookupError:
            return error('not_found',404)
        except psycopg.errors.InvalidParameterValue as refused:
            # 0210: a determination refusal names its fixed code (never a value); other handlers stay without details.
            code=re.fullmatch(r'(?:effect determination|shadow copy|experiment report|consumer twin|strategy|experiment|consumer erasure|operator persona|contact preference|outreach controls|outreach purposes|outreach send|outreach approval|marketing consent|object sets|semantic release|evolution stop|object views|sim world|sim generation|sim clock): ([a-z_]{1,40})',refused.diag.message_primary or '')
            return error('not_allowed_in_state',409,{'reason':code.group(1)} if code else None)  # the handler refused this transition
        except psycopg.errors.SerializationFailure:
            return error('conflict',409)
        except (PermissionError,psycopg.errors.InsufficientPrivilege):
            return error('forbidden',403)
        except Exception as failure:
            if type(failure).__name__ in ('ActionAuthorizationDenied','AuthorizationFactDenied'):return error('forbidden',403)
            # AuthorizationUnavailable and dependencies: cannot be verified now (same label as NX-046), never a silent success.
            return error('unavailable',503)
    return routes
