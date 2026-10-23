"""Static Definition dependency graph and impact analysis (stage 10).

A pure read-side assembler: it extracts the dependency edges already frozen
into the Definition contracts and answers "if this node changes, what breaks"
without any runtime or execution data. Nodes are Definitions and the ontology
schema elements they reference (object types, properties, relations); edges
point from a dependent to its dependency, so downstream impact of a node is
everything that transitively depends on it.

This is the zero-runtime-dependency deliverable of the lineage stage — it can
gate CI ("changing this Property breaks these Actions and Views") today, while
the runtime edge ledger waits behind the async worker.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Literal

from eios.ontology.definitions import (
    ActionDefinition,
    Definition,
    FunctionDefinition,
    InterfaceDefinition,
    ObjectSetDefinition,
    ObjectViewDefinition,
    OntologySchemaReference,
    OntologySchemaType,
    PropertyReference,
)


Direction = Literal["upstream", "downstream"]


def definition_node(reference) -> str:
    return f"definition:{reference.definition_type.value}:{reference.stable_name}:{reference.version}"


def _definition_node_from(definition: Definition) -> str:
    return (
        f"definition:{definition.definition_type.value}:"
        f"{definition.stable_name}:{definition.version}"
    )


def object_type_node(reference: OntologySchemaReference) -> str:
    return f"object_type:{reference.stable_name}:{reference.version}"


def relation_node(reference: OntologySchemaReference) -> str:
    return f"relation:{reference.stable_name}:{reference.version}"


def property_node(reference: PropertyReference) -> str:
    return (
        f"property:{reference.object_type.stable_name}:"
        f"{reference.object_type.version}:{reference.property_name}"
    )


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    dependent: str
    dependency: str
    kind: str


@dataclass(slots=True)
class DefinitionDependencyGraph:
    """Directed dependency graph over Definitions and schema elements."""

    nodes: set[str] = field(default_factory=set)
    edges: list[DependencyEdge] = field(default_factory=list)
    _forward: dict[str, set[str]] = field(default_factory=dict)
    _reverse: dict[str, set[str]] = field(default_factory=dict)

    def add_edge(self, dependent: str, dependency: str, kind: str) -> None:
        if dependent == dependency:
            return
        self.nodes.add(dependent)
        self.nodes.add(dependency)
        edge = DependencyEdge(dependent, dependency, kind)
        self.edges.append(edge)
        self._forward.setdefault(dependent, set()).add(dependency)
        self._reverse.setdefault(dependency, set()).add(dependent)

    def impact_of(
        self, node: str, direction: Direction, *, max_hops: int | None = None
    ) -> tuple[tuple[str, int], ...]:
        """BFS over the graph; returns (node, hop) pairs excluding the origin.

        direction 'downstream' answers "what depends on this node" (impact);
        'upstream' answers "what this node depends on".
        """

        adjacency = (
            self._reverse if direction == "downstream" else self._forward
        )
        seen = {node}
        frontier: deque[tuple[str, int]] = deque([(node, 0)])
        result: list[tuple[str, int]] = []
        while frontier:
            current, hop = frontier.popleft()
            if max_hops is not None and hop >= max_hops:
                continue
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                result.append((neighbor, hop + 1))
                frontier.append((neighbor, hop + 1))
        return tuple(result)


def _add_schema_edge(
    graph: DefinitionDependencyGraph,
    dependent: str,
    reference: OntologySchemaReference,
    kind: str,
) -> None:
    if reference.schema_type is OntologySchemaType.OBJECT_TYPE:
        graph.add_edge(dependent, object_type_node(reference), kind)
    else:
        graph.add_edge(dependent, relation_node(reference), kind)


def _add_property_edge(
    graph: DefinitionDependencyGraph,
    dependent: str,
    reference: PropertyReference,
    kind: str,
) -> None:
    node = property_node(reference)
    graph.add_edge(dependent, node, kind)
    # A property depends on its object type.
    graph.add_edge(node, object_type_node(reference.object_type), "property_of")


def _collect_object_set(
    graph: DefinitionDependencyGraph, definition: ObjectSetDefinition
) -> None:
    node = _definition_node_from(definition)
    _add_schema_edge(graph, node, definition.object_type, "object_type")
    for entry in definition.filters:
        _add_property_edge(graph, node, entry.property, "filter")
    for entry in definition.projection:
        _add_property_edge(graph, node, entry, "projection")
    for traversal in definition.link_traversals:
        _add_schema_edge(graph, node, traversal.relation, "traversal")
        _add_schema_edge(
            graph, node, traversal.target_object_type, "traversal_target"
        )
    for aggregation in definition.aggregations:
        if aggregation.property is not None:
            _add_property_edge(graph, node, aggregation.property, "aggregation")
        for reference in aggregation.group_by:
            _add_property_edge(graph, node, reference, "group_by")


def _collect_object_view(
    graph: DefinitionDependencyGraph, definition: ObjectViewDefinition
) -> None:
    node = _definition_node_from(definition)
    if definition.source.object_type is not None:
        _add_schema_edge(graph, node, definition.source.object_type, "source")
    if definition.source.object_set is not None:
        graph.add_edge(
            node, definition_node(definition.source.object_set), "source_set"
        )
    for entry in definition.fields:
        _add_property_edge(graph, node, entry, "field")
    for relation in definition.relations:
        _add_schema_edge(graph, node, relation, "relation")
    for function in definition.functions:
        graph.add_edge(node, definition_node(function), "function")
    for action in definition.actions:
        graph.add_edge(node, definition_node(action), "action")


def _collect_function(
    graph: DefinitionDependencyGraph, definition: FunctionDefinition
) -> None:
    node = _definition_node_from(definition)
    for target in definition.applies_to:
        if target.object_type is not None:
            _add_schema_edge(graph, node, target.object_type, "applies_to")
        if target.object_set is not None:
            graph.add_edge(node, definition_node(target.object_set), "applies_to_set")
    for dependency in definition.property_dependencies:
        _add_property_edge(graph, node, dependency, "property_dependency")
    for dependency in definition.cache_policy.vary_by:
        _add_property_edge(graph, node, dependency, "cache_vary_by")


def _collect_action(
    graph: DefinitionDependencyGraph, definition: ActionDefinition
) -> None:
    node = _definition_node_from(definition)
    for object_type in definition.object_types:
        _add_schema_edge(graph, node, object_type, "object_type")
    # governance change_scope is a write edge.
    for object_type in definition.governance.change_scope.object_types:
        _add_schema_edge(graph, node, object_type, "writes")
    for reference in definition.governance.change_scope.properties:
        _add_property_edge(graph, node, reference, "writes_property")


def _collect_interface(
    graph: DefinitionDependencyGraph, definition: InterfaceDefinition
) -> None:
    node = _definition_node_from(definition)
    graph.add_edge(node, definition_node(definition.object_view), "object_view")
    for function in definition.functions:
        graph.add_edge(node, definition_node(function), "function")
    for action in definition.actions:
        graph.add_edge(node, definition_node(action), "action")


_COLLECTORS = {
    ObjectSetDefinition: _collect_object_set,
    ObjectViewDefinition: _collect_object_view,
    FunctionDefinition: _collect_function,
    ActionDefinition: _collect_action,
    InterfaceDefinition: _collect_interface,
}


def build_dependency_graph(
    definitions: Iterable[Definition],
) -> DefinitionDependencyGraph:
    """Assemble the static dependency graph from a Definition set."""

    graph = DefinitionDependencyGraph()
    for definition in definitions:
        graph.nodes.add(_definition_node_from(definition))
        collector = _COLLECTORS.get(type(definition))
        if collector is not None:
            collector(graph, definition)
    return graph


__all__ = [
    "DefinitionDependencyGraph",
    "DependencyEdge",
    "Direction",
    "build_dependency_graph",
    "definition_node",
    "object_type_node",
    "property_node",
    "relation_node",
]
