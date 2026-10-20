# Ported from EvoOntology (https://github.com/ruc-datalab/EvoOntology, commit f64413dae88d88645b1f2c069cf4e17308ad0f89),
# evoontology/evolution/adapter.py. MIT License, Copyright (c) 2026 Meiduo Chong; see licenses/EvoOntology-MIT.txt.
# Modified for NexLoop (NX-038 E1, design §4.3): the subject is a candidate id (or the Parent revision), the cases are frozen
# case ids, and there is no output path hint: an adapter writes nothing to a caller-chosen location.
"""Minimal evaluation-adapter contract (the NX-039 EvaluationAdapter). The lifecycle consumes only the normalized result."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvaluationAdapter(Protocol):
    def evaluate(self, subject: str, cases: list[str]) -> dict[str, Any]:
        """Evaluate `subject` on the frozen `cases`. Returns at least {"metrics": {...}}; optionally "cases" (per-case results)
        and "artifact_refs" (local Artifact references, never file paths)."""
        ...


def normalize_result(result: Any) -> dict[str, Any]:
    """Validate an adapter result and fill the two optional list fields."""
    if not isinstance(result, dict):
        raise ValueError('adapter result must be a dict')
    if not isinstance(result.get('metrics'), dict):
        raise ValueError("adapter result must contain a 'metrics' dict")
    normalized = dict(result)
    for field in ('cases', 'artifact_refs'):
        value = normalized.get(field)
        if value is None:
            normalized[field] = []
        elif not isinstance(value, list):
            raise ValueError(f'adapter result {field!r} must be a list')
    return normalized
