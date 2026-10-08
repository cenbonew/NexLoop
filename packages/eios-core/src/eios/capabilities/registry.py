from __future__ import annotations

import hashlib
import json

from .models import CapabilitySearchResult, CapabilitySummary


def _version_key(
    version: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
    # Lazy import avoids the control.catalog -> capabilities package cycle while
    # keeping one authoritative SemVer implementation.
    from eios.control.catalog import capability_version_key

    return capability_version_key(version)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], object] = {}

    def register(self, capability: object) -> None:
        metadata = capability.metadata()
        _version_key(metadata.version)
        key = (metadata.name, metadata.version)
        if key in self._items:
            raise ValueError(
                f"duplicate capability registration: {metadata.name}@{metadata.version}"
            )
        self._items[key] = capability

    def get(self, name: str, version: str | None = None):
        if version is not None:
            return self._items.get((name, version))
        candidates = [
            item for (item_name, _), item in self._items.items() if item_name == name
        ]
        active = [item for item in candidates if not item.metadata().deprecated]
        pool = active or candidates
        return max(
            pool, key=lambda item: _version_key(item.metadata().version), default=None
        )

    def versions(self, name: str) -> list[CapabilitySummary]:
        items = [
            item.summary()
            for (item_name, _), item in self._items.items()
            if item_name == name
        ]
        return sorted(items, key=lambda item: _version_key(item.version), reverse=True)

    def active_capabilities(self) -> list[object]:
        names = sorted({name for name, _ in self._items})
        return [item for name in names if (item := self.get(name)) is not None]

    def registered_capabilities(self) -> list[object]:
        """Return every exactly-addressable capability version in stable order."""

        return sorted(
            self._items.values(),
            key=lambda item: (
                item.metadata().name,
                _version_key(item.metadata().version),
            ),
        )

    def list(self, *, category: str | None = None) -> list[CapabilitySummary]:
        results = []
        for item in self.active_capabilities():
            metadata = item.metadata()
            if category and metadata.category != category:
                continue
            results.append(item.summary())
        return results

    def search(self, query: str) -> list[CapabilitySearchResult]:
        terms = [term.lower() for term in str(query or "").split() if term]
        results: list[CapabilitySearchResult] = []
        for item in self.active_capabilities():
            metadata = item.metadata()
            fields = {
                "name": metadata.name,
                "title": metadata.title,
                "description": metadata.description,
                "category": metadata.category,
                "tags": " ".join(metadata.tags),
            }
            matched = tuple(
                name
                for name, value in fields.items()
                if any(term in value.lower() for term in terms)
            )
            if not matched:
                continue
            score = sum(2.0 if name in {"name", "tags"} else 1.0 for name in matched)
            results.append(
                CapabilitySearchResult(
                    capability=item.summary(), score=score, matched_fields=matched
                )
            )
        return sorted(results, key=lambda item: (-item.score, item.capability.name))

    def revision(self) -> str:
        payload = [
            item.metadata().model_dump(mode="json")
            for item in sorted(
                self._items.values(),
                key=lambda value: (
                    value.metadata().name,
                    _version_key(value.metadata().version),
                ),
            )
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
