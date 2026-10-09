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
    evidence_kind: Literal['verified_fact', 'user_statement', 'hypothesis', 'policy', 'schema', 'memory', 'current_message']
    access_decision_ref: str

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
    _canonical_schema = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:nexloop:contracts:v1:context-manifest","title":"context-manifest","description":"NexLoop target contract; server-derived identity required. This is not an existing upstream EIOS/Pi API.","type":"object","additionalProperties":false,"properties":{"schema_version":{"const":"1.0"},"context_id":{"type":"string","format":"uuid"},"tenant_id":{"type":"string","format":"uuid"},"world_id":{"type":"string","minLength":1},"mode":{"type":"string","enum":["real","simulation","shadow","test"]},"run_id":{"type":"string","format":"uuid"},"call_sequence":{"type":"integer","minimum":1},"goal_version_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"policy_revision":{"type":"string","minLength":1},"ontology_schema_revision":{"type":"string","minLength":1},"semantic_snapshot_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"context_strategy_version":{"type":"string","minLength":1},"model_provider":{"type":"string","minLength":1},"model_id":{"type":"string","minLength":1},"embedding_profile_ref":{"anyOf":[{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},{"type":"null"}]},"sources":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"revision":{"type":"string","minLength":1},"content_hash":{"type":"string","pattern":"^[a-f0-9]{64}$"},"evidence_kind":{"enum":["verified_fact","user_statement","hypothesis","policy","schema","memory","current_message"]},"access_decision_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"}},"required":["ref","revision","content_hash","evidence_kind","access_decision_ref"]},"minItems":1,"maxItems":256},"prompt_artifact_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"request_digest":{"type":"string","pattern":"^[a-f0-9]{64}$"},"input_token_budget":{"type":"integer","minimum":256},"output_token_budget":{"type":"integer","minimum":128},"redaction_policy_ref":{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z][A-Za-z0-9_.-]*:[^\\\\s]+$"},"created_at":{"type":"string","format":"date-time","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}T\\\\d{2}:\\\\d{2}:\\\\d{2}(\\\\.\\\\d{1,9})?(Z|[+-]\\\\d{2}:\\\\d{2})$"}},"required":["schema_version","context_id","tenant_id","world_id","mode","run_id","call_sequence","goal_version_ref","policy_revision","ontology_schema_revision","semantic_snapshot_ref","context_strategy_version","model_provider","model_id","embedding_profile_ref","sources","prompt_artifact_ref","request_digest","input_token_budget","output_token_budget","redaction_policy_ref","created_at"],"allOf":[{"if":{"properties":{"mode":{"const":"real"}},"required":["mode"]},"then":{"properties":{"world_id":{"const":"real"}}},"else":{"properties":{"world_id":{"not":{"const":"real"}}}}}]}')
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
