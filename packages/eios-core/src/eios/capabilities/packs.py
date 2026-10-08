from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eios.control.models import normalize_scopes
from eios.kernel.ports.repositories import OntologyRegistry, OntologyStore

from .base import BaseCapability
from .models import ExecutionMode


PACK_MANIFEST_SCHEMA = "eios-capability-pack/v1"
_HEX_64_PATTERN = r"^[0-9a-f]{64}$"
_QUEUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_FACTORY_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$",
    re.ASCII,
)


class CapabilityPackConflict(ValueError):
    code = "capability_pack_conflict"


class CapabilityBindingUnavailable(LookupError):
    code = "capability_binding_unavailable"


T = TypeVar("T")


class CapabilityBindingResolver(Protocol):
    def resolve(self, binding_key: str, *, expected_type: type[T]) -> T: ...


class RejectingCapabilityBindingResolver:
    def resolve(self, binding_key: str, *, expected_type: type[T]) -> T:
        del expected_type
        clean = str(binding_key or "").strip()
        raise CapabilityBindingUnavailable(
            f"unknown capability binding: {clean or '<empty>'}"
        )


@dataclass(frozen=True, slots=True)
class CapabilityFactoryContext:
    ontology_registry: OntologyRegistry
    ontology_store: OntologyStore
    binding_resolver: CapabilityBindingResolver
    # Live external-inventory readers, keyed by booking system code. Optional
    # by design: a process that must not reach an external booking system
    # leaves this empty and the affected capabilities fail closed instead of
    # fabricating inventory. Never a route for writes — reads only.
    tennis_inventory_providers: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.ontology_registry is None or self.ontology_store is None:
            raise ValueError("ontology dependencies are required")
        if self.tennis_inventory_providers is not None and not isinstance(
            self.tennis_inventory_providers, Mapping
        ):
            raise ValueError("tennis inventory providers are invalid")
        if self.binding_resolver is None or not callable(
            getattr(self.binding_resolver, "resolve", None)
        ):
            raise ValueError("binding resolver is required")


CapabilityFactory = Callable[[CapabilityFactoryContext], BaseCapability]


def _pack_identifier(value: str) -> str:
    if not value or value != value.strip() or not value.isprintable():
        raise ValueError("pack identifier is invalid")
    return value


def _execution_mode(value: ExecutionMode | str) -> str:
    if isinstance(value, ExecutionMode):
        return value.value
    try:
        return ExecutionMode(value).value
    except ValueError:
        raise ValueError("execution_mode is invalid") from None


def capability_contract_revision(
    *,
    name: str,
    version: str,
    schema_hash: str,
    execution_mode: ExecutionMode | str,
    queue: str,
    auth_scopes: Iterable[str],
) -> str:
    if not re.fullmatch(_HEX_64_PATTERN, schema_hash):
        raise ValueError("schema_hash must be 64 lowercase hexadecimal characters")
    payload = {
        "auth_scopes": list(normalize_scopes(auth_scopes)),
        "execution_mode": _execution_mode(execution_mode),
        "name": _pack_identifier(name),
        "queue": str(queue),
        "schema_hash": schema_hash,
        "version": _pack_identifier(version),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CapabilityPackEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str
    version: str
    schema_hash: str = Field(pattern=_HEX_64_PATTERN)
    contract_revision: str = Field(pattern=_HEX_64_PATTERN)
    execution_mode: ExecutionMode
    queue: str
    auth_scopes: tuple[str, ...]
    factory: str

    @field_validator("name", "version")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        return _pack_identifier(value)

    @field_validator("auth_scopes", mode="before")
    @classmethod
    def _normalize_auth_scopes(cls, value) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if not normalized:
            raise ValueError("auth_scopes must not be empty")
        return normalized

    @field_validator("factory")
    @classmethod
    def _validate_factory(cls, value: str) -> str:
        if not _FACTORY_PATTERN.fullmatch(value):
            raise ValueError("factory must be a module:callable reference")
        return value

    @model_validator(mode="after")
    def _validate_contract(self) -> CapabilityPackEntry:
        if not _QUEUE_PATTERN.fullmatch(self.queue):
            raise ValueError("queue is invalid")
        if self.execution_mode is ExecutionMode.SYNC and self.queue != "inline":
            raise ValueError("sync capability queue must be inline")
        expected = capability_contract_revision(
            name=self.name,
            version=self.version,
            schema_hash=self.schema_hash,
            execution_mode=self.execution_mode,
            queue=self.queue,
            auth_scopes=self.auth_scopes,
        )
        if self.contract_revision != expected:
            raise ValueError("contract revision does not match canonical entry")
        return self


def capability_pack_revision(
    *,
    pack_name: str,
    pack_version: str,
    capabilities: Iterable[CapabilityPackEntry],
) -> str:
    entries = tuple(capabilities)
    payload = {
        "contract": PACK_MANIFEST_SCHEMA,
        "pack_name": _pack_identifier(pack_name),
        "pack_version": _pack_identifier(pack_version),
        "capabilities": [
            {
                "name": entry.name,
                "version": entry.version,
                "schema_hash": entry.schema_hash,
                "contract_revision": entry.contract_revision,
                "execution_mode": entry.execution_mode.value,
                "queue": entry.queue,
                "auth_scopes": list(entry.auth_scopes),
            }
            for entry in entries
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CapabilityPackManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    manifest_schema: str
    pack_name: str
    pack_version: str
    pack_revision: str = Field(pattern=_HEX_64_PATTERN)
    capabilities: tuple[CapabilityPackEntry, ...]

    @field_validator("pack_name", "pack_version")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        return _pack_identifier(value)

    @model_validator(mode="after")
    def _validate_manifest(self) -> CapabilityPackManifest:
        if self.manifest_schema != PACK_MANIFEST_SCHEMA:
            raise ValueError("manifest_schema is invalid")
        if not self.capabilities:
            raise ValueError("capability pack must not be empty")
        keys = [(entry.name, entry.version) for entry in self.capabilities]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate capability entry")
        if keys != sorted(keys):
            raise ValueError("capability entries must be sorted")
        expected = capability_pack_revision(
            pack_name=self.pack_name,
            pack_version=self.pack_version,
            capabilities=self.capabilities,
        )
        if self.pack_revision != expected:
            raise ValueError("pack revision does not match canonical manifest")
        return self


@dataclass(frozen=True, slots=True)
class LoadedCapabilityPack:
    manifest: CapabilityPackManifest
    capabilities: tuple[BaseCapability, ...]

    def __post_init__(self) -> None:
        capabilities = tuple(self.capabilities)
        object.__setattr__(self, "capabilities", capabilities)
        declared = {
            (entry.name, entry.version): entry
            for entry in self.manifest.capabilities
        }
        actual: dict[tuple[str, str], BaseCapability] = {}
        for capability in capabilities:
            if not isinstance(capability, BaseCapability):
                raise CapabilityPackConflict("factory must return BaseCapability")
            metadata = capability.metadata()
            key = (metadata.name, metadata.version)
            if key in actual:
                raise CapabilityPackConflict("duplicate loaded capability")
            actual[key] = capability
        missing = sorted(set(declared) - set(actual))
        extra = sorted(set(actual) - set(declared))
        if missing:
            raise CapabilityPackConflict("missing declared capability")
        if extra:
            raise CapabilityPackConflict("extra loaded capability")
        for key, entry in declared.items():
            metadata = actual[key].metadata()
            observed = (
                metadata.name,
                metadata.version,
                metadata.schema_hash,
                metadata.execution_mode,
                metadata.queue,
                normalize_scopes(metadata.auth_scopes),
            )
            expected = (
                entry.name,
                entry.version,
                entry.schema_hash,
                entry.execution_mode,
                entry.queue,
                entry.auth_scopes,
            )
            if observed != expected:
                raise CapabilityPackConflict("capability metadata conflicts with manifest")

    def entry(self, name: str, version: str) -> CapabilityPackEntry:
        for entry in self.manifest.capabilities:
            if entry.name == name and entry.version == version:
                return entry
        raise CapabilityPackConflict("capability entry is missing")

    def capability(self, name: str, version: str) -> BaseCapability:
        for capability in self.capabilities:
            metadata = capability.metadata()
            if metadata.name == name and metadata.version == version:
                return capability
        raise CapabilityPackConflict("loaded capability is missing")
