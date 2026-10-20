# Ported from EvoOntology (https://github.com/ruc-datalab/EvoOntology, commit f64413dae88d88645b1f2c069cf4e17308ad0f89),
# evoontology/evaluation/evaluation.py. MIT License, Copyright (c) 2026 Meiduo Chong; see licenses/EvoOntology-MIT.txt.
# Modified for NexLoop (NX-038 E1, design §4.2 step 3 and §12.4): anonymization is reproducible (an explicit swap or a seeded
# random.Random, never the module-global generator), the ground-truth gate takes an optional frozen non-inferiority margin (D7),
# and inputs are checked as the upstream session's publication gate checks them (paired, finite, unique case ids).
"""Evaluation gate: decide whether a Candidate beats its Parent.

Two protocols, chosen by whether ground truth exists:

- **Ground truth**: absolute scores per case; without a margin the Candidate is accepted only when its average strictly exceeds
  the Parent's (upstream rule). With a frozen non-inferiority margin m (D7) it is accepted when delta >= -m and delta != 0:
  an equal score is never an acceptance.
- **No ground truth**: an anonymous judge compares Parent and Candidate per case; the Candidate is accepted only when it wins
  strictly more non-tie cases than the Parent and records zero critical errors.

This module owns the aggregation and the A/B anonymization helpers only; it does not score or call a judge model, and it never
publishes anything.
"""
from __future__ import annotations

import math
import random
from typing import Any


class GateInputError(ValueError):
    """The evaluation input cannot support a decision (upstream: EvolutionError in the publication gate)."""


def _scores(values: Any, label: str) -> list[float]:
    if not isinstance(values, list) or not values:
        raise GateInputError(f'{label} must be a non-empty list')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise GateInputError(f'{label} must be finite numbers')
    return [float(v) for v in values]


class EvaluationGate:
    # ---- ground truth ------------------------------------------------------

    @staticmethod
    def decide_gt(parent_scores: list[float], candidate_scores: list[float], *, case_ids: list[str] | None = None,
                  non_inferiority_margin: float | None = None) -> dict[str, Any]:
        parent, candidate = _scores(parent_scores, 'parent_scores'), _scores(candidate_scores, 'candidate_scores')
        if len(parent) != len(candidate):
            raise GateInputError('parent and candidate scores must be paired')
        if case_ids is not None and (len(case_ids) != len(parent) or len(set(case_ids)) != len(case_ids)):
            raise GateInputError('scores need unique matching case_ids')
        if non_inferiority_margin is not None and (isinstance(non_inferiority_margin, bool) or not isinstance(non_inferiority_margin, (int, float))
                                                   or not math.isfinite(non_inferiority_margin) or non_inferiority_margin < 0):
            raise GateInputError('non_inferiority_margin must be a finite number >= 0')
        parent_avg = sum(parent) / len(parent)
        candidate_avg = sum(candidate) / len(candidate)
        delta = candidate_avg - parent_avg
        if non_inferiority_margin is None:
            accept = candidate_avg > parent_avg
        else:
            accept = delta >= -non_inferiority_margin and delta != 0
        return {'accept': accept, 'parent_score': parent_avg, 'candidate_score': candidate_avg, 'delta': delta,
                'protocol': 'ground_truth', 'non_inferiority_margin': non_inferiority_margin}

    # ---- judge -------------------------------------------------------------

    @staticmethod
    def decide_judge(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate decoded verdicts: each {"winner": "candidate"|"parent"|"tie", "critical_error": bool}. A critical error of the
        Candidate is a hard reject."""
        if not isinstance(verdicts, list) or not verdicts or any(
                not isinstance(v, dict) or v.get('winner') not in {'parent', 'candidate', 'tie'} or not isinstance(v.get('critical_error'), bool)
                for v in verdicts):
            raise GateInputError('the gate needs non-empty valid judge verdicts')
        w_c = sum(1 for v in verdicts if v['winner'] == 'candidate')
        w_p = sum(1 for v in verdicts if v['winner'] == 'parent')
        ties = sum(1 for v in verdicts if v['winner'] == 'tie')
        e_c = sum(1 for v in verdicts if v['critical_error'])
        return {'accept': e_c == 0 and w_c > w_p, 'candidate_wins': w_c, 'parent_wins': w_p, 'ties': ties,
                'candidate_critical_errors': e_c, 'protocol': 'llm_judge'}

    # ---- anonymization -----------------------------------------------------

    @staticmethod
    def anonymize(parent_answer: Any, candidate_answer: Any, *, swap: bool | None = None, rng: random.Random | None = None) -> dict[str, Any]:
        """Label the two answers A/B so the judge never sees which is the Parent. Reproducible (M22): pass the swap explicitly or a
        seeded random.Random from the frozen evaluation bundle; the result carries `swap` so the report can record it."""
        if swap is None:
            if rng is None:
                raise GateInputError('anonymize needs an explicit swap or a seeded random.Random')
            swap = rng.random() < 0.5
        if swap:
            return {'A': candidate_answer, 'B': parent_answer, 'A_is': 'candidate', 'B_is': 'parent', 'swap': True}
        return {'A': parent_answer, 'B': candidate_answer, 'A_is': 'parent', 'B_is': 'candidate', 'swap': False}

    @staticmethod
    def decode(judgment: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
        """Turn an A/B judgment back into candidate/parent terms; `critical_error` may name a side or be a bare boolean."""
        winner = judgment.get('winner')
        decoded_winner = mapping.get(f'{winner}_is', winner) if winner in ('A', 'B') else 'tie'
        raw = judgment.get('critical_error')
        if raw in ('A', 'B'):
            critical = mapping.get(f'{raw}_is') == 'candidate'
        elif isinstance(raw, bool):
            critical = raw
        else:
            critical = False
        return {'winner': decoded_winner, 'critical_error': critical, 'reason': judgment.get('reason', '')}
