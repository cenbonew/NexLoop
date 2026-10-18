"""Context pack v6 wire assembly (contract packages/contracts/context-pack-v6).

Pure: takes already-authorized inputs (the v2 core sections, partitioned Items and the
budget outcome) and returns the canonical v6 body validated against the generated
contract model. It never reads or binds anything; ``context_artifacts`` binds it and
SQL re-verifies every source (0091).
"""
from nexloop_eios.context_engine.budget import MANDATORY_SECTIONS,apply_budget
from nexloop_eios.context_engine.sections import assert_partition
from nexloop_eios.context_engine.strategy import PROTOCOL,strategy_ref
from nexloop_eios.contracts import ContextPackV6
from nexloop_eios.postgres_artifacts import canonical_payload

ITEM_SECTIONS=('constraints','consumer_state','open_work','evidence','semantics','experience')
MAX_PACK_BYTES=65536  # same bound as the bound Run input (Host, 0053 bind)
# context-pack-v6.schema.json item caps per section: a pack beyond them is as unbindable as one beyond the bytes.
MAX_SECTION_ITEMS={'constraints':64,'consumer_state':256,'open_work':128,'evidence':256,'semantics':64,'experience':64}


def _too_large(body):
    return len(canonical_payload(body).encode())>MAX_PACK_BYTES or any(len(body[name])>limit for name,limit in MAX_SECTION_ITEMS.items())


def wire_item(item):
    return {'subsection':item.subsection,'ref':item.ref,'revision':item.revision,'content':item.content,'content_hash':item.content_hash,
        'evidence_kind':item.evidence_kind,'access_decision_ref':item.access_decision_ref,'relevance_permille':round(item.relevance*1000),'at':item.at,
        'tags':sorted(item.tags)}


def _assert_no_float(value):
    # The Host and SQL re-canonicalize the exact bytes; floats have no shared spelling.
    if isinstance(value,float):raise ValueError('context_pack_float')
    if isinstance(value,dict):
        for child in value.values():_assert_no_float(child)
    elif isinstance(value,list):
        for child in value:_assert_no_float(child)


def assemble_v6(*,strategy,bindings,role,current_event,goal,formal_facts,current_constraints,supply,items,user_statement=None,relationship_context=None,extra_insufficient=(),estimator=None,effect_supplies=None,operator_persona=None):
    """Returns (body, outcome). Pinned/mandatory items are never trimmed (see budget)."""
    items=assert_partition(list(items))
    if any(i.section in MANDATORY_SECTIONS and i.section!='constraints' for i in items):
        raise ValueError('bindings/role/goal/current_event are structured sections, not items')
    outcome=apply_budget(items,strategy,estimator=estimator)
    body=_body(strategy,bindings,role,current_event,user_statement,goal,formal_facts,current_constraints,supply,outcome,extra_insufficient,relationship_context,effect_supplies,operator_persona)
    # 10/18 stage: the token budget does not bound bytes (UTF-8 text, item metadata, the omission report). Over the bound
    # input size, the non-pinned section quotas (recent turns included) are halved until it fits; pinned items never go.
    tight=strategy
    while _too_large(body) and any(not i.pinned for i in outcome.kept):
        tight={**tight,'sections':{name:{k:(v//2 if k in ('max_items','recent_turns') else v) for k,v in quota.items()} for name,quota in tight['sections'].items()}}
        outcome=apply_budget(items,tight,estimator=estimator)
        body=_body(strategy,bindings,role,current_event,user_statement,goal,formal_facts,current_constraints,supply,outcome,extra_insufficient,relationship_context,effect_supplies,operator_persona)
        if all(q['max_items']==0 for q in tight['sections'].values()):break
    _assert_no_float(body)
    if _too_large(body):raise ValueError('context_pack_too_large')
    ContextPackV6.model_validate(body)
    return body,outcome


def _body(strategy,bindings,role,current_event,user_statement,goal,formal_facts,current_constraints,supply,outcome,extra_insufficient,relationship_context,effect_supplies,operator_persona):
    body={'schema_version':PROTOCOL,'strategy_ref':strategy_ref(strategy),'bindings':bindings,'role':role,'current_event':current_event,
        'user_statement':user_statement,'goal':goal,'formal_facts':formal_facts,'current_constraints':current_constraints,'supply':supply,
        **{name:[wire_item(i) for i in outcome.kept if i.section==name] for name in ITEM_SECTIONS},
        'budget_report':outcome.report,'insufficient':list(outcome.insufficient)+list(extra_insufficient)}
    # v4 relationship section, verbatim from the SQL-derived v4 core (optional in v6).
    if relationship_context is not None:body['relationship_context']=relationship_context
    # ADR-030: the enterprise slot's offering, verbatim from the v2 core (present only when the Run is bound to that slot).
    if effect_supplies is not None:body['effect_supplies']=effect_supplies
    # 0221 operator persona slice 1: persona@version for this Consumer, verbatim from SQL (re-derived and checked at bind).
    if operator_persona is not None:body['operator_persona']=operator_persona
    return body
