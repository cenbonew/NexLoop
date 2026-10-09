"""Context pack v6 wire assembly (draft contract packages/contracts/context-pack-v6).

Pure: takes already-authorized inputs (v2/v3/v5-shaped sections, partitioned Items and
the budget outcome) and returns the canonical v6 body validated against the generated
contract model. It never reads or binds anything; NX-023-B binds it to a Run.
"""
from nexloop_eios.context_engine.budget import MANDATORY_SECTIONS,apply_budget
from nexloop_eios.context_engine.sections import assert_partition
from nexloop_eios.context_engine.strategy import PROTOCOL,strategy_ref
from nexloop_eios.contracts import ContextPackV6
from nexloop_eios.postgres_artifacts import canonical_payload

ITEM_SECTIONS=('constraints','consumer_state','open_work','evidence','semantics','experience')
MAX_PACK_BYTES=262144


def wire_item(item):
    return {'subsection':item.subsection,'ref':item.ref,'revision':item.revision,'content':item.content,'content_hash':item.content_hash,
        'evidence_kind':item.evidence_kind,'access_decision_ref':item.access_decision_ref,'relevance':item.relevance,'at':item.at,
        'tags':sorted(item.tags)}


def assemble_v6(*,strategy,bindings,role,current_event,goal,formal_facts,current_constraints,supply,items,extra_insufficient=(),estimator=None):
    """Returns (body, outcome). Pinned/mandatory items are never trimmed (see budget)."""
    items=assert_partition(list(items))
    if any(i.section in MANDATORY_SECTIONS and i.section!='constraints' for i in items):
        raise ValueError('bindings/role/goal/current_event are structured sections, not items')
    outcome=apply_budget(items,strategy,estimator=estimator)
    body={'schema_version':PROTOCOL,'strategy_ref':strategy_ref(strategy),'bindings':bindings,'role':role,'current_event':current_event,
        'goal':goal,'formal_facts':formal_facts,'current_constraints':current_constraints,'supply':supply,
        **{name:[wire_item(i) for i in outcome.kept if i.section==name] for name in ITEM_SECTIONS},
        'budget_report':outcome.report,'insufficient':list(outcome.insufficient)+list(extra_insufficient)}
    ContextPackV6.model_validate(body)
    if len(canonical_payload(body).encode())>MAX_PACK_BYTES:raise ValueError('context_pack_too_large')
    return body,outcome
