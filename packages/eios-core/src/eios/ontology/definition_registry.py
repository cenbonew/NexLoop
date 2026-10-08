from __future__ import annotations

from dataclasses import dataclass
import re
from threading import RLock
from typing import Protocol, runtime_checkable

from pydantic import Field, ValidationError, field_validator

from eios.ontology.definitions import (
    ActionDefinition,
    Definition,
    DefinitionReference,
    DefinitionType,
    FrozenContract,
    FunctionDefinition,
    InterfaceDefinition,
    ObjectSetDefinition,
    ObjectViewDefinition,
)


_STABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
_DEFINITION_CLASSES = (
    FunctionDefinition,
    ActionDefinition,
    ObjectSetDefinition,
    ObjectViewDefinition,
    InterfaceDefinition,
)


class DefinitionRegistryError(ValueError):
    """Stable fail-closed Registry error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DefinitionConflictError(DefinitionRegistryError):
    """An immutable exact key already contains different content."""


class DefinitionVersionError(DefinitionRegistryError):
    """A registration would violate the one linear version chain."""


class DefinitionNotFoundError(DefinitionRegistryError):
    """No exact Definition matches the requested coordinates."""


class DefinitionKey(FrozenContract):
    tenant_id: str
    definition_type: DefinitionType
    stable_name: str
    version: int = Field(gt=0)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("tenant_id must be non-empty")
        return clean

    @field_validator("stable_name")
    @classmethod
    def _validate_stable_name(cls, value: str) -> str:
        clean = value.strip()
        if _STABLE_NAME_RE.fullmatch(clean) is None:
            raise ValueError("stable_name must be a stable dotted name")
        return clean

    @classmethod
    def from_definition(cls, definition: Definition) -> DefinitionKey:
        return cls(
            tenant_id=definition.tenant_id,
            definition_type=definition.definition_type,
            stable_name=definition.stable_name,
            version=definition.version,
        )

    @classmethod
    def from_reference(cls, reference: DefinitionReference) -> DefinitionKey:
        return cls(
            tenant_id=reference.tenant_id,
            definition_type=reference.definition_type,
            stable_name=reference.stable_name,
            version=reference.version,
        )


@dataclass(frozen=True, slots=True)
class DefinitionRegistration:
    definition: Definition
    registry_revision: int
    replayed: bool


@runtime_checkable
class DefinitionRegistryPort(Protocol):
    @property
    def revision(self) -> int: ...

    def register(self, definition: Definition) -> DefinitionRegistration: ...

    def get_by_key(self, key: DefinitionKey) -> Definition: ...

    def get_exact(self, reference: DefinitionReference) -> Definition: ...

    def list_definitions(
        self,
        *,
        tenant_id: str,
        definition_type: DefinitionType,
        stable_name: str | None = None,
    ) -> tuple[Definition, ...]: ...


@runtime_checkable
class AtomicDefinitionRegistryPort(DefinitionRegistryPort, Protocol):
    """Registry extension for one-tenant, all-or-nothing frozen bundles.

    ``definitions`` and the returned registrations are exact tuples. Empty or
    cross-tenant bundles fail before locking or writing. A successful call
    makes every new Definition visible together; each new Definition advances
    the global revision once, while an exact replay retains its original
    revision and reports ``replayed=True``. Any failure leaves definitions,
    chain heads, and the global revision unchanged.
    """

    def register_batch(
        self,
        definitions: tuple[Definition, ...],
    ) -> tuple[DefinitionRegistration, ...]: ...


class InMemoryDefinitionRegistry:
    """Thread-safe immutable Definition history with no latest resolution."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[DefinitionKey, Definition] = {}
        self._record_revisions: dict[DefinitionKey, int] = {}
        self._heads: dict[tuple[str, DefinitionType, str], DefinitionKey] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def register(self, definition: Definition) -> DefinitionRegistration:
        if type(definition) not in _DEFINITION_CLASSES:
            raise DefinitionConflictError(
                "definition_type_unsupported",
                "Registry accepts only concrete Domain Expansion Definitions",
            )
        try:
            validated = type(definition).model_validate(definition)
        except ValidationError as error:
            raise DefinitionConflictError(
                "definition_invalid",
                "Definition failed frozen contract validation",
            ) from error
        key = DefinitionKey.from_definition(validated)
        identity = (key.tenant_id, key.definition_type, key.stable_name)

        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if existing != validated:
                    raise DefinitionConflictError(
                        "definition_key_conflict",
                        "exact Definition key already contains different content",
                    )
                return DefinitionRegistration(
                    definition=existing,
                    registry_revision=self._record_revisions[key],
                    replayed=True,
                )

            head_key = self._heads.get(identity)
            self._validate_next_version(validated, head_key)

            self._revision += 1
            self._records[key] = validated
            self._record_revisions[key] = self._revision
            self._heads[identity] = key
            return DefinitionRegistration(
                definition=validated,
                registry_revision=self._revision,
                replayed=False,
            )

    def register_batch(
        self,
        definitions: tuple[Definition, ...],
    ) -> tuple[DefinitionRegistration, ...]:
        """Register one ordered bundle atomically, including injected failures."""

        if not isinstance(definitions, tuple) or not definitions:
            raise DefinitionConflictError(
                "definition_batch_invalid",
                "Definition batch must be a non-empty tuple",
            )
        if len({getattr(item, "tenant_id", None) for item in definitions}) != 1:
            raise DefinitionConflictError(
                "definition_batch_tenant_mismatch",
                "Definition batch must belong to one tenant",
            )
        with self._lock:
            snapshot = (
                dict(self._records),
                dict(self._record_revisions),
                dict(self._heads),
                self._revision,
            )
            try:
                return tuple(self.register(definition) for definition in definitions)
            except Exception:
                (
                    self._records,
                    self._record_revisions,
                    self._heads,
                    self._revision,
                ) = snapshot
                raise

    def _validate_next_version(
        self,
        definition: Definition,
        head_key: DefinitionKey | None,
    ) -> None:
        if head_key is None:
            if definition.version != 1:
                raise DefinitionVersionError(
                    "definition_version_gap",
                    "a Definition chain must start at version 1",
                )
            if definition.previous_version is not None:
                raise DefinitionVersionError(
                    "definition_predecessor_forbidden",
                    "version 1 must not declare a predecessor",
                )
            return

        expected_version = head_key.version + 1
        if definition.version != expected_version:
            code = (
                "definition_version_gap"
                if definition.version > expected_version
                else "definition_version_backfill"
            )
            raise DefinitionVersionError(
                code,
                f"next version must be exactly {expected_version}",
            )
        if definition.previous_version is None:
            raise DefinitionVersionError(
                "definition_predecessor_required",
                "version greater than 1 requires the exact current head",
            )
        head = self._records[head_key]
        if definition.previous_version != head.reference():
            raise DefinitionVersionError(
                "definition_predecessor_mismatch",
                "previous_version must equal the exact current head",
            )

    def get_by_key(self, key: DefinitionKey) -> Definition:
        validated_key = DefinitionKey.model_validate(key)
        with self._lock:
            definition = self._records.get(validated_key)
        if definition is None:
            raise DefinitionNotFoundError(
                "definition_not_found",
                "exact Definition key was not found",
            )
        return definition

    def get_exact(self, reference: DefinitionReference) -> Definition:
        validated_reference = DefinitionReference.model_validate(reference)
        definition = self.get_by_key(DefinitionKey.from_reference(validated_reference))
        if definition.contract_digest != validated_reference.contract_digest:
            raise DefinitionNotFoundError(
                "definition_digest_mismatch",
                "exact Definition digest does not match",
            )
        return definition

    def list_definitions(
        self,
        *,
        tenant_id: str,
        definition_type: DefinitionType,
        stable_name: str | None = None,
    ) -> tuple[Definition, ...]:
        clean_tenant = tenant_id.strip()
        if not clean_tenant:
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(definition_type, DefinitionType):
            raise ValueError("definition_type must be a DefinitionType")
        clean_name = stable_name.strip() if stable_name is not None else None
        if clean_name is not None and _STABLE_NAME_RE.fullmatch(clean_name) is None:
            raise ValueError("stable_name must be a stable dotted name")

        with self._lock:
            matches = tuple(
                definition
                for key, definition in self._records.items()
                if key.tenant_id == clean_tenant
                and key.definition_type is definition_type
                and (clean_name is None or key.stable_name == clean_name)
            )
        return tuple(
            sorted(
                matches,
                key=lambda item: (item.stable_name, item.version),
            )
        )
