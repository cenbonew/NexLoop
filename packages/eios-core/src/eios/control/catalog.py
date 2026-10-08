from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from eios.capabilities.base import BaseCapability
from eios.capabilities.models import (
    CapabilitySummary,
    CapabilityType,
    RiskLevel,
)
from eios.capabilities.packs import (
    CapabilityPackConflict,
    CapabilityPackEntry,
    LoadedCapabilityPack,
)

from .models import PolicyDecision, PolicyReasonCode, TenantContext
from .policy import CapabilityPolicy, _validated_context


# Catalog-only read override. A caller that holds this scope may *discover*
# every active capability (list, detail and contract) regardless of each
# capability's own ``auth_scopes``. It deliberately grants no execution: the
# validate/plan/execute path resolves capabilities through ``visible_capability``
# with ``admin`` left False, so an admin-scoped key still cannot invoke a
# capability it lacks the real scope for. Read/observe stays split from execute.
CATALOG_ADMIN_SCOPE = "catalog.admin"


# Capabilities suppressed from every discovery *listing* (the ``/api/capabilities``
# catalog and the agent manifest). They remain fully registered in the pack and
# stay resolvable/executable through ``get`` — so contract reads, direct execute
# and the test fixtures that rely on them are unaffected — they simply no longer
# clutter discovery. Used to retire the built-in echo demos from the surfaced
# catalog without a pack-revision change.
_CATALOG_HIDDEN_NAMES = frozenset({"example.echo", "example.async_echo"})


_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$",
    re.ASCII,
)
_PrereleaseIdentifier = tuple[int, int, str]
_VersionKey = tuple[int, int, int, int, tuple[_PrereleaseIdentifier, ...]]


def capability_version_key(version: str) -> _VersionKey:
    match = _SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError("capability version is invalid or unsupported")
    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key: tuple[_PrereleaseIdentifier, ...] = ()
        release_rank = 1
    else:
        identifiers: list[_PrereleaseIdentifier] = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    raise ValueError("capability version prerelease is invalid")
                identifiers.append((0, int(identifier), ""))
            else:
                identifiers.append((1, 0, identifier))
        prerelease_key = tuple(identifiers)
        release_rank = 0
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        release_rank,
        prerelease_key,
    )


@dataclass(frozen=True, slots=True)
class CatalogList:
    items: tuple[CapabilitySummary, ...]
    total: int
    revision: str
    hidden_count: int


@dataclass(frozen=True, slots=True)
class CatalogLookup:
    exists: bool
    capability: BaseCapability | None
    entry: CapabilityPackEntry | None
    decision: PolicyDecision | None
    revision: str


@dataclass(frozen=True, slots=True)
class _CatalogSnapshot:
    capability_type: CapabilityType
    category: str
    title: str
    description: str
    risk_level: RiskLevel
    tags: tuple[str, ...]
    deprecated: bool


@dataclass(frozen=True, slots=True)
class _CatalogItem:
    capability: BaseCapability
    entry: CapabilityPackEntry
    snapshot: _CatalogSnapshot
    version_key: _VersionKey

    def summary(self) -> CapabilitySummary:
        return CapabilitySummary(
            name=self.entry.name,
            version=self.entry.version,
            capability_type=self.snapshot.capability_type,
            category=self.snapshot.category,
            title=self.snapshot.title,
            description=self.snapshot.description,
            execution_mode=self.entry.execution_mode,
            risk_level=self.snapshot.risk_level,
            tags=self.snapshot.tags,
            deprecated=self.snapshot.deprecated,
            schema_hash=self.entry.schema_hash,
        )


class CatalogView:
    def __init__(self, loaded_pack: LoadedCapabilityPack) -> None:
        if not isinstance(loaded_pack, LoadedCapabilityPack):
            raise TypeError("loaded capability pack is required")
        observed: dict[tuple[str, str], tuple[BaseCapability, _CatalogSnapshot]] = {}
        for capability in loaded_pack.capabilities:
            metadata = capability.metadata()
            key = (metadata.name, metadata.version)
            if key in observed:
                raise CapabilityPackConflict("duplicate loaded capability")
            observed[key] = (
                capability,
                _CatalogSnapshot(
                    capability_type=metadata.capability_type,
                    category=metadata.category,
                    title=metadata.title,
                    description=metadata.description,
                    risk_level=metadata.risk_level,
                    tags=tuple(metadata.tags),
                    deprecated=metadata.deprecated,
                ),
            )
        items: list[_CatalogItem] = []
        for entry in loaded_pack.manifest.capabilities:
            loaded = observed.get((entry.name, entry.version))
            if loaded is None:
                raise CapabilityPackConflict("loaded capability is missing")
            capability, snapshot = loaded
            items.append(
                _CatalogItem(
                    capability=capability,
                    entry=entry,
                    snapshot=snapshot,
                    version_key=capability_version_key(entry.version),
                )
            )
        if len(items) != len(observed):
            raise CapabilityPackConflict("extra loaded capability")
        self._items = tuple(items)
        self._by_key = {
            (item.entry.name, item.entry.version): item for item in self._items
        }
        known_scopes = {
            scope for item in self._items for scope in item.entry.auth_scopes
        }
        self._policy = CapabilityPolicy(known_scopes=known_scopes)

    def _active(self) -> tuple[_CatalogItem, ...]:
        names = sorted({item.entry.name for item in self._items})
        active: list[_CatalogItem] = []
        for name in names:
            candidates = [item for item in self._items if item.entry.name == name]
            preferred = [item for item in candidates if not item.snapshot.deprecated]
            pool = preferred or candidates
            if pool:
                active.append(max(pool, key=lambda item: item.version_key))
        return tuple(active)

    @staticmethod
    def _admin_visibility(
        context: TenantContext | None,
        admin: bool,
    ) -> bool:
        """Whether this read may bypass per-capability visibility.

        True only when the caller both asked for an admin discovery read
        (``admin``) and presents a trusted context holding
        :data:`CATALOG_ADMIN_SCOPE`. The execution path never passes
        ``admin=True``, so this can only widen discovery, never execution.
        """
        if not admin:
            return False
        trusted = _validated_context(context)
        if trusted is None:
            return False
        return CATALOG_ADMIN_SCOPE in trusted.scopes

    def _visible_active(
        self,
        context: TenantContext | None,
        admin: bool = False,
    ) -> tuple[_CatalogItem, ...]:
        active = self._active()
        if self._admin_visibility(context, admin):
            return active
        return tuple(
            item
            for item in active
            if self._policy.decide(context, item.entry.auth_scopes).allowed
        )

    @staticmethod
    def _revision(items: tuple[_CatalogItem, ...]) -> str:
        payload = {
            "contract": "catalog-visible-v1",
            "items": [
                {
                    "name": item.entry.name,
                    "version": item.entry.version,
                    "schema_hash": item.entry.schema_hash,
                    "deprecated": item.snapshot.deprecated,
                }
                for item in sorted(
                    items,
                    key=lambda item: (item.entry.name, item.version_key),
                )
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def list(
        self,
        context: TenantContext | None,
        *,
        category: str | None = None,
        admin: bool = False,
    ) -> CatalogList:
        visible = self._visible_active(context, admin)
        selected = tuple(
            item.summary()
            for item in visible
            if item.entry.name not in _CATALOG_HIDDEN_NAMES
            and (category is None or item.snapshot.category == category)
        )
        return CatalogList(
            items=selected,
            total=len(selected),
            revision=self._revision(visible),
            hidden_count=len(self._active()) - len(visible),
        )

    def get(
        self,
        name: str,
        *,
        context: TenantContext | None,
        version: str | None = None,
        admin: bool = False,
    ) -> CatalogLookup:
        visible = self._visible_active(context, admin)
        revision = self._revision(visible)
        if version is None:
            item = next(
                (
                    candidate
                    for candidate in self._active()
                    if candidate.entry.name == name
                ),
                None,
            )
        else:
            item = self._by_key.get((name, version))
        if item is None:
            return CatalogLookup(
                exists=False,
                capability=None,
                entry=None,
                decision=None,
                revision=revision,
            )
        if self._admin_visibility(context, admin):
            decision = PolicyDecision(
                allowed=True,
                reason_code=PolicyReasonCode.ALLOWED,
                required_scopes=tuple(item.entry.auth_scopes),
                missing_scopes=(),
            )
        else:
            decision = self._policy.decide(context, item.entry.auth_scopes)
        if not decision.allowed:
            return CatalogLookup(
                exists=True,
                capability=None,
                entry=None,
                decision=decision,
                revision=revision,
            )
        return CatalogLookup(
            exists=True,
            capability=item.capability,
            entry=item.entry,
            decision=decision,
            revision=revision,
        )
