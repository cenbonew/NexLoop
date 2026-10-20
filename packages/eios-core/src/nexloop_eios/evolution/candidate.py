"""NX-038 E1: one evolution candidate as the `evolution-candidate` contract describes it (packages/contracts, unchanged).

`from_contract` validates the wire value with the generated model (nexloop_eios.contracts.EvolutionCandidate, the exact
canonical schema) before building the record; `to_contract` returns the same wire value. A candidate is an isolated artifact:
it holds ids, digests, a hypothesis and a patch reference, never a business object, and `publication_permit_ref` is always null
(publication is NX-039 and human review, never this record).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from nexloop_eios.contracts import EvolutionCandidate

STATUSES = ('proposed', 'evaluating', 'rejected', 'accepted_for_release', 'incomplete')
TERMINAL = frozenset({'rejected', 'accepted_for_release', 'incomplete'})
# The only transitions a candidate may take (08 §5): diagnosis happens inside `proposed`.
TRANSITIONS = {'proposed': frozenset({'evaluating', 'incomplete'}),
               'evaluating': frozenset({'rejected', 'accepted_for_release', 'incomplete'})}
_LISTS = ('design_case_ids', 'validation_case_ids', 'allowed_change_scope')


class TransitionRefused(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    schema_version: str
    candidate_id: str
    tenant_id: str
    parent_revision: str
    candidate_revision: str
    dimension: str
    primary_hypothesis: str
    patch_artifact_ref: str
    input_snapshot_digest: str
    design_case_ids: tuple[str, ...]
    validation_case_ids: tuple[str, ...]
    evaluator_version: str
    model_profile: str
    maximum_rounds: int
    allowed_change_scope: tuple[str, ...]
    status: str
    created_at: str
    publication_permit_ref: None = None

    @classmethod
    def from_contract(cls, value: dict[str, Any]) -> Candidate:
        wire = EvolutionCandidate.model_validate(value).model_dump(mode='json')
        return cls(**{k: tuple(v) if k in _LISTS else v for k, v in wire.items()})

    def to_contract(self) -> dict[str, Any]:
        wire = {k: list(getattr(self, k)) if k in _LISTS else getattr(self, k) for k in EvolutionCandidate.model_fields}
        return EvolutionCandidate.model_validate(wire).model_dump(mode='json')

    def moved_to(self, status: str) -> Candidate:
        if status not in TRANSITIONS.get(self.status, frozenset()):
            raise TransitionRefused(f'{self.status} -> {status} is not a candidate transition')
        return replace(self, status=status)
