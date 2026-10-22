"""Stage-10 deliverable 1: static Definition dependency graph + impact."""

from __future__ import annotations

from datetime import UTC, datetime

from eios.ontology.definitions import OntologySchemaReference, OntologySchemaType
from eios.ontology.semantics import schema_contract_digest


def object_type_reference(tenant_id, definition):
    """eios.composition.schema_contracts.object_type_reference (not vendored), same body."""
    return OntologySchemaReference(tenant_id=tenant_id, schema_type=OntologySchemaType.OBJECT_TYPE,
                                   stable_name=definition.type_name, version=definition.version,
                                   schema_digest=schema_contract_digest(definition))
from eios.ontology.definitions import (
    ActionChangeScope,
    ActionDefinition,
    ActionGovernanceContract,
    ActionIdempotencyPolicy,
    ActionRiskLevel,
    CapabilityBinding,
    DefinitionStatus,
    FunctionDefinition,
    FunctionTarget,
    ObjectViewDefinition,
    PropertyReference,
    ViewSource,
)
from eios.ontology.impact import (
    build_dependency_graph,
    definition_node,
    property_node,
)
from eios.ontology.models import (
    ObjectTypeDefinition,
    PropertyDefinition,
    PropertyValueType,
)
from eios.ontology.registry import InMemoryOntologyRegistry


TENANT = "tenant-impact"
CREATED = datetime(2026, 7, 18, 12, tzinfo=UTC)


def _binding(name: str) -> CapabilityBinding:
    return CapabilityBinding(
        capability_name=name, capability_version="1.0.0", schema_hash="c" * 64
    )


def _world():
    registry = InMemoryOntologyRegistry()
    customer = registry.register_object_type(
        TENANT,
        ObjectTypeDefinition(
            type_name="Customer",
            properties=(
                PropertyDefinition(
                    property_name="name",
                    value_type=PropertyValueType.STRING,
                    required=True,
                ),
                PropertyDefinition(
                    property_name="revenue", value_type=PropertyValueType.NUMBER
                ),
            ),
        ),
        idempotency_key="schema-customer",
    )
    customer_ref = object_type_reference(TENANT, customer)
    revenue = PropertyReference(
        object_type=customer_ref, property_name="revenue"
    )

    function = FunctionDefinition(
        tenant_id=TENANT,
        stable_name="customer.score",
        version=1,
        status=DefinitionStatus.PUBLISHED,
        required_scopes=("ontology.object.read",),
        created_by="owner",
        created_at=CREATED,
        capability_binding=_binding("probe.read"),
        applies_to=(FunctionTarget(object_type=customer_ref),),
        property_dependencies=(revenue,),
        input_schema={"type": "object"},
        output_schema={"type": "number"},
    )
    action = ActionDefinition(
        tenant_id=TENANT,
        stable_name="customer.reprice",
        version=1,
        status=DefinitionStatus.PUBLISHED,
        required_scopes=(
            "action.customer.update",
            "enterprise.crm.write",
            "ontology.object.read",
        ),
        created_by="owner",
        created_at=CREATED,
        capability_binding=_binding("probe.write"),
        object_types=(customer_ref,),
        governance=ActionGovernanceContract(
            change_scope=ActionChangeScope(
                object_types=(customer_ref,),
                properties=(revenue,),
                target_systems=("crm",),
            ),
            risk_level=ActionRiskLevel.LOW,
            policy_refs=(),
            idempotency=ActionIdempotencyPolicy(key_fields=("request_id",)),
        ),
        receipt_schema={"type": "object"},
    )
    view = ObjectViewDefinition(
        tenant_id=TENANT,
        stable_name="customer.card",
        version=1,
        status=DefinitionStatus.PUBLISHED,
        required_scopes=(
            "action.customer.update",
            "enterprise.crm.write",
            "ontology.object.read",
        ),
        created_by="owner",
        created_at=CREATED,
        source=ViewSource(object_type=customer_ref),
        fields=(revenue,),
        functions=(function.reference(),),
        actions=(action.reference(),),
        display_hints={"title_property": "revenue"},
    )
    return customer_ref, revenue, function, action, view


def test_changing_a_property_reports_the_downstream_definitions() -> None:
    customer_ref, revenue, function, action, view = _world()
    graph = build_dependency_graph((function, action, view))

    downstream = {
        node for node, _hop in graph.impact_of(property_node(revenue), "downstream")
    }
    # Every Definition that reads or writes the revenue property is impacted.
    assert definition_node(function.reference()) in downstream
    assert definition_node(action.reference()) in downstream
    assert definition_node(view.reference()) in downstream


def test_downstream_is_transitive_with_hop_counts() -> None:
    customer_ref, revenue, function, action, view = _world()
    graph = build_dependency_graph((function, action, view))
    # The View references the Function; changing the Function impacts the View
    # at one hop, and the property that the Function depends on impacts the
    # View at two hops (property -> function -> view).
    hops = dict(graph.impact_of(property_node(revenue), "downstream"))
    assert hops[definition_node(function.reference())] == 1
    assert hops[definition_node(view.reference())] == 1  # view -> revenue direct
    function_downstream = dict(
        graph.impact_of(definition_node(function.reference()), "downstream")
    )
    assert function_downstream[definition_node(view.reference())] == 1


def test_upstream_reports_what_a_definition_depends_on() -> None:
    customer_ref, revenue, function, action, view = _world()
    graph = build_dependency_graph((function, action, view))
    upstream = {
        node for node, _hop in graph.impact_of(
            definition_node(view.reference()), "upstream"
        )
    }
    assert property_node(revenue) in upstream
    assert definition_node(function.reference()) in upstream
    assert definition_node(action.reference()) in upstream


def test_max_hops_bounds_the_traversal() -> None:
    customer_ref, revenue, function, action, view = _world()
    graph = build_dependency_graph((function, action, view))
    bounded = graph.impact_of(
        property_node(revenue), "downstream", max_hops=1
    )
    assert all(hop <= 1 for _node, hop in bounded)
