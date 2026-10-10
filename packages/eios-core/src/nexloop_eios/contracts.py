# Generated from canonical schemas; do not edit.
from __future__ import annotations
import json
from typing import Any, ClassVar, Literal
from pydantic import BaseModel, ConfigDict, model_validator
from jsonschema import Draft202012Validator, FormatChecker

class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
    _canonical_schema: ClassVar[dict[str, Any]]
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        return json.loads(json.dumps(cls._canonical_schema))
    @model_validator(mode="wrap")
    @classmethod
    def _validate_wire(cls, value: Any, handler: Any) -> Any:
        try:
            if isinstance(value, cls): value = value.model_dump(mode="json", exclude_unset=True, warnings=False)
            def check_json(item: Any) -> None:
                if type(item) in (str, int, float, bool, type(None)): return
                if type(item) is list:
                    for child in item: check_json(child)
                    return
                if type(item) is dict and all(type(key) is str for key in item):
                    for child in item.values(): check_json(child)
                    return
                raise ValueError()
            check_json(value)
            json.dumps(value, allow_nan=False)
        except Exception: raise ValueError("canonical contract validation failed") from None
        errors = list(Draft202012Validator(cls._canonical_schema, format_checker=FormatChecker()).iter_errors(value))
        if errors: raise ValueError("canonical contract validation failed")
        return handler(value)

class ActionIntentExpectedVersionsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_ref: str
    revision: int

class CandidateDefinitionProposed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = None
    display_name: str
    description: str | None = None
    owner_type_ref: str | None = None
    property_ref: str | None = None
    canonical_ref: str | None = None
    type_ref: str | None = None
    value_type: Literal['string', 'integer', 'decimal', 'boolean', 'date', 'datetime', 'enum', 'ref'] | None = None
    closed_vocabulary: bool | None = None
    property_group: Literal['demographics', 'needs_intent', 'purchase_behavior', 'preference', 'pain_point', 'spending_power', 'sentiment_attitude', 'lifestyle', 'channel_tech', 'other'] | None = None
    value: Any | None = None
    alias_text: str | None = None
    identifying_properties: dict[str, Any] | None = None
    strong_identifier: bool | None = None

class CandidateDefinitionRecallItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str
    method: Literal['vector', 'fts', 'trgm', 'strong_id', 'rule']
    score: float | int

class CandidateDefinitionMergeScores(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lexical_similarity: float | int | None = None
    core_term_containment: float | int | None = None
    vector_cluster: float | int | None = None
    rule_whitelist: float | int | None = None
    weighted_total: float | int | None = None
    best_match_ref: str | None = None
    threshold: float | int | None = None
    config_version: str | None = None

class ClaimSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal['consumer', 'enterprise', 'entity']
    ref: str
    text: str

class ClaimValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal['string', 'number', 'boolean', 'money', 'none']
    value: Any

class ClaimValidTime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: str
    kind: Literal['none', 'point', 'interval', 'deadline', 'unparsed', 'past_reference']
    status: Literal['absent', 'resolved', 'ambiguous', 'unresolved']
    granularity: Literal['minute', 'hour', 'day', 'part_of_day', 'am_pm_unspecified', 'period', 'month'] | None = None
    start: str | None
    end: str | None
    latest_bound_window: list[str] | None = None
    timezone: str
    anchor: str

class ClaimSource0(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_id: str
    sequence: int
    span_start: int
    span_end: int
    content_hash: str
    quote: str

class ContextManifestSourcesItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str
    revision: str
    content_hash: str
    evidence_kind: Literal['verified_fact', 'user_statement', 'hypothesis', 'policy', 'schema', 'memory', 'current_message', 'formal_object', 'conversation', 'execution_state']
    access_decision_ref: str

class ContextPackV6Bindings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tenant_id: str
    world_id: Literal['real']
    run_id: str
    source_principal: str
    context_id: str
    namespace: str
    artifact_id: str
    command_digest: str

class ContextPackV6Role1RoleBindingBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    tenant_id: str
    world: Literal['real']
    consumer_id: str
    link_id: str
    role_id: str
    step_id: str
    link_revision: int
    role_revision: int
    step_revision: int
    role_ref: str
    scope: str
    expires_at: str

class ContextPackV6Role1RoleBindingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    responsibility: str
    ceiling_ref: str
    active: Literal[True]
    valid_from: str
    valid_until: str

class ContextPackV6Role1RoleBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    binding: ContextPackV6Role1RoleBindingBinding
    definition: ContextPackV6Role1RoleBindingDefinition
    definition_provenance: str
    mapping_provenance: str
    grants_authority: Literal[False]

class ContextPackV6Role1RolePolicy1Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    tenant_id: str
    world: Literal['real']
    ceiling_id: str
    ceiling_revision: int
    scope_id: str
    scope_revision: int
    budget: dict[str, Any]
    effect_units: int
    expires_at: str

class ContextPackV6Role1RolePolicy1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    binding: ContextPackV6Role1RolePolicy1Binding
    ceiling: dict[str, Any]
    scope: dict[str, Any]
    ceiling_provenance: str
    scope_provenance: str
    grants_authority: Literal[False]

class ContextPackV6Role1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role_binding: ContextPackV6Role1RoleBinding
    role_policy: None | ContextPackV6Role1RolePolicy1

class ContextPackV6CurrentEvent0(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal['consumer_message']
    message_id: str
    provenance: str

class ContextPackV6CurrentEvent1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal['service_trigger']
    event_id: str
    source_principal: str
    body: str
    provenance: str

class ContextPackV6UserStatement0(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_id: str
    conversation_id: str
    sequence: int
    body: str
    provenance: str

class ContextPackV6GoalControlSnapshot1ScopesItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal['role', 'consumer', 'strategy', 'action_type']
    ref: str

class ContextPackV6GoalControlSnapshot1GoalsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_id: str
    version: int

class ContextPackV6GoalControlSnapshot1ObjectsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type_name: str
    object_id: str
    revision: int

class ContextPackV6GoalControlSnapshot1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    control_revision: int
    scopes: list[ContextPackV6GoalControlSnapshot1ScopesItem]
    goals: list[ContextPackV6GoalControlSnapshot1GoalsItem]
    objects: list[ContextPackV6GoalControlSnapshot1ObjectsItem]
    budgets: list[Literal['model', 'incentive']]

class ContextPackV6Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_version_refs: list[str]
    control_snapshot: None | ContextPackV6GoalControlSnapshot1

class ContextPackV6FormalFactsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal['Consumer', 'Goal', 'PlanStep', 'EffectControl']
    id: str
    revision: int
    provenance: str

class ContextPackV6CurrentConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal['nexloop.service.request:1']
    allow_effect: Literal[True]
    budget_units: int
    reserved_units: int
    valid_until: str
    executor_principal: str

class ContextPackV6SupplyProperties(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    service_code: Literal['local.json-export']
    title: str
    delivery_action: Literal['nexloop.service.request:1']
    content_kind: Literal['json-message-export']
    price_amount: Literal['0']
    currency: Literal['CNY']
    eligibility: Literal['current_consumer_plan']
    allowed_guarantees: list[Any]
    allowed_discounts: list[Any]
    evidence_kind: Literal['fsynced_json_export']
    active: Literal[True]
    valid_until: str

class ContextPackV6Supply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    offering_id: str
    offering_revision: int
    binding_id: str
    binding_revision: int
    provenance: str
    properties: ContextPackV6SupplyProperties

class ContextPackV6RelationshipContextCurrentStatementsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    assessment_ref: str
    revision: int
    relation_type_ref: str | None
    source_ref: str
    target_ref: str
    epistemic_kind: Literal['hypothesis', 'user_statement']
    resolution_state: Literal['resolved', 'awaiting_definition', 'unresolved']
    conclusion: str
    valid_from: str
    valid_to: str | None
    source_message_ref: str | None
    source_content_hash: str | None

class ContextPackV6RelationshipContextEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    assessment_ref: str
    revision: int
    relation_type_ref: str | None
    source_ref: str
    target_ref: str
    epistemic_kind: Literal['hypothesis', 'user_statement']
    resolution_state: Literal['resolved', 'awaiting_definition', 'unresolved']
    conclusion: str
    valid_from: str
    valid_to: str | None
    source_message_ref: str | None
    source_content_hash: str | None

class ContextPackV6RelationshipContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_statements: list[ContextPackV6RelationshipContextCurrentStatementsItem]
    evidence: list[ContextPackV6RelationshipContextEvidenceItem]

class ContextPackV6ConstraintsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subsection: str
    ref: str
    revision: str
    content: Any
    content_hash: str
    evidence_kind: Literal['formal_object', 'policy']
    access_decision_ref: str
    relevance_permille: int
    at: str
    tags: list[Literal['negation', 'contact_limit', 'unconfirmed']]

class ContextPackV6ConsumerStateItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subsection: str
    ref: str
    revision: str
    content: Any
    content_hash: str
    evidence_kind: Literal['formal_object', 'policy']
    access_decision_ref: str
    relevance_permille: int
    at: str
    tags: list[Literal['negation', 'contact_limit', 'unconfirmed']]

class ContextPackV6OpenWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subsection: str
    ref: str
    revision: str
    content: Any
    content_hash: str
    evidence_kind: Literal['formal_object', 'execution_state', 'policy']
    access_decision_ref: str
    relevance_permille: int
    at: str
    tags: list[Literal['negation', 'contact_limit', 'unconfirmed']]

class ContextPackV6EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subsection: Literal['conversation', 'claim_evidence', 'relationships', 'recall', 'hypotheses']
    ref: str
    revision: str
    content: Any
    content_hash: str
    evidence_kind: Literal['user_statement', 'conversation', 'hypothesis', 'memory']
    access_decision_ref: str
    relevance_permille: int
    at: str
    tags: list[Literal['negation', 'contact_limit', 'unconfirmed']]

class ContextPackV6SemanticsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subsection: str
    ref: str
    revision: str
    content: Any
    content_hash: str
    evidence_kind: Literal['schema']
    access_decision_ref: str
    relevance_permille: int
    at: str
    tags: list[Literal['negation', 'contact_limit', 'unconfirmed']]

class ContextPackV6ExperienceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subsection: str
    ref: str
    revision: str
    content: Any
    content_hash: str
    evidence_kind: Literal['memory']
    access_decision_ref: str
    relevance_permille: int
    at: str
    tags: list[Literal['negation', 'contact_limit', 'unconfirmed']]

class ContextPackV6BudgetReportOmittedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    section: str
    subsection: str
    ref: str
    reason: Literal['section_quota', 'mandatory_exceeds_budget', 'core_exceeds_budget', 'duplicate_evidence', 'experience', 'hypotheses', 'semantics_low_relevance', 'older_conversation', 'older_claim_evidence', 'low_score_recall']

class ContextPackV6BudgetReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    estimator: str
    input_token_budget: int
    output_reserve: int
    framing_reserve: int
    available: int
    used: int
    sections: dict[str, Any]
    omitted: list[ContextPackV6BudgetReportOmittedItem]

class ContextPackV6InsufficientItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: Literal['mandatory_exceeds_budget', 'core_trimmed', 'required_source_unreadable', 'required_source_stale', 'goal_not_current', 'control_paused', 'semantic_ambiguous_required']
    section: None | str
    refs: list[str]

class ConversationMessageProvider1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    namespace: str
    message_ref: None | str
    sequence: None | int
    sent_at: None | str
    trust: Literal['server', 'signed', 'client']
    skewed: bool

class OntologyMutationOperationsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal['create_object', 'set_property', 'invalidate_property', 'link_relation', 'end_relation', 'supersede_claim']
    target_ref: str
    type_ref: str | None = None
    expected_revision: int | None
    property_name: str | None = None
    value: Any | None = None
    relation_type_ref: str | None = None
    to_ref: str | None = None
    valid_from: str | None = None
    valid_to: str | None | None = None
    evidence_refs: list[str]

class ReviewDecisionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_revision_before: str
    schema_revision_after: str | None = None
    published_refs: list[str] | None = None
    applied_claim_count: int | None = None
    gate_failures: list[str] | None = None

class RunCommandBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    maximum_model_turns: int
    maximum_tool_calls: int
    active_timeout_seconds: int
    maximum_cost: str
    currency: str

class RunOutcomePlanUpdate1StepsItemStopIfItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type_name: str
    object_id: str
    property: str
    equals: Any

class RunOutcomePlanUpdate1StepsItemBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    maximum_model_turns: int
    maximum_tool_calls: int
    active_timeout_seconds: int

class RunOutcomePlanUpdate1StepsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    step_key: str
    step_object_id: None | str
    prerequisites: list[str]
    expected_result: str
    stop_if: list[RunOutcomePlanUpdate1StepsItemStopIfItem]
    reassess_at: None | str
    budget: RunOutcomePlanUpdate1StepsItemBudget
    intent_ref: None | str

class RunOutcomePlanUpdate1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy: None | str
    steps: list[RunOutcomePlanUpdate1StepsItem]

class ActionIntent(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:action-intent","title":"action-intent","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"intent_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"run_id":{"type":"string","format":"uuid"},"action_name":{"type":"string","pattern":"^nexloop\\\\.[a-z][a-z0-9_.]+$"},"contract_version":{"type":"string","minLength":1},"actor_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"consumer_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"goal_version_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"plan_step_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"idempotency_key":{"type":"string","minLength":16,"maxLength":200},"payload_digest":{"type":"string","pattern":"^[a-f0-9]{64}$"},"expected_versions":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"resource_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"revision":{"type":"integer","minimum":1}},"required":["resource_ref","revision"]},"minItems":1,"maxItems":256},"requested_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"not_after":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"evidence_refs":{"type":"array","items":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"minItems":1,"maxItems":256},"parameters":{"type":"object"},"decision_rationale":{"type":"string","minLength":1,"maxLength":2000}},"required":["schema_version","intent_id","tenant_id","world_id","mode","run_id","action_name","contract_version","actor_ref","consumer_ref","goal_version_ref","plan_step_ref","idempotency_key","payload_digest","expected_versions","requested_at","not_after","evidence_refs","parameters","decision_rationale"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}}]}')
    schema_version: Literal['1.0']
    intent_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    run_id: str
    action_name: str
    contract_version: str
    actor_ref: str
    consumer_ref: str
    goal_version_ref: str
    plan_step_ref: str
    idempotency_key: str
    payload_digest: str
    expected_versions: list[ActionIntentExpectedVersionsItem]
    requested_at: str
    not_after: str
    evidence_refs: list[str]
    parameters: dict[str, Any]
    decision_rationale: str

class CandidateDefinition(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:candidate-definition","title":"candidate-definition","description":"ADR-019. A proposed new ontology definition or instance that did not match the recalled schema/instances. Staged for merge or human review; never applied directly. Server-derived identity required. Not an existing upstream API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"candidate_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"kind":{"enum":["object_type","property","vocabulary_value","alias","object_instance"]},"extraction_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"source_content_hash":{"type":"string","pattern":"^[a-f0-9]{64}$"},"extractor_version":{"type":"string","minLength":1},"ontology_schema_revision":{"type":"string","minLength":1},"proposed":{"type":"object","additionalProperties":false,"properties":{"name":{"type":"string","minLength":1,"maxLength":128,"pattern":"^[a-z][a-z0-9_]*$"},"display_name":{"type":"string","minLength":1,"maxLength":200},"description":{"type":"string","maxLength":2000},"owner_type_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"property_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"canonical_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"type_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"value_type":{"enum":["string","integer","decimal","boolean","date","datetime","enum","ref"]},"closed_vocabulary":{"type":"boolean"},"property_group":{"enum":["demographics","needs_intent","purchase_behavior","preference","pain_point","spending_power","sentiment_attitude","lifestyle","channel_tech","other"]},"value":{},"alias_text":{"type":"string","minLength":1,"maxLength":200},"identifying_properties":{"type":"object"},"strong_identifier":{"type":"boolean"}},"required":["display_name"]},"recall":{"type":"array","maxItems":50,"items":{"type":"object","additionalProperties":false,"properties":{"ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"method":{"enum":["vector","fts","trgm","strong_id","rule"]},"score":{"type":"number","minimum":0,"maximum":1}},"required":["ref","method","score"]}},"merge_scores":{"type":"object","additionalProperties":false,"properties":{"lexical_similarity":{"type":"number","minimum":0,"maximum":1},"core_term_containment":{"type":"number","minimum":0,"maximum":1},"vector_cluster":{"type":"number","minimum":0,"maximum":1},"rule_whitelist":{"type":"number","minimum":0,"maximum":1},"weighted_total":{"type":"number","minimum":0,"maximum":1},"best_match_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"threshold":{"type":"number","minimum":0,"maximum":1},"config_version":{"type":"string","minLength":1}}},"status":{"enum":["staged","merged","pending_review","published","rejected","superseded"]},"dependent_claim_refs":{"type":"array","minItems":1,"maxItems":1024,"items":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"}},"evidence_refs":{"type":"array","minItems":1,"maxItems":256,"items":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"}},"created_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}},"required":["schema_version","candidate_id","tenant_id","world_id","mode","kind","extraction_ref","source_content_hash","extractor_version","ontology_schema_revision","proposed","recall","status","dependent_claim_refs","evidence_refs","created_at"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}},{"if":{"properties":{"kind":{"const":"property"}}},"then":{"properties":{"proposed":{"required":["name","owner_type_ref","value_type","closed_vocabulary","property_group"]}}}},{"if":{"properties":{"kind":{"const":"object_type"}}},"then":{"properties":{"proposed":{"required":["name"]}}}},{"if":{"properties":{"kind":{"const":"vocabulary_value"}}},"then":{"properties":{"proposed":{"required":["property_ref","value"]}}}},{"if":{"properties":{"kind":{"const":"alias"}}},"then":{"properties":{"proposed":{"required":["canonical_ref","alias_text"]}}}},{"if":{"properties":{"kind":{"const":"object_instance"}}},"then":{"properties":{"proposed":{"required":["type_ref","identifying_properties","strong_identifier"]}}}},{"if":{"properties":{"status":{"enum":["merged","pending_review"]}}},"then":{"required":["merge_scores"]}}]}')
    schema_version: Literal['1.0']
    candidate_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    kind: Literal['object_type', 'property', 'vocabulary_value', 'alias', 'object_instance']
    extraction_ref: str
    source_content_hash: str
    extractor_version: str
    ontology_schema_revision: str
    proposed: CandidateDefinitionProposed
    recall: list[CandidateDefinitionRecallItem]
    merge_scores: CandidateDefinitionMergeScores | None = None
    status: Literal['staged', 'merged', 'pending_review', 'published', 'rejected', 'superseded']
    dependent_claim_refs: list[str]
    evidence_refs: list[str]
    created_at: str

class Claim(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:claim","title":"claim","description":"NX-019. One evidence-bound Claim extracted from persisted conversation Messages: candidate knowledge, never a formal business object. Identity, tenant, world, consumer and evidence are server-derived; extractors never produce verified_fact. Not an existing upstream API.","type":"object","additionalProperties":false,"properties":{"claim_id":{"type":"string","pattern":"^[0-9a-f]{64}$"},"tenant_id":{"type":"string","minLength":1},"world_id":{"type":"string","minLength":1},"conversation_id":{"type":"string","pattern":"^[0-9a-f]{64}$"},"consumer_id":{"type":"string","pattern":"^[0-9a-f]{64}$"},"topic_key":{"type":"string","pattern":"^[0-9a-f]{64}$"},"subject":{"type":"object","additionalProperties":false,"required":["kind","ref","text"],"properties":{"kind":{"enum":["consumer","enterprise","entity"]},"ref":{"type":"string","description":"consumer: server-derived consumer object id; otherwise empty"},"text":{"type":"string","maxLength":200,"description":"referent words present in the topic window, or empty"}},"if":{"properties":{"kind":{"const":"consumer"}}},"then":{"properties":{"ref":{"type":"string","pattern":"^[0-9a-f]{64}$"}}},"else":{"properties":{"ref":{"const":""}}}},"predicate":{"type":"string","minLength":1,"maxLength":120},"value":{"type":"object","additionalProperties":false,"required":["type","value"],"properties":{"type":{"enum":["string","number","boolean","money","none"]},"value":{}},"allOf":[{"if":{"properties":{"type":{"const":"string"}}},"then":{"properties":{"value":{"type":"string","minLength":1,"maxLength":2000}}}},{"if":{"properties":{"type":{"const":"number"}}},"then":{"properties":{"value":{"type":"number"}}}},{"if":{"properties":{"type":{"const":"boolean"}}},"then":{"properties":{"value":{"type":"boolean"}}}},{"if":{"properties":{"type":{"const":"money"}}},"then":{"properties":{"value":{"type":"object","additionalProperties":false,"required":["amount","currency"],"properties":{"amount":{"type":"number"},"currency":{"type":"string","pattern":"^[A-Z]{3}$"}}}}}},{"if":{"properties":{"type":{"const":"none"}}},"then":{"properties":{"value":{"type":"null"}}}}]},"speaker":{"enum":["consumer","agent"]},"polarity":{"enum":["affirmed","negated"]},"modality":{"enum":["asserted","conditional","tentative","requested"]},"condition":{"type":"string","maxLength":400,"description":"verbatim condition clause; required when modality=conditional"},"time_expression":{"type":"string","maxLength":80},"valid_time":{"type":"object","additionalProperties":false,"required":["expression","kind","status","start","end","timezone","anchor"],"properties":{"expression":{"type":"string","maxLength":80},"kind":{"enum":["none","point","interval","deadline","unparsed","past_reference"]},"status":{"enum":["absent","resolved","ambiguous","unresolved"]},"granularity":{"enum":["minute","hour","day","part_of_day","am_pm_unspecified","period","month"]},"start":{"anyOf":[{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},{"type":"null"}]},"end":{"anyOf":[{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},{"type":"null"}]},"latest_bound_window":{"type":"array","items":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"minItems":2,"maxItems":2},"timezone":{"type":"string","minLength":1,"maxLength":64,"description":"IANA tenant timezone"},"anchor":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$","description":"server acceptance time of the cited Message"}},"allOf":[{"if":{"properties":{"status":{"enum":["absent","unresolved"]}}},"then":{"properties":{"start":{"type":"null"},"end":{"type":"null"}}},"else":{"properties":{"start":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"end":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}}}}]},"source":{"anyOf":[{"type":"object","additionalProperties":false,"required":["message_id","sequence","span_start","span_end","content_hash","quote"],"properties":{"message_id":{"type":"string","pattern":"^[0-9a-f]{64}$"},"sequence":{"type":"integer","minimum":1},"span_start":{"type":"integer","minimum":0,"description":"Unicode code point offset into the Message body"},"span_end":{"type":"integer","minimum":1},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"sha256 of the full Message body (UTF-8)"},"quote":{"type":"string","minLength":1}}},{"type":"null"}]},"derived_from":{"type":"array","items":{"type":"string","pattern":"^[0-9a-f]{64}$"},"uniqueItems":true,"maxItems":256},"corrects_claim_id":{"anyOf":[{"type":"string","pattern":"^[0-9a-f]{64}$"},{"type":"null"}]},"extractor_version":{"type":"string","minLength":1,"maxLength":80},"confidence":{"type":"number","minimum":0,"maximum":1,"description":"extractor self-estimate, not calibrated"},"epistemic_kind":{"enum":["user_statement","verified_fact","preference","constraint","intent","need_problem","commitment","hypothesis","correction"]},"resolution_state":{"enum":["unresolved","hypothesis_only","needs_resolution","awaiting_definition","rejected_definition","resolved","superseded"]},"correlation_key":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"same consumer+kind+predicate+normalized value+polarity; links repeats, never deduplicates"},"guard_flags":{"type":"array","items":{"type":"string","minLength":1,"maxLength":80},"uniqueItems":true,"maxItems":64},"recorded_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}},"required":["claim_id","tenant_id","world_id","conversation_id","consumer_id","topic_key","subject","predicate","value","speaker","polarity","modality","condition","time_expression","valid_time","source","derived_from","corrects_claim_id","extractor_version","confidence","epistemic_kind","resolution_state","correlation_key","guard_flags","recorded_at"],"allOf":[{"description":"extractors never produce verified_fact; only verification channels may, outside this contract","properties":{"epistemic_kind":{"not":{"const":"verified_fact"}}}},{"if":{"properties":{"epistemic_kind":{"const":"hypothesis"}}},"then":{"properties":{"resolution_state":{"enum":["hypothesis_only","superseded"]}},"anyOf":[{"properties":{"source":{"type":"object"}}},{"properties":{"derived_from":{"minItems":1}}}]},"else":{"properties":{"source":{"type":"object"},"derived_from":{"maxItems":0},"resolution_state":{"not":{"const":"hypothesis_only"}}}}},{"if":{"properties":{"modality":{"const":"conditional"}}},"then":{"properties":{"condition":{"minLength":1}}}},{"if":{"properties":{"corrects_claim_id":{"type":"string"}}},"then":{"properties":{"epistemic_kind":{"const":"correction"}}}},{"if":{"properties":{"speaker":{"const":"agent"}}},"then":{"properties":{"epistemic_kind":{"enum":["commitment","hypothesis"]}}}},{"if":{"properties":{"speaker":{"const":"consumer"}}},"then":{"properties":{"epistemic_kind":{"not":{"const":"commitment"}}}}}]}')
    claim_id: str
    tenant_id: str
    world_id: str
    conversation_id: str
    consumer_id: str
    topic_key: str
    subject: ClaimSubject
    predicate: str
    value: ClaimValue
    speaker: Literal['consumer', 'agent']
    polarity: Literal['affirmed', 'negated']
    modality: Literal['asserted', 'conditional', 'tentative', 'requested']
    condition: str
    time_expression: str
    valid_time: ClaimValidTime
    source: ClaimSource0 | None
    derived_from: list[str]
    corrects_claim_id: str | None
    extractor_version: str
    confidence: float | int
    epistemic_kind: Literal['user_statement', 'verified_fact', 'preference', 'constraint', 'intent', 'need_problem', 'commitment', 'hypothesis', 'correction']
    resolution_state: Literal['unresolved', 'hypothesis_only', 'needs_resolution', 'awaiting_definition', 'rejected_definition', 'resolved', 'superseded']
    correlation_key: str
    guard_flags: list[str]
    recorded_at: str

class ContextManifest(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:context-manifest","title":"context-manifest","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"context_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"run_id":{"type":"string","format":"uuid"},"call_sequence":{"type":"integer","minimum":1},"goal_version_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"policy_revision":{"type":"string","minLength":1},"ontology_schema_revision":{"type":"string","minLength":1},"semantic_snapshot_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"context_strategy_version":{"type":"string","minLength":1},"model_provider":{"type":"string","minLength":1},"model_id":{"type":"string","minLength":1},"embedding_profile_ref":{"anyOf":[{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},{"type":"null"}]},"sources":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"revision":{"type":"string","minLength":1},"content_hash":{"type":"string","pattern":"^[a-f0-9]{64}$"},"evidence_kind":{"enum":["verified_fact","user_statement","hypothesis","policy","schema","memory","current_message","formal_object","conversation","execution_state"]},"access_decision_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"}},"required":["ref","revision","content_hash","evidence_kind","access_decision_ref"]},"minItems":1,"maxItems":256},"prompt_artifact_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"request_digest":{"type":"string","pattern":"^[a-f0-9]{64}$"},"input_token_budget":{"type":"integer","minimum":256},"output_token_budget":{"type":"integer","minimum":128},"redaction_policy_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"created_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}},"required":["schema_version","context_id","tenant_id","world_id","mode","run_id","call_sequence","goal_version_ref","policy_revision","ontology_schema_revision","semantic_snapshot_ref","context_strategy_version","model_provider","model_id","embedding_profile_ref","sources","prompt_artifact_ref","request_digest","input_token_budget","output_token_budget","redaction_policy_ref","created_at"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}}]}')
    schema_version: Literal['1.0']
    context_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    run_id: str
    call_sequence: int
    goal_version_ref: str
    policy_revision: str
    ontology_schema_revision: str
    semantic_snapshot_ref: str
    context_strategy_version: str
    model_provider: str
    model_id: str
    embedding_profile_ref: str | None
    sources: list[ContextManifestSourcesItem]
    prompt_artifact_ref: str
    request_digest: str
    input_token_budget: int
    output_token_budget: int
    redaction_policy_ref: str
    created_at: str

class ContextPackV6(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:context-pack-v6","title":"context-pack-v6","description":"NX-023 Context pack v6. Message Runs: the frozen v2 core (bindings, user_statement, formal_facts, current_constraints, supply), or the v4 core when the optional relationship_context section is present (current statements are resolved user statements only; hypotheses are evidence only), re-derived by SQL at bind; Role Runs: the frozen v3/v5 core under role and a service-trigger current_event, plus strategy, goal/control snapshot, labelled item sections, budget report and explicit insufficiency. Every item cites its source revision, canonical content hash and the current READ decision it was read under; SQL re-verifies each against the source row. Formal sections carry only governed objects/policy/ledger state; Claims under review appear only as source text; hypotheses never enter a real-world Context. No floating-point numbers. Server-assembled; grants nothing. v2-v5 stay frozen and old Runs keep using them.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"nexloop.context-pack.v6"},"strategy_ref":{"type":"string","pattern":"^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$"},"bindings":{"type":"object","properties":{"tenant_id":{"type":"string","format":"uuid"},"world_id":{"const":"real"},"run_id":{"type":"string","format":"uuid"},"source_principal":{"type":"string","minLength":1,"maxLength":512},"context_id":{"type":"string","format":"uuid"},"namespace":{"type":"string","pattern":"^[a-f0-9]{64}$"},"artifact_id":{"type":"string","pattern":"^[a-f0-9]{32}$"},"command_digest":{"type":"string","pattern":"^[a-f0-9]{64}$"}},"required":["artifact_id","command_digest","context_id","namespace","run_id","source_principal","tenant_id","world_id"],"additionalProperties":false},"role":{"anyOf":[{"type":"null"},{"type":"object","additionalProperties":false,"properties":{"role_binding":{"type":"object","properties":{"binding":{"type":"object","properties":{"run_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world":{"const":"real"},"consumer_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"link_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"role_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"step_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"link_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"role_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"step_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"role_ref":{"type":"string","pattern":"^role:[a-f0-9]{64}:mapping:[a-f0-9]{64}$"},"scope":{"type":"string","minLength":1,"maxLength":8192},"expires_at":{"type":"string","format":"date-time","pattern":"(Z|\\\\+00:00)$"}},"required":["consumer_id","expires_at","link_id","link_revision","role_id","role_ref","role_revision","run_id","scope","step_id","step_revision","tenant_id","world"],"additionalProperties":false},"definition":{"type":"object","properties":{"name":{"type":"string","minLength":1,"maxLength":8192},"responsibility":{"type":"string","minLength":1,"maxLength":8192},"ceiling_ref":{"type":"string","minLength":1,"maxLength":8192},"active":{"const":true},"valid_from":{"type":"string","format":"date-time","pattern":"(Z|\\\\+00:00)$"},"valid_until":{"type":"string","format":"date-time","pattern":"(Z|\\\\+00:00)$"}},"required":["active","ceiling_ref","name","responsibility","valid_from","valid_until"],"additionalProperties":false},"definition_provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"},"mapping_provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"},"grants_authority":{"const":false}},"required":["binding","definition","definition_provenance","grants_authority","mapping_provenance"],"additionalProperties":false},"role_policy":{"anyOf":[{"type":"null"},{"type":"object","properties":{"binding":{"type":"object","properties":{"run_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world":{"const":"real"},"ceiling_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"ceiling_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"scope_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"scope_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"budget":{"type":"object"},"effect_units":{"type":"integer","minimum":1,"maximum":1000000},"expires_at":{"type":"string","format":"date-time","pattern":"(Z|\\\\+00:00)$"}},"required":["budget","ceiling_id","ceiling_revision","effect_units","expires_at","run_id","scope_id","scope_revision","tenant_id","world"],"additionalProperties":false},"ceiling":{"type":"object"},"scope":{"type":"object"},"ceiling_provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"},"scope_provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"},"grants_authority":{"const":false}},"required":["binding","ceiling","ceiling_provenance","grants_authority","scope","scope_provenance"],"additionalProperties":false}]}},"required":["role_binding","role_policy"]}]},"current_event":{"anyOf":[{"type":"object","properties":{"kind":{"const":"consumer_message"},"message_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"}},"required":["kind","message_id","provenance"],"additionalProperties":false},{"type":"object","properties":{"kind":{"const":"service_trigger"},"event_id":{"type":"string","format":"uuid"},"source_principal":{"type":"string","minLength":1,"maxLength":512},"body":{"type":"string","minLength":1,"maxLength":8192},"provenance":{"type":"string","pattern":"^eios:role-trigger:[a-f0-9-]{36}$"}},"required":["body","event_id","kind","provenance","source_principal"],"additionalProperties":false}]},"user_statement":{"anyOf":[{"type":"object","properties":{"message_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"conversation_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"sequence":{"type":"integer","minimum":1,"maximum":9007199254740991},"body":{"type":"string","maxLength":32768},"provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"}},"required":["body","conversation_id","message_id","provenance","sequence"],"additionalProperties":false},{"type":"null"}]},"goal":{"type":"object","additionalProperties":false,"properties":{"goal_version_refs":{"type":"array","items":{"type":"string","pattern":"^goal:[a-z0-9][a-z0-9._-]{0,127}@[1-9][0-9]*$"},"maxItems":16,"uniqueItems":true},"control_snapshot":{"anyOf":[{"type":"null"},{"type":"object","additionalProperties":false,"properties":{"control_revision":{"type":"integer","minimum":0},"scopes":{"type":"array","maxItems":32,"items":{"type":"object","additionalProperties":false,"properties":{"kind":{"enum":["role","consumer","strategy","action_type"]},"ref":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$"}},"required":["kind","ref"]}},"goals":{"type":"array","maxItems":16,"items":{"type":"object","additionalProperties":false,"properties":{"goal_id":{"type":"string","pattern":"^[a-z0-9][a-z0-9._-]{0,127}$"},"version":{"type":"integer","minimum":1}},"required":["goal_id","version"]}},"objects":{"type":"array","maxItems":32,"items":{"type":"object","additionalProperties":false,"properties":{"type_name":{"type":"string","pattern":"^[A-Za-z][A-Za-z0-9_]{0,63}$"},"object_id":{"type":"string","minLength":1,"maxLength":128},"revision":{"type":"integer","minimum":1}},"required":["object_id","revision","type_name"]}},"budgets":{"type":"array","maxItems":2,"uniqueItems":true,"items":{"enum":["model","incentive"]}}},"required":["budgets","control_revision","goals","objects","scopes"]}]}},"required":["control_snapshot","goal_version_refs"]},"formal_facts":{"type":"array","items":{"type":"object","properties":{"type":{"enum":["Consumer","Goal","PlanStep","EffectControl"]},"id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"}},"required":["id","provenance","revision","type"],"additionalProperties":false},"minItems":4,"maxItems":4},"current_constraints":{"type":"object","properties":{"action":{"const":"nexloop.service.request:1"},"allow_effect":{"const":true},"budget_units":{"type":"integer","minimum":0,"maximum":9007199254740991},"reserved_units":{"type":"integer","minimum":0,"maximum":9007199254740991},"valid_until":{"type":"string","format":"date-time"},"executor_principal":{"type":"string","minLength":1,"maxLength":512}},"required":["action","allow_effect","budget_units","executor_principal","reserved_units","valid_until"],"additionalProperties":false},"supply":{"type":"object","properties":{"offering_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"offering_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"binding_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"binding_revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"provenance":{"type":"string","pattern":"^eios:object:[a-f0-9]{64}$"},"properties":{"type":"object","properties":{"service_code":{"const":"local.json-export"},"title":{"type":"string","minLength":1,"maxLength":256},"delivery_action":{"const":"nexloop.service.request:1"},"content_kind":{"const":"json-message-export"},"price_amount":{"const":"0"},"currency":{"const":"CNY"},"eligibility":{"const":"current_consumer_plan"},"allowed_guarantees":{"type":"array","maxItems":0},"allowed_discounts":{"type":"array","maxItems":0},"evidence_kind":{"const":"fsynced_json_export"},"active":{"const":true},"valid_until":{"type":"string","format":"date-time"}},"required":["active","allowed_discounts","allowed_guarantees","content_kind","currency","delivery_action","eligibility","evidence_kind","price_amount","service_code","title","valid_until"],"additionalProperties":false}},"required":["binding_id","binding_revision","offering_id","offering_revision","properties","provenance"],"additionalProperties":false},"relationship_context":{"type":"object","properties":{"current_statements":{"type":"array","items":{"type":"object","properties":{"assessment_ref":{"type":"string","pattern":"^eios:object:RelationshipAssessment/[a-f0-9]{64}$"},"revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"relation_type_ref":{"type":["string","null"],"pattern":"^eios:link_type:[A-Za-z][A-Za-z0-9_]*:[1-9][0-9]*$"},"source_ref":{"type":"string","pattern":"^eios:object:[A-Za-z][A-Za-z0-9_]*/[a-f0-9]{64}$"},"target_ref":{"type":"string","pattern":"^eios:object:[A-Za-z][A-Za-z0-9_]*/[a-f0-9]{64}$"},"epistemic_kind":{"enum":["hypothesis","user_statement"]},"resolution_state":{"enum":["resolved","awaiting_definition","unresolved"]},"conclusion":{"type":"string","minLength":1,"maxLength":8192},"valid_from":{"type":"string","format":"date-time"},"valid_to":{"type":["string","null"],"format":"date-time"},"source_message_ref":{"type":["string","null"],"pattern":"^eios:object:Message/[a-f0-9]{64}$"},"source_content_hash":{"type":["string","null"],"pattern":"^[a-f0-9]{64}$"}},"required":["assessment_ref","conclusion","epistemic_kind","relation_type_ref","resolution_state","revision","source_content_hash","source_message_ref","source_ref","target_ref","valid_from","valid_to"],"additionalProperties":false},"maxItems":4},"evidence":{"type":"array","items":{"type":"object","properties":{"assessment_ref":{"type":"string","pattern":"^eios:object:RelationshipAssessment/[a-f0-9]{64}$"},"revision":{"type":"integer","minimum":1,"maximum":9007199254740991},"relation_type_ref":{"type":["string","null"],"pattern":"^eios:link_type:[A-Za-z][A-Za-z0-9_]*:[1-9][0-9]*$"},"source_ref":{"type":"string","pattern":"^eios:object:[A-Za-z][A-Za-z0-9_]*/[a-f0-9]{64}$"},"target_ref":{"type":"string","pattern":"^eios:object:[A-Za-z][A-Za-z0-9_]*/[a-f0-9]{64}$"},"epistemic_kind":{"enum":["hypothesis","user_statement"]},"resolution_state":{"enum":["resolved","awaiting_definition","unresolved"]},"conclusion":{"type":"string","minLength":1,"maxLength":8192},"valid_from":{"type":"string","format":"date-time"},"valid_to":{"type":["string","null"],"format":"date-time"},"source_message_ref":{"type":["string","null"],"pattern":"^eios:object:Message/[a-f0-9]{64}$"},"source_content_hash":{"type":["string","null"],"pattern":"^[a-f0-9]{64}$"}},"required":["assessment_ref","conclusion","epistemic_kind","relation_type_ref","resolution_state","revision","source_content_hash","source_message_ref","source_ref","target_ref","valid_from","valid_to"],"additionalProperties":false},"maxItems":4}},"required":["current_statements","evidence"],"additionalProperties":false},"constraints":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"subsection":{"type":"string","minLength":1,"maxLength":64},"ref":{"type":"string","minLength":1,"maxLength":512},"revision":{"type":"string","minLength":1,"maxLength":128},"content":{},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"evidence_kind":{"enum":["formal_object","policy"]},"access_decision_ref":{"type":"string","pattern":"^decision:[0-9a-f]{64}$"},"relevance_permille":{"type":"integer","minimum":0,"maximum":1000},"at":{"type":"string","maxLength":64},"tags":{"type":"array","items":{"enum":["negation","contact_limit","unconfirmed"]},"uniqueItems":true,"maxItems":3}},"required":["access_decision_ref","at","content","content_hash","evidence_kind","ref","relevance_permille","revision","subsection","tags"]},"maxItems":64},"consumer_state":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"subsection":{"type":"string","minLength":1,"maxLength":64},"ref":{"type":"string","minLength":1,"maxLength":512},"revision":{"type":"string","minLength":1,"maxLength":128},"content":{},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"evidence_kind":{"enum":["formal_object","policy"]},"access_decision_ref":{"type":"string","pattern":"^decision:[0-9a-f]{64}$"},"relevance_permille":{"type":"integer","minimum":0,"maximum":1000},"at":{"type":"string","maxLength":64},"tags":{"type":"array","items":{"enum":["negation","contact_limit","unconfirmed"]},"uniqueItems":true,"maxItems":3}},"required":["access_decision_ref","at","content","content_hash","evidence_kind","ref","relevance_permille","revision","subsection","tags"]},"maxItems":256},"open_work":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"subsection":{"type":"string","minLength":1,"maxLength":64},"ref":{"type":"string","minLength":1,"maxLength":512},"revision":{"type":"string","minLength":1,"maxLength":128},"content":{},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"evidence_kind":{"enum":["formal_object","execution_state","policy"]},"access_decision_ref":{"type":"string","pattern":"^decision:[0-9a-f]{64}$"},"relevance_permille":{"type":"integer","minimum":0,"maximum":1000},"at":{"type":"string","maxLength":64},"tags":{"type":"array","items":{"enum":["negation","contact_limit","unconfirmed"]},"uniqueItems":true,"maxItems":3}},"required":["access_decision_ref","at","content","content_hash","evidence_kind","ref","relevance_permille","revision","subsection","tags"]},"maxItems":128},"evidence":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"subsection":{"enum":["conversation","claim_evidence","relationships","recall","hypotheses"]},"ref":{"type":"string","minLength":1,"maxLength":512},"revision":{"type":"string","minLength":1,"maxLength":128},"content":{},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"evidence_kind":{"enum":["user_statement","conversation","hypothesis","memory"]},"access_decision_ref":{"type":"string","pattern":"^decision:[0-9a-f]{64}$"},"relevance_permille":{"type":"integer","minimum":0,"maximum":1000},"at":{"type":"string","maxLength":64},"tags":{"type":"array","items":{"enum":["negation","contact_limit","unconfirmed"]},"uniqueItems":true,"maxItems":3}},"required":["access_decision_ref","at","content","content_hash","evidence_kind","ref","relevance_permille","revision","subsection","tags"],"allOf":[{"if":{"properties":{"evidence_kind":{"const":"hypothesis"}}},"then":{"properties":{"subsection":{"const":"hypotheses"}}}},{"if":{"properties":{"subsection":{"const":"hypotheses"}}},"then":{"properties":{"evidence_kind":{"const":"hypothesis"}}}}]},"maxItems":256},"semantics":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"subsection":{"type":"string","minLength":1,"maxLength":64},"ref":{"type":"string","minLength":1,"maxLength":512},"revision":{"type":"string","minLength":1,"maxLength":128},"content":{},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"evidence_kind":{"enum":["schema"]},"access_decision_ref":{"type":"string","pattern":"^decision:[0-9a-f]{64}$"},"relevance_permille":{"type":"integer","minimum":0,"maximum":1000},"at":{"type":"string","maxLength":64},"tags":{"type":"array","items":{"enum":["negation","contact_limit","unconfirmed"]},"uniqueItems":true,"maxItems":3}},"required":["access_decision_ref","at","content","content_hash","evidence_kind","ref","relevance_permille","revision","subsection","tags"]},"maxItems":64},"experience":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"subsection":{"type":"string","minLength":1,"maxLength":64},"ref":{"type":"string","minLength":1,"maxLength":512},"revision":{"type":"string","minLength":1,"maxLength":128},"content":{},"content_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"evidence_kind":{"enum":["memory"]},"access_decision_ref":{"type":"string","pattern":"^decision:[0-9a-f]{64}$"},"relevance_permille":{"type":"integer","minimum":0,"maximum":1000},"at":{"type":"string","maxLength":64},"tags":{"type":"array","items":{"enum":["negation","contact_limit","unconfirmed"]},"uniqueItems":true,"maxItems":3}},"required":["access_decision_ref","at","content","content_hash","evidence_kind","ref","relevance_permille","revision","subsection","tags"]},"maxItems":64},"budget_report":{"type":"object","additionalProperties":false,"properties":{"estimator":{"type":"string","minLength":1,"maxLength":64},"input_token_budget":{"type":"integer","minimum":1024},"output_reserve":{"type":"integer","minimum":128},"framing_reserve":{"type":"integer","minimum":0},"available":{"type":"integer"},"used":{"type":"integer","minimum":0},"sections":{"type":"object"},"omitted":{"type":"array","maxItems":1024,"items":{"type":"object","additionalProperties":false,"properties":{"section":{"type":"string","maxLength":32},"subsection":{"type":"string","maxLength":64},"ref":{"type":"string","minLength":1,"maxLength":512},"reason":{"enum":["section_quota","mandatory_exceeds_budget","core_exceeds_budget","duplicate_evidence","experience","hypotheses","semantics_low_relevance","older_conversation","older_claim_evidence","low_score_recall"]}},"required":["reason","ref","section","subsection"]}}},"required":["available","estimator","framing_reserve","input_token_budget","omitted","output_reserve","sections","used"]},"insufficient":{"type":"array","maxItems":16,"items":{"type":"object","additionalProperties":false,"properties":{"code":{"enum":["mandatory_exceeds_budget","core_trimmed","required_source_unreadable","required_source_stale","goal_not_current","control_paused","semantic_ambiguous_required"]},"section":{"anyOf":[{"type":"null"},{"type":"string","maxLength":32}]},"refs":{"type":"array","maxItems":512,"items":{"type":"string","minLength":1,"maxLength":512}}},"required":["code","refs","section"]}}},"required":["bindings","budget_report","constraints","consumer_state","current_constraints","current_event","evidence","experience","formal_facts","goal","insufficient","open_work","role","schema_version","semantics","strategy_ref","supply","user_statement"]}')
    schema_version: Literal['nexloop.context-pack.v6']
    strategy_ref: str
    bindings: ContextPackV6Bindings
    role: None | ContextPackV6Role1
    current_event: ContextPackV6CurrentEvent0 | ContextPackV6CurrentEvent1
    user_statement: ContextPackV6UserStatement0 | None
    goal: ContextPackV6Goal
    formal_facts: list[ContextPackV6FormalFactsItem]
    current_constraints: ContextPackV6CurrentConstraints
    supply: ContextPackV6Supply
    relationship_context: ContextPackV6RelationshipContext | None = None
    constraints: list[ContextPackV6ConstraintsItem]
    consumer_state: list[ContextPackV6ConsumerStateItem]
    open_work: list[ContextPackV6OpenWorkItem]
    evidence: list[ContextPackV6EvidenceItem]
    semantics: list[ContextPackV6SemanticsItem]
    experience: list[ContextPackV6ExperienceItem]
    budget_report: ContextPackV6BudgetReport
    insufficient: list[ContextPackV6InsufficientItem]

class ConversationMessage(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:conversation-message","title":"conversation-message","description":"NX-051 (AT-014). The read projection of one conversation Message, as the conversation read API returns it. `sequence` and `accepted_at` are this system\'s receipt order and time (assigned at commit, never rewritten). `provider` is the channel\'s own order, time and reference as evidence (trust: server = produced here, signed = a verified signed channel, client = what the browser client stated; client values never order anything and are never a time anchor). `reply_to_message_id` is the resolved reply target in the same conversation: for an Agent reply it mirrors the server-derived trigger_message_id; for an inbound message it is the reference the channel carried, once resolved (null while pending, for another conversation, or none). Server-derived; grants nothing. NX-028 (ruling B, approved contract change): an outbound message is an Agent reply (sender_kind=agent, with its governed intent_id) or a staff reply written during the author\'s own takeover of a native WebChat conversation (sender_kind=human_takeover, no intent: it does not go through the effect ledger); both carry the server-checked trigger_message_id.","type":"object","additionalProperties":false,"properties":{"id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"conversation_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"sequence":{"type":"integer","minimum":1},"actor":{"type":"string","minLength":1,"maxLength":512},"body":{"type":"string","minLength":1,"maxLength":8192},"accepted_at":{"type":"string","minLength":1,"maxLength":64,"description":"This system\'s acceptance time as stored with the Message (PostgreSQL timestamptz text)."},"status":{"const":"accepted"},"direction":{"const":"outbound"},"sender_kind":{"enum":["agent","human_takeover"]},"intent_id":{"type":"string","format":"uuid"},"trigger_message_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"reply_to_message_id":{"anyOf":[{"type":"null"},{"type":"string","pattern":"^[a-f0-9]{64}$"}]},"provider":{"anyOf":[{"type":"null"},{"type":"object","additionalProperties":false,"properties":{"namespace":{"type":"string","pattern":"^[a-z][a-z0-9.-]{1,63}$"},"message_ref":{"anyOf":[{"type":"null"},{"type":"string","minLength":1,"maxLength":256}]},"sequence":{"anyOf":[{"type":"null"},{"type":"integer","minimum":1,"maximum":9007199254740991}]},"sent_at":{"anyOf":[{"type":"null"},{"type":"string","format":"date-time"}]},"trust":{"enum":["server","signed","client"]},"skewed":{"type":"boolean"}},"required":["namespace","message_ref","sequence","sent_at","trust","skewed"]}]}},"required":["id","conversation_id","sequence","actor","body","accepted_at","status"],"allOf":[{"if":{"required":["direction"]},"then":{"required":["sender_kind","trigger_message_id"]}},{"if":{"required":["sender_kind"],"properties":{"sender_kind":{"const":"agent"}}},"then":{"required":["intent_id"]}},{"if":{"required":["sender_kind"],"properties":{"sender_kind":{"const":"human_takeover"}}},"then":{"not":{"required":["intent_id"]}}}]}')
    id: str
    conversation_id: str
    sequence: int
    actor: str
    body: str
    accepted_at: str
    status: Literal['accepted']
    direction: Literal['outbound'] | None = None
    sender_kind: Literal['agent', 'human_takeover'] | None = None
    intent_id: str | None = None
    trigger_message_id: str | None = None
    reply_to_message_id: None | str | None = None
    provider: None | ConversationMessageProvider1 | None = None

class EventEnvelope(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:event-envelope","title":"event-envelope","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"event_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"event_type":{"type":"string","pattern":"^[a-z][a-z0-9_.]+$"},"source":{"type":"string","minLength":1},"source_event_id":{"type":"string","minLength":1},"occurred_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"recorded_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"subject_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"correlation_id":{"type":"string","format":"uuid"},"causation_id":{"anyOf":[{"type":"string","format":"uuid"},{"type":"null"}]},"payload":{"type":"object"}},"required":["schema_version","event_id","tenant_id","world_id","mode","event_type","source","source_event_id","occurred_at","recorded_at","subject_ref","correlation_id","causation_id","payload"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}}]}')
    schema_version: Literal['1.0']
    event_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    event_type: str
    source: str
    source_event_id: str
    occurred_at: str
    recorded_at: str
    subject_ref: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]

class EvolutionCandidate(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:evolution-candidate","title":"evolution-candidate","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"candidate_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"parent_revision":{"type":"string","minLength":1},"candidate_revision":{"type":"string","minLength":1},"dimension":{"enum":["semantic_content","semantic_tool","ontology_schema"]},"primary_hypothesis":{"type":"string","minLength":10,"maxLength":4000},"patch_artifact_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"input_snapshot_digest":{"type":"string","pattern":"^[a-f0-9]{64}$"},"design_case_ids":{"type":"array","items":{"type":"string","minLength":1},"minItems":1,"maxItems":256},"validation_case_ids":{"type":"array","items":{"type":"string","minLength":1},"minItems":1,"maxItems":256},"evaluator_version":{"type":"string","minLength":1},"model_profile":{"type":"string","minLength":1},"maximum_rounds":{"type":"integer","minimum":1,"maximum":32},"allowed_change_scope":{"type":"array","items":{"type":"string","minLength":1},"minItems":1,"maxItems":256},"status":{"enum":["proposed","evaluating","rejected","accepted_for_release","incomplete"]},"publication_permit_ref":{"type":"null"},"created_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}},"required":["schema_version","candidate_id","tenant_id","parent_revision","candidate_revision","dimension","primary_hypothesis","patch_artifact_ref","input_snapshot_digest","design_case_ids","validation_case_ids","evaluator_version","model_profile","maximum_rounds","allowed_change_scope","status","publication_permit_ref","created_at"]}')
    schema_version: Literal['1.0']
    candidate_id: str
    tenant_id: str
    parent_revision: str
    candidate_revision: str
    dimension: Literal['semantic_content', 'semantic_tool', 'ontology_schema']
    primary_hypothesis: str
    patch_artifact_ref: str
    input_snapshot_digest: str
    design_case_ids: list[str]
    validation_case_ids: list[str]
    evaluator_version: str
    model_profile: str
    maximum_rounds: int
    allowed_change_scope: list[str]
    status: Literal['proposed', 'evaluating', 'rejected', 'accepted_for_release', 'incomplete']
    publication_permit_ref: None
    created_at: str

class OntologyMutation(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:ontology-mutation","title":"ontology-mutation","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"proposal_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"extraction_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"source_content_hash":{"type":"string","pattern":"^[a-f0-9]{64}$"},"extractor_version":{"type":"string","minLength":1},"ontology_schema_revision":{"type":"string","minLength":1},"business_intent_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"risk_class":{"enum":["low","medium","high"]},"rationale_summary":{"type":"string","minLength":1,"maxLength":2000},"epistemic_kind":{"enum":["user_statement","verified_fact","preference","constraint","intent","need_problem","commitment","hypothesis","correction"]},"operations":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"op":{"enum":["create_object","set_property","invalidate_property","link_relation","end_relation","supersede_claim"]},"target_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"type_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"expected_revision":{"type":["integer","null"],"minimum":1},"property_name":{"type":"string","minLength":1},"value":{},"relation_type_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"to_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"valid_from":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"valid_to":{"anyOf":[{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},{"type":"null"}]},"evidence_refs":{"type":"array","items":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"minItems":1,"maxItems":256}},"required":["op","target_ref","expected_revision","evidence_refs"],"allOf":[{"if":{"properties":{"op":{"const":"create_object"}}},"then":{"required":["type_ref","value"],"properties":{"expected_revision":{"type":"null"}}}},{"if":{"properties":{"op":{"const":"set_property"}}},"then":{"required":["property_name","value"],"properties":{"expected_revision":{"type":"integer","minimum":1}}}},{"if":{"properties":{"op":{"const":"invalidate_property"}}},"then":{"required":["property_name"],"properties":{"expected_revision":{"type":"integer","minimum":1}}}},{"if":{"properties":{"op":{"const":"link_relation"}}},"then":{"required":["relation_type_ref","to_ref"],"properties":{"expected_revision":{"type":"integer","minimum":1}}}},{"if":{"properties":{"op":{"enum":["end_relation","supersede_claim"]}}},"then":{"properties":{"expected_revision":{"type":"integer","minimum":1}}}}]},"minItems":1,"maxItems":256},"conflict_policy":{"const":"reject_and_reassess"},"created_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}},"required":["schema_version","proposal_id","tenant_id","world_id","mode","extraction_ref","source_content_hash","extractor_version","ontology_schema_revision","business_intent_ref","risk_class","rationale_summary","epistemic_kind","operations","conflict_policy","created_at"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}}]}')
    schema_version: Literal['1.0']
    proposal_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    extraction_ref: str
    source_content_hash: str
    extractor_version: str
    ontology_schema_revision: str
    business_intent_ref: str
    risk_class: Literal['low', 'medium', 'high']
    rationale_summary: str
    epistemic_kind: Literal['user_statement', 'verified_fact', 'preference', 'constraint', 'intent', 'need_problem', 'commitment', 'hypothesis', 'correction']
    operations: list[OntologyMutationOperationsItem]
    conflict_policy: Literal['reject_and_reassess']
    created_at: str

class ReviewDecision(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:review-decision","title":"review-decision","description":"ADR-019. A human reviewer\'s decision on a candidate-definition. Reviewer identity is server-derived from an authenticated human session; the decision itself is a governed human Action and is audited. Not an existing upstream API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"decision_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"candidate_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"reviewer_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"decision":{"enum":["approve","merge_into","reject"]},"merge_target_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"rationale":{"type":"string","minLength":1,"maxLength":2000},"expected_candidate_status":{"const":"pending_review"},"decided_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"publication":{"type":"object","additionalProperties":false,"properties":{"schema_revision_before":{"type":"string","minLength":1},"schema_revision_after":{"type":"string","minLength":1},"published_refs":{"type":"array","items":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"maxItems":64},"applied_claim_count":{"type":"integer","minimum":0},"gate_failures":{"type":"array","items":{"type":"string","minLength":1},"maxItems":64}},"required":["schema_revision_before"]}},"required":["schema_version","decision_id","tenant_id","world_id","mode","candidate_ref","reviewer_ref","decision","rationale","expected_candidate_status","decided_at"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}},{"if":{"properties":{"decision":{"const":"merge_into"}}},"then":{"required":["merge_target_ref"]}},{"if":{"properties":{"decision":{"const":"reject"}}},"then":{"properties":{"publication":false}}}]}')
    schema_version: Literal['1.0']
    decision_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    candidate_ref: str
    reviewer_ref: str
    decision: Literal['approve', 'merge_into', 'reject']
    merge_target_ref: str | None = None
    rationale: str
    expected_candidate_status: Literal['pending_review']
    decided_at: str
    publication: ReviewDecisionPublication | None = None

class RunCommand(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:run-command","title":"run-command","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"run_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"request_id":{"type":"string","minLength":16,"maxLength":200},"trigger_event_id":{"type":"string","format":"uuid"},"role_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"consumer_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"goal_version_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"context_manifest_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"runtime_profile":{"type":"string","minLength":1},"credential_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"budget":{"type":"object","additionalProperties":false,"properties":{"maximum_model_turns":{"type":"integer","minimum":1,"maximum":64},"maximum_tool_calls":{"type":"integer","minimum":1,"maximum":128},"active_timeout_seconds":{"type":"integer","minimum":1,"maximum":3600},"maximum_cost":{"type":"string","pattern":"^\\\\d+(\\\\.\\\\d{1,8})?$"},"currency":{"type":"string","pattern":"^[A-Z]{3}$"}},"required":["maximum_model_turns","maximum_tool_calls","active_timeout_seconds","maximum_cost","currency"]},"not_after":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"},"runtime_owner_epoch":{"type":"integer","minimum":1}},"required":["schema_version","run_id","tenant_id","world_id","mode","request_id","trigger_event_id","role_ref","consumer_ref","goal_version_ref","context_manifest_ref","runtime_profile","credential_ref","budget","not_after","runtime_owner_epoch"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}}]}')
    schema_version: Literal['1.0']
    run_id: str
    tenant_id: str
    world_id: str
    mode: Literal['real', 'simulation', 'shadow', 'test']
    request_id: str
    trigger_event_id: str
    role_ref: str
    consumer_ref: str
    goal_version_ref: str
    context_manifest_ref: str
    runtime_profile: str
    credential_ref: str
    budget: RunCommandBudget
    not_after: str
    runtime_owner_epoch: int

class RunOutcome(_Contract):
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:run-outcome","title":"run-outcome","description":"NX-024. The result a plan reevaluation Run records through its Host for the current plan version. no_action, needs_information, waiting_external and escalate are normal results, not failures; plan_update supersedes the plan with new steps; action_intent names an intent this very Run submitted. Identity (tenant, world, Run, plan) is derived by the server from the Run\'s live activation; this body carries none. Grants nothing.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"kind":{"enum":["no_action","needs_information","waiting_external","escalate","plan_update","action_intent"]},"reasons":{"type":"array","maxItems":8,"items":{"type":"string","minLength":1,"maxLength":500}},"reassess_at":{"anyOf":[{"type":"null"},{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|\\\\+00:00)$"}]},"evidence_refs":{"type":"array","maxItems":32,"items":{"type":"string","minLength":3,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"}},"intent_ref":{"anyOf":[{"type":"null"},{"type":"string","format":"uuid","pattern":"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}]},"plan_update":{"anyOf":[{"type":"null"},{"type":"object","additionalProperties":false,"properties":{"strategy":{"anyOf":[{"type":"null"},{"type":"string","minLength":1,"maxLength":8192}]},"steps":{"type":"array","minItems":1,"maxItems":32,"items":{"type":"object","additionalProperties":false,"properties":{"step_key":{"type":"string","pattern":"^[a-z0-9][a-z0-9._-]{0,63}$"},"step_object_id":{"anyOf":[{"type":"null"},{"type":"string","pattern":"^[a-f0-9]{64}$"}]},"prerequisites":{"type":"array","maxItems":32,"items":{"type":"string","minLength":1,"maxLength":500}},"expected_result":{"type":"string","minLength":1,"maxLength":2000},"stop_if":{"type":"array","maxItems":16,"items":{"type":"object","additionalProperties":false,"properties":{"type_name":{"type":"string","pattern":"^[A-Za-z][A-Za-z0-9_]{0,63}$"},"object_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"property":{"type":"string","pattern":"^[A-Za-z][A-Za-z0-9_]{0,63}$"},"equals":{}},"required":["type_name","object_id","property","equals"]}},"reassess_at":{"anyOf":[{"type":"null"},{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|\\\\+00:00)$"}]},"budget":{"type":"object","additionalProperties":false,"properties":{"maximum_model_turns":{"type":"integer","minimum":1,"maximum":64},"maximum_tool_calls":{"type":"integer","minimum":1,"maximum":128},"active_timeout_seconds":{"type":"integer","minimum":1,"maximum":3600}},"required":["maximum_model_turns","maximum_tool_calls","active_timeout_seconds"]},"intent_ref":{"anyOf":[{"type":"null"},{"type":"string","pattern":"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}]}},"required":["step_key","step_object_id","prerequisites","expected_result","stop_if","reassess_at","budget","intent_ref"]}}},"required":["steps","strategy"]}]}},"required":["schema_version","kind","reasons","reassess_at","evidence_refs","intent_ref","plan_update"],"allOf":[{"if":{"properties":{"kind":{"const":"plan_update"}}},"then":{"properties":{"plan_update":{"type":"object"}}},"else":{"properties":{"plan_update":{"type":"null"}}}},{"if":{"properties":{"kind":{"const":"action_intent"}}},"then":{"properties":{"intent_ref":{"type":"string"}}}},{"if":{"properties":{"kind":{"const":"waiting_external"}}},"then":{"anyOf":[{"properties":{"reassess_at":{"type":"string"}}},{"properties":{"intent_ref":{"type":"string"}}}]}}]}')
    schema_version: Literal['1.0']
    kind: Literal['no_action', 'needs_information', 'waiting_external', 'escalate', 'plan_update', 'action_intent']
    reasons: list[str]
    reassess_at: None | str
    evidence_refs: list[str]
    intent_ref: None | str
    plan_update: None | RunOutcomePlanUpdate1
