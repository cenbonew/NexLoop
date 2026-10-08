from importlib import import_module


__all__ = [
    "EventTypeDefinition",
    "InMemoryOntologyRegistry",
    "InMemoryOntologyStore",
    "ObjectTypeDefinition",
    "PropertyDefinition",
    "RelationTypeDefinition",
]

_LAZY_EXPORTS = {
    "EventTypeDefinition": ("eios.ontology.models", "EventTypeDefinition"),
    "InMemoryOntologyRegistry": (
        "eios.ontology.registry",
        "InMemoryOntologyRegistry",
    ),
    "InMemoryOntologyStore": ("eios.ontology.store", "InMemoryOntologyStore"),
    "ObjectTypeDefinition": ("eios.ontology.models", "ObjectTypeDefinition"),
    "PropertyDefinition": ("eios.ontology.models", "PropertyDefinition"),
    "RelationTypeDefinition": ("eios.ontology.models", "RelationTypeDefinition"),
}


def __getattr__(name: str) -> object:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
