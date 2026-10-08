from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import Field, ValidationError, field_validator, model_validator

from eios.ontology.definition_registry import (
    DefinitionRegistryError,
    DefinitionRegistryPort,
)
from eios.ontology.definitions import (
    ActionDefinition,
    ActionRiskLevel,
    CapabilityBinding,
    Definition,
    DefinitionReference,
    DefinitionStatus,
    DefinitionType,
    FrozenContract,
    FunctionDefinition,
    InterfaceDefinition,
    LinkDirection,
    ObjectSetDefinition,
    ObjectViewDefinition,
    OntologySchemaReference,
    FrozenJsonMap,
    OntologySchemaType,
    PropertyReference,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RISK_ORDER = {
    ActionRiskLevel.LOW: 0,
    ActionRiskLevel.MEDIUM: 1,
    ActionRiskLevel.HIGH: 2,
    ActionRiskLevel.CRITICAL: 3,
}
_CONCRETE_DEFINITION_CLASSES = (
    FunctionDefinition,
    ActionDefinition,
    ObjectSetDefinition,
    ObjectViewDefinition,
    InterfaceDefinition,
)


class ResolutionContractError(ValueError):
    """Base class for stable fail-closed resolution errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DefinitionResolutionError(ResolutionContractError):
    """An exact Definition cannot be resolved for use."""


class CapabilityBindingError(ResolutionContractError):
    """A Definition and frozen Capability snapshot do not match."""


class DefinitionGraphError(ResolutionContractError):
    """A Definition graph edge is missing, weakened, or inconsistent."""


class OntologySchemaContractNotFound(ResolutionContractError):
    """An exact read-only Ontology schema snapshot is unavailable."""


class CapabilityContractKind(str, Enum):
    ATOMIC = "atomic"
    WORKFLOW = "workflow"


class OntologySchemaLifecycle(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    INACTIVE = "inactive"


def _non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must be non-empty")
    return clean


def _normalized_strings(
    values: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(_non_blank(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"duplicate {field_name}")
    return tuple(sorted(normalized))


def _reference_sort_key(
    reference: OntologySchemaReference,
) -> tuple[str, str, str, int, str]:
    return (
        reference.tenant_id,
        reference.schema_type.value,
        reference.stable_name,
        reference.version,
        reference.schema_digest,
    )


def _normalized_references(
    values: tuple[OntologySchemaReference, ...],
    field_name: str,
) -> tuple[OntologySchemaReference, ...]:
    keys = tuple(_reference_sort_key(value) for value in values)
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {field_name}")
    return tuple(sorted(values, key=_reference_sort_key))


class CapabilityContractSnapshot(FrozenContract):
    """Local immutable metadata copy used for exact binding checks."""

    capability_name: str
    capability_version: str
    schema_hash: str = Field(pattern=_SHA256_PATTERN)
    kind: CapabilityContractKind
    has_side_effects: bool
    idempotent: bool
    required_scopes: tuple[str, ...] = ()
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW

    @field_validator("capability_name", "capability_version")
    @classmethod
    def _validate_non_blank(cls, value: str, info) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("required_scopes")
    @classmethod
    def _validate_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_strings(values, "required_scopes")

    def binding(self) -> CapabilityBinding:
        return CapabilityBinding(
            capability_name=self.capability_name,
            capability_version=self.capability_version,
            schema_hash=self.schema_hash,
        )


class PropertyContractMetadata(FrozenContract):
    """Display metadata for one property of an exact Object Type snapshot."""

    property_name: str
    value_type: str = ""
    required: bool = False
    display_name: str = ""
    render_hint: str = "plain"
    visibility: str = "normal"

    @field_validator("property_name")
    @classmethod
    def _validate_property_name(cls, value: str) -> str:
        return _non_blank(value, "property_name")


class PropertyGroupContract(FrozenContract):
    """One display grouping of properties in an Object Type snapshot."""

    group_name: str
    display_name: str = ""
    property_names: tuple[str, ...] = ()

    @field_validator("group_name")
    @classmethod
    def _validate_group_name(cls, value: str) -> str:
        return _non_blank(value, "group_name")

    @field_validator("property_names")
    @classmethod
    def _validate_property_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_strings(values, "property_names")


class ObjectTypeDisplayContract(FrozenContract):
    """Object-level display metadata of an exact Object Type snapshot."""

    display_name: str = ""
    plural_display_name: str = ""
    title_property: str = ""
    icon: str = ""
    color: str = ""
    property_groups: tuple[PropertyGroupContract, ...] = ()


class OntologySchemaContractSnapshot(FrozenContract):
    """Read-only exact schema metadata supplied by the Integration boundary."""

    reference: OntologySchemaReference
    status: OntologySchemaLifecycle
    required_scopes: tuple[str, ...] = ()
    # Stage 9: marking floor of the object/relation type — a Definition
    # referencing it must declare at least these markings.
    required_markings: tuple[str, ...] = ()
    property_names: tuple[str, ...] = ()
    source_object_types: tuple[OntologySchemaReference, ...] = ()
    target_object_types: tuple[OntologySchemaReference, ...] = ()
    # Additive display metadata; optional so pre-existing resolvers that only
    # supply property_names remain valid.
    property_metadata: tuple[PropertyContractMetadata, ...] = ()
    display: ObjectTypeDisplayContract | None = None
    # Stage 9: property_name -> required markings. A caller lacking any of a
    # property's markings sees it masked in the read projection.
    property_markings: FrozenJsonMap | None = None
    # Stage 11: derived property name -> {"function_name", "function_version"}.
    # Derived names are merged into property_names so Views can reference them;
    # this map tells the read path how each one is computed.
    derived_properties: FrozenJsonMap | None = None

    @field_validator("required_scopes", "required_markings")
    @classmethod
    def _validate_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_strings(values, "required_scopes")

    @field_validator("property_names")
    @classmethod
    def _validate_property_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_strings(values, "property_names")

    @field_validator("source_object_types", "target_object_types")
    @classmethod
    def _validate_endpoints(
        cls,
        values: tuple[OntologySchemaReference, ...],
        info,
    ) -> tuple[OntologySchemaReference, ...]:
        return _normalized_references(values, info.field_name)

    @model_validator(mode="after")
    def _validate_shape(self) -> OntologySchemaContractSnapshot:
        if self.reference.schema_type is OntologySchemaType.OBJECT_TYPE:
            if self.source_object_types or self.target_object_types:
                raise ValueError("Object Type snapshot cannot declare relation endpoints")
            return self
        if self.property_names:
            raise ValueError("Relation snapshot cannot declare object properties")
        if not self.source_object_types or not self.target_object_types:
            raise ValueError("Relation snapshot requires source and target endpoints")
        for endpoint in self.source_object_types + self.target_object_types:
            if endpoint.schema_type is not OntologySchemaType.OBJECT_TYPE:
                raise ValueError("relation endpoints must reference Object Types")
            if endpoint.tenant_id != self.reference.tenant_id:
                raise ValueError("relation endpoint tenant must match relation tenant")
        return self


@runtime_checkable
class OntologySchemaContractResolver(Protocol):
    def resolve_exact(
        self,
        reference: OntologySchemaReference,
    ) -> OntologySchemaContractSnapshot: ...


def resolve_exact(
    registry: DefinitionRegistryPort,
    reference: DefinitionReference,
    *,
    require_published: bool = True,
) -> Definition:
    if not isinstance(reference, DefinitionReference):
        raise DefinitionResolutionError(
            "exact_reference_required",
            "resolution requires an exact DefinitionReference",
        )
    try:
        validated_reference = DefinitionReference.model_validate(reference)
        raw_definition = registry.get_exact(validated_reference)
    except DefinitionRegistryError as error:
        raise DefinitionResolutionError(error.code, str(error)) from error
    except ValidationError as error:
        raise DefinitionResolutionError(
            "exact_reference_invalid",
            "exact DefinitionReference is invalid",
        ) from error
    if type(raw_definition) not in _CONCRETE_DEFINITION_CLASSES:
        raise DefinitionResolutionError(
            "definition_registry_contract_invalid",
            "Definition Registry returned an unsupported payload",
        )
    try:
        definition = type(raw_definition).model_validate(raw_definition)
    except ValidationError as error:
        raise DefinitionResolutionError(
            "definition_registry_contract_invalid",
            "Definition Registry returned an invalid Definition",
        ) from error
    if definition.reference() != validated_reference:
        raise DefinitionResolutionError(
            "definition_registry_contract_mismatch",
            "Definition Registry returned different exact coordinates",
        )
    if require_published and definition.status is not DefinitionStatus.PUBLISHED:
        raise DefinitionResolutionError(
            "definition_not_published",
            "exact Definition is not published",
        )
    return definition


def validate_capability_binding(
    definition: Definition,
    snapshot: CapabilityContractSnapshot,
) -> CapabilityContractSnapshot:
    if type(definition) not in (FunctionDefinition, ActionDefinition):
        raise CapabilityBindingError(
            "definition_has_no_capability_binding",
            "only Function and Action Definitions bind Capabilities",
        )
    try:
        validated_definition = type(definition).model_validate(definition)
    except ValidationError as error:
        raise CapabilityBindingError(
            "definition_invalid",
            "Definition failed frozen contract validation",
        ) from error
    if type(snapshot) is not CapabilityContractSnapshot:
        raise CapabilityBindingError(
            "capability_snapshot_invalid",
            "Capability snapshot must use the frozen local contract",
        )
    try:
        validated_snapshot = CapabilityContractSnapshot.model_validate(snapshot)
    except ValidationError as error:
        raise CapabilityBindingError(
            "capability_snapshot_invalid",
            "Capability snapshot is invalid",
        ) from error

    expected = validated_definition.capability_binding
    comparisons = (
        (
            expected.capability_name,
            validated_snapshot.capability_name,
            "capability_name_mismatch",
        ),
        (
            expected.capability_version,
            validated_snapshot.capability_version,
            "capability_version_mismatch",
        ),
        (
            expected.schema_hash,
            validated_snapshot.schema_hash,
            "capability_schema_hash_mismatch",
        ),
    )
    for expected_value, actual_value, code in comparisons:
        if expected_value != actual_value:
            raise CapabilityBindingError(code, "Capability Binding triple mismatch")

    missing_scopes = set(validated_snapshot.required_scopes).difference(
        validated_definition.required_scopes
    )
    if missing_scopes:
        raise CapabilityBindingError(
            "capability_scope_weakening",
            "Definition omits Capability-required scopes",
        )

    if isinstance(validated_definition, FunctionDefinition):
        if validated_snapshot.kind is not CapabilityContractKind.ATOMIC:
            raise CapabilityBindingError(
                "function_capability_not_atomic",
                "Function must bind an Atomic Capability",
            )
        if validated_snapshot.has_side_effects:
            raise CapabilityBindingError(
                "function_capability_has_side_effects",
                "Function Capability must be read-only",
            )
        return validated_snapshot

    if not validated_snapshot.has_side_effects:
        raise CapabilityBindingError(
            "action_capability_read_only",
            "Action must bind a side-effecting Capability",
        )
    if not validated_snapshot.idempotent:
        raise CapabilityBindingError(
            "action_capability_not_idempotent",
            "Action Capability must be idempotent",
        )
    if _RISK_ORDER[validated_definition.governance.risk_level] < _RISK_ORDER[
        validated_snapshot.risk_level
    ]:
        raise CapabilityBindingError(
            "capability_risk_downgrade",
            "Action risk declaration is weaker than the Capability snapshot",
        )
    return validated_snapshot


class DefinitionGraphValidator:
    """Recursively validates exact Definition and Ontology schema edges."""

    def __init__(
        self,
        registry: DefinitionRegistryPort,
        schema_resolver: OntologySchemaContractResolver,
    ) -> None:
        self._registry = registry
        self._schema_resolver = schema_resolver

    def validate(self, definition: Definition) -> Definition:
        if type(definition) not in _CONCRETE_DEFINITION_CLASSES:
            raise DefinitionGraphError(
                "definition_type_unsupported",
                "graph validation requires a concrete Definition",
            )
        try:
            validated = type(definition).model_validate(definition)
        except ValidationError as error:
            raise DefinitionGraphError(
                "definition_invalid",
                "Definition is invalid",
            ) from error
        self._validate_definition(validated, set())
        return validated

    def _validate_definition(
        self,
        definition: Definition,
        visited: set[tuple[str, DefinitionType, str, int, str]],
    ) -> None:
        identity = self._definition_identity(definition)
        if identity in visited:
            return
        visited.add(identity)

        if isinstance(definition, FunctionDefinition):
            self._validate_function(definition, visited)
        elif isinstance(definition, ActionDefinition):
            self._validate_action(definition)
        elif isinstance(definition, ObjectSetDefinition):
            self._validate_object_set(definition)
        elif isinstance(definition, ObjectViewDefinition):
            self._validate_view(definition, visited)
        elif isinstance(definition, InterfaceDefinition):
            self._validate_interface(definition, visited)

    @staticmethod
    def _definition_identity(
        definition: Definition,
    ) -> tuple[str, DefinitionType, str, int, str]:
        if definition.contract_digest is None:  # pragma: no cover - model invariant
            raise DefinitionGraphError(
                "definition_digest_missing",
                "Definition digest is unavailable",
            )
        return (
            definition.tenant_id,
            definition.definition_type,
            definition.stable_name,
            definition.version,
            definition.contract_digest,
        )

    def _resolve_definition_edge(
        self,
        parent: Definition,
        reference: DefinitionReference,
        expected_type: DefinitionType,
        visited: set[tuple[str, DefinitionType, str, int, str]],
    ) -> Definition:
        try:
            target = resolve_exact(self._registry, reference)
        except DefinitionResolutionError as error:
            raise DefinitionGraphError(error.code, str(error)) from error
        if target.definition_type is not expected_type:
            raise DefinitionGraphError(
                "definition_graph_type_mismatch",
                "Definition graph edge targets the wrong Definition type",
            )
        if target.tenant_id != parent.tenant_id:
            raise DefinitionGraphError(
                "definition_graph_tenant_mismatch",
                "Definition graph edge crosses tenants",
            )
        if set(target.required_scopes).difference(parent.required_scopes):
            raise DefinitionGraphError(
                "definition_graph_scope_weakening",
                "referencing Definition weakens target scopes",
            )
        if set(target.required_markings).difference(parent.required_markings):
            raise DefinitionGraphError(
                "definition_graph_marking_weakening",
                "referencing Definition weakens target markings",
            )
        self._validate_definition(target, visited)
        return target

    def _resolve_schema(
        self,
        parent: Definition,
        reference: OntologySchemaReference,
        expected_type: OntologySchemaType,
    ) -> OntologySchemaContractSnapshot:
        try:
            raw_snapshot = self._schema_resolver.resolve_exact(reference)
            snapshot = OntologySchemaContractSnapshot.model_validate(raw_snapshot)
        except OntologySchemaContractNotFound as error:
            raise DefinitionGraphError(error.code, str(error)) from error
        except KeyError as error:
            raise DefinitionGraphError(
                "ontology_schema_not_found",
                "exact Ontology schema snapshot was not found",
            ) from error
        except ValidationError as error:
            raise DefinitionGraphError(
                "ontology_schema_snapshot_invalid",
                "Ontology schema snapshot is invalid",
            ) from error
        if snapshot.reference != reference:
            raise DefinitionGraphError(
                "ontology_schema_reference_mismatch",
                "Ontology resolver returned different exact coordinates",
            )
        if reference.tenant_id != parent.tenant_id:
            raise DefinitionGraphError(
                "ontology_schema_tenant_mismatch",
                "Ontology schema edge crosses tenants",
            )
        if reference.schema_type is not expected_type:
            raise DefinitionGraphError(
                "ontology_schema_type_mismatch",
                "Ontology schema edge has the wrong kind",
            )
        if snapshot.status is not OntologySchemaLifecycle.PUBLISHED:
            raise DefinitionGraphError(
                "ontology_schema_not_published",
                "Ontology schema snapshot is not published",
            )
        if set(snapshot.required_scopes).difference(parent.required_scopes):
            raise DefinitionGraphError(
                "ontology_schema_scope_weakening",
                "Definition weakens Ontology schema scopes",
            )
        if set(snapshot.required_markings).difference(parent.required_markings):
            raise DefinitionGraphError(
                "ontology_schema_marking_weakening",
                "Definition weakens Ontology schema markings",
            )
        if snapshot.reference.schema_type is OntologySchemaType.RELATION_TYPE:
            for endpoint in (
                snapshot.source_object_types + snapshot.target_object_types
            ):
                self._resolve_schema(
                    parent,
                    endpoint,
                    OntologySchemaType.OBJECT_TYPE,
                )
        return snapshot

    def _validate_property(
        self,
        parent: Definition,
        reference: PropertyReference,
    ) -> None:
        snapshot = self._resolve_schema(
            parent,
            reference.object_type,
            OntologySchemaType.OBJECT_TYPE,
        )
        if reference.property_name not in snapshot.property_names:
            raise DefinitionGraphError(
                "ontology_property_not_found",
                "Property is absent from the exact Object Type snapshot",
            )

    def _validate_function(
        self,
        definition: FunctionDefinition,
        visited: set[tuple[str, DefinitionType, str, int, str]],
    ) -> None:
        for target in definition.applies_to:
            if target.object_type is not None:
                self._resolve_schema(
                    definition,
                    target.object_type,
                    OntologySchemaType.OBJECT_TYPE,
                )
            elif target.object_set is not None:
                self._resolve_definition_edge(
                    definition,
                    target.object_set,
                    DefinitionType.OBJECT_SET,
                    visited,
                )
        for dependency in definition.property_dependencies:
            self._validate_property(definition, dependency)
        for dependency in definition.cache_policy.vary_by:
            self._validate_property(definition, dependency)
        for relation in definition.link_dependencies:
            self._resolve_schema(
                definition,
                relation,
                OntologySchemaType.RELATION_TYPE,
            )

    def _validate_action(self, definition: ActionDefinition) -> None:
        for object_type in definition.object_types:
            self._resolve_schema(
                definition,
                object_type,
                OntologySchemaType.OBJECT_TYPE,
            )
        for precondition in definition.preconditions:
            for dependency in precondition.property_dependencies:
                self._validate_property(definition, dependency)
        for object_type in definition.governance.change_scope.object_types:
            self._resolve_schema(
                definition,
                object_type,
                OntologySchemaType.OBJECT_TYPE,
            )
        for property_reference in definition.governance.change_scope.properties:
            self._validate_property(definition, property_reference)

    def _validate_object_set(self, definition: ObjectSetDefinition) -> None:
        self._resolve_schema(
            definition,
            definition.object_type,
            OntologySchemaType.OBJECT_TYPE,
        )
        for item in definition.filters:
            self._validate_property(definition, item.property)
        for item in definition.sort:
            self._validate_property(definition, item.property)
        for item in definition.projection:
            self._validate_property(definition, item)
        for traversal in definition.link_traversals:
            relation = self._resolve_schema(
                definition,
                traversal.relation,
                OntologySchemaType.RELATION_TYPE,
            )
            self._resolve_schema(
                definition,
                traversal.target_object_type,
                OntologySchemaType.OBJECT_TYPE,
            )
            outbound = (
                definition.object_type in relation.source_object_types
                and traversal.target_object_type in relation.target_object_types
            )
            inbound = (
                definition.object_type in relation.target_object_types
                and traversal.target_object_type in relation.source_object_types
            )
            valid = (
                traversal.direction is LinkDirection.BOTH
                and (outbound or inbound)
            ) or (
                traversal.direction is LinkDirection.OUTBOUND and outbound
            ) or (
                traversal.direction is LinkDirection.INBOUND and inbound
            )
            if not valid:
                raise DefinitionGraphError(
                    "ontology_relation_endpoint_mismatch",
                    "Relation endpoints do not match Link traversal direction",
                )

    def _validate_view(
        self,
        definition: ObjectViewDefinition,
        visited: set[tuple[str, DefinitionType, str, int, str]],
    ) -> None:
        source_type: OntologySchemaReference
        if definition.source.object_type is not None:
            source_type = definition.source.object_type
            self._resolve_schema(
                definition,
                source_type,
                OntologySchemaType.OBJECT_TYPE,
            )
        else:
            if definition.source.object_set is None:  # pragma: no cover
                raise DefinitionGraphError(
                    "definition_graph_source_missing",
                    "Object View source is missing",
                )
            source = self._resolve_definition_edge(
                definition,
                definition.source.object_set,
                DefinitionType.OBJECT_SET,
                visited,
            )
            if not isinstance(source, ObjectSetDefinition):  # pragma: no cover
                raise DefinitionGraphError(
                    "definition_graph_type_mismatch",
                    "Object View source is not an Object Set",
                )
            source_type = source.object_type
        for field in definition.fields:
            if field.object_type != source_type:
                raise DefinitionGraphError(
                    "definition_graph_view_source_mismatch",
                    "Object View field does not belong to its exact source",
                )
            self._validate_property(definition, field)
        for relation in definition.relations:
            self._resolve_schema(
                definition,
                relation,
                OntologySchemaType.RELATION_TYPE,
            )
        for function in definition.functions:
            self._resolve_definition_edge(
                definition,
                function,
                DefinitionType.FUNCTION,
                visited,
            )
        for action in definition.actions:
            self._resolve_definition_edge(
                definition,
                action,
                DefinitionType.ACTION,
                visited,
            )

    def _validate_interface(
        self,
        definition: InterfaceDefinition,
        visited: set[tuple[str, DefinitionType, str, int, str]],
    ) -> None:
        view = self._resolve_definition_edge(
            definition,
            definition.object_view,
            DefinitionType.OBJECT_VIEW,
            visited,
        )
        if not isinstance(view, ObjectViewDefinition):  # pragma: no cover
            raise DefinitionGraphError(
                "definition_graph_type_mismatch",
                "Interface target is not an Object View",
            )
        for function in definition.functions:
            self._resolve_definition_edge(
                definition,
                function,
                DefinitionType.FUNCTION,
                visited,
            )
            if function not in view.functions:
                raise DefinitionGraphError(
                    "definition_graph_interface_exposure_mismatch",
                    "Interface exposes a Function absent from its Object View",
                )
        for action in definition.actions:
            self._resolve_definition_edge(
                definition,
                action,
                DefinitionType.ACTION,
                visited,
            )
            if action not in view.actions:
                raise DefinitionGraphError(
                    "definition_graph_interface_exposure_mismatch",
                    "Interface exposes an Action absent from its Object View",
                )
