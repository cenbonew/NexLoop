# Rules derived from EvoOntology (https://github.com/ruc-datalab/EvoOntology, commit f64413dae88d88645b1f2c069cf4e17308ad0f89),
# evoontology/evolution/session.py (EvolutionSession: begin_round, record_round, extend_budget, _publication_gate,
# mark_incomplete; INCOMPLETE_REASONS, DEFAULT_MIN_REJECTS_BEFORE_INCOMPLETE). MIT License, Copyright (c) 2026 Meiduo Chong;
# see licenses/EvoOntology-MIT.txt. Rewritten for NexLoop (NX-038 E1; design §4.2, frozen-check §2.2): only the rules are kept.
# No workspace JSON, no SemanticStore, no validate(), and above all no publication: the upstream accept() publishes and switches
# the active version in one step; here acceptance ends in `accepted_for_release`, which is not a publication (08 §5, ADR-019).
# One upstream run of rounds becomes a chain of NexLoop candidates: a rejected round ends its candidate as `rejected` and the next
# round is a new candidate of the same Parent.
"""The evolution chain state machine (pure: values in, values out).

    running -- reject ----------------------> running (next candidate)
    running -- accept (gate passed) --------> accepted_for_release   (terminal; not published)
    running -- budget / external stop ------> incomplete             (terminal)

A reject, a missing hypothesis or one finished experiment never ends the chain. `incomplete` needs a legitimate external
reason; the judgment-based ones (missing_data, unreliable_evaluation, external_block) need at least `min_rejects` rejected
candidates first, and `budget_exhausted` only when the frozen round budget is spent. The budget is frozen at the start and can
only grow with a named confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from nexloop_eios.evolution.candidate import Candidate
from nexloop_eios.evolution.gate import EvaluationGate

RUNNING, ACCEPTED, INCOMPLETE = 'running', 'accepted_for_release', 'incomplete'
INCOMPLETE_REASONS = frozenset({'budget_exhausted', 'user_interrupted', 'missing_data', 'missing_permissions', 'unreliable_evaluation',
                                'external_block'})
JUDGMENT_REASONS = frozenset({'missing_data', 'unreliable_evaluation', 'external_block'})
DEFAULT_MIN_REJECTS = 2
PROTOCOLS = ('ground_truth', 'llm_judge')


class LifecycleError(RuntimeError):
    pass


class BudgetExhausted(LifecycleError):
    pass


@dataclass(frozen=True)
class Chain:
    tenant_id: str
    parent_revision: str
    max_rounds: int
    protocol: str
    non_inferiority_margin: float | None = None
    min_rejects: int = DEFAULT_MIN_REJECTS
    candidates: tuple[Candidate, ...] = ()
    status: str = RUNNING
    end_reason: str | None = None

    @classmethod
    def start(cls, *, tenant_id: str, parent_revision: str, max_rounds: int, protocol: str, non_inferiority_margin: float | None = None,
              min_rejects: int = DEFAULT_MIN_REJECTS) -> Chain:
        if not parent_revision:
            raise ValueError('parent_revision is required')
        if type(max_rounds) is not int or not 1 <= max_rounds <= 32:
            raise ValueError('max_rounds must be an integer from 1 to 32 (the contract range)')
        if protocol not in PROTOCOLS:
            raise ValueError('unknown acceptance protocol')
        if type(min_rejects) is not int or min_rejects < 1:
            raise ValueError('min_rejects must be a positive integer')
        return cls(tenant_id, parent_revision, max_rounds, protocol, non_inferiority_margin, min_rejects)

    @property
    def current(self) -> Candidate | None:
        return self.candidates[-1] if self.candidates else None

    def rejected_count(self) -> int:
        return sum(1 for c in self.candidates if c.status == 'rejected')

    def _running(self) -> None:
        if self.status != RUNNING:
            raise LifecycleError(f'the chain is {self.status}')

    def _replace_current(self, candidate: Candidate, **changes: Any) -> Chain:
        return replace(self, candidates=self.candidates[:-1] + (candidate,), **changes)

    def begin_round(self, candidate: Candidate) -> Chain:
        """Open the next round with one `proposed` candidate of this chain's Parent. When the frozen budget is spent the chain
        ends `incomplete/budget_exhausted` and BudgetExhausted is raised with that ended chain."""
        self._running()
        if self.current is not None and self.current.status not in ('rejected',):
            raise LifecycleError('the current candidate has not been rejected')
        if len(self.candidates) >= self.max_rounds:
            raise BudgetExhausted(replace(self, status=INCOMPLETE, end_reason='budget_exhausted'))
        if candidate.status != 'proposed' or candidate.tenant_id != self.tenant_id or candidate.parent_revision != self.parent_revision:
            raise LifecycleError('a round needs a proposed candidate of this tenant and Parent')
        if not candidate.primary_hypothesis.strip():
            raise ValueError('hypothesis is required')
        if candidate.maximum_rounds != self.max_rounds:
            raise LifecycleError('the candidate carries another round budget than the frozen one')
        return replace(self, candidates=self.candidates + (candidate,))

    def start_evaluation(self) -> Chain:
        self._running()
        if self.current is None:
            raise LifecycleError('no candidate to evaluate')
        return self._replace_current(self.current.moved_to('evaluating'))

    def reject(self) -> Chain:
        """A rejected candidate ends as `rejected`; the chain keeps running for the next round."""
        self._running()
        if self.current is None:
            raise LifecycleError('no candidate to reject')
        return self._replace_current(self.current.moved_to('rejected'))

    def gate(self, gate_input: dict[str, Any]) -> dict[str, Any]:
        """The frozen acceptance protocol over the recorded evaluation (upstream _publication_gate rules)."""
        if not isinstance(gate_input, dict):
            raise LifecycleError('record the candidate evaluation gate input first')
        if gate_input.get('unacceptable_regressions') is not False:
            raise LifecycleError('the evaluation must explicitly rule out unacceptable regressions')
        if gate_input.get('protocol') != self.protocol:
            raise LifecycleError('the gate protocol differs from the frozen acceptance protocol')
        if self.protocol == 'ground_truth':
            return EvaluationGate.decide_gt(gate_input.get('parent_scores'), gate_input.get('candidate_scores'),
                                            case_ids=gate_input.get('case_ids') or [], non_inferiority_margin=self.non_inferiority_margin)
        return EvaluationGate.decide_judge(gate_input.get('verdicts'))

    def accept(self, gate_input: dict[str, Any]) -> Chain:
        """`accepted_for_release` only when the gate accepts; the chain then ends. Nothing is published here."""
        self._running()
        if self.current is None or self.current.status != 'evaluating':
            raise LifecycleError('no candidate under evaluation to accept')
        if not self.gate(gate_input)['accept']:
            raise LifecycleError('the candidate did not pass the frozen evaluation gate')
        return self._replace_current(self.current.moved_to('accepted_for_release'), status=ACCEPTED)

    def mark_incomplete(self, reason: str) -> Chain:
        self._running()
        if reason not in INCOMPLETE_REASONS:
            raise ValueError(f'incomplete reason must be one of {sorted(INCOMPLETE_REASONS)}')
        if reason in JUDGMENT_REASONS and self.rejected_count() < self.min_rejects:
            raise LifecycleError(f'cannot stop for {reason!r} before {self.min_rejects} rejected candidates; design the next candidate')
        if reason == 'budget_exhausted' and len(self.candidates) < self.max_rounds:
            raise LifecycleError('cannot stop for budget_exhausted: the round budget is not spent')
        chain = replace(self, status=INCOMPLETE, end_reason=reason)
        if self.current is not None and self.current.status in ('proposed', 'evaluating'):
            chain = chain._replace_current(self.current.moved_to('incomplete'))
        return chain

    def extend_budget(self, max_rounds: int, *, confirmed_by: str) -> Chain:
        """Raise the frozen round budget only with a named confirmation (the owner's, D8/D20)."""
        self._running()
        if not confirmed_by:
            raise LifecycleError('a budget extension needs a named confirmation')
        if type(max_rounds) is not int or max_rounds <= self.max_rounds or max_rounds > 32:
            raise ValueError(f'extend_budget must increase max_rounds (current {self.max_rounds}, contract maximum 32)')
        return replace(self, max_rounds=max_rounds)
