"""NX-038 E4: the candidate store port (migration nx038_attribution_candidates; design §3.2, §12.2, §12.5).

Runs as the login role nexloop_evo_writer with a service credential read from a private file; every call is one security definer
function that derives the tenant from that credential, world real. The store writes diagnoses, candidates, patches (ids, hashes,
refs) and their event log only: no definition, alias, authorization fact, business object, Claim or Commitment, and there is no
publication or activation call (ADR-007, D18). Budget: ``reserve`` before each model call (three tiers, D20); a spent tier ends the
diagnosis or candidate `incomplete/budget_exhausted` and returns ``reserved: False``.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from nexloop_eios.evolution.attribution import CandidateDraft, Diagnosis
from nexloop_eios.evolution.candidate import Candidate

WRITER_ROLE = 'nexloop_evo_writer'


class StoreUnavailable(RuntimeError):
    pass


def diagnosis_id(digest: str) -> str:
    """One diagnosis per frozen snapshot: the same inputs give the same id (and replay)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'nexloop:evolution:diagnosis:' + digest))


class CandidateStore:
    def __init__(self, connect, credential_file):
        from nexloop_eios.private_configuration import read_private_text
        self.connect = connect
        self._digest = lambda: hashlib.sha256(read_private_text(credential_file, maximum=16384).strip().encode()).hexdigest()

    def _call(self, sql: str, params: tuple) -> Any:
        with self.connect() as db:
            if db.execute('select current_user').fetchone()[0] != WRITER_ROLE:
                raise StoreUnavailable('the evolution writer role is required')
            return db.execute(sql, (self._digest(), *params)).fetchone()[0]

    def open_diagnosis(self, diagnosis: Diagnosis, samples: list[dict], *, parent_revision: str, evaluator_version: str,
                       model_profile: str) -> dict:
        runs = sorted({s['run_id'] for s in samples if s.get('run_id') and not s.get('test_stack')})
        consumers = sorted({s['consumer_id'] for s in samples if s.get('consumer_id') and not s.get('test_stack')})
        body = {'diagnosis_id': diagnosis_id(diagnosis.snapshot_digest), 'input_snapshot_digest': diagnosis.snapshot_digest,
                'parent_revision': parent_revision, 'evaluator_version': evaluator_version, 'model_profile': model_profile,
                'run_ids': runs, 'consumer_ids': consumers, 'attribution': {**diagnosis.counts, **{'marked_' + k: v for k, v in diagnosis.marked.items()}}}
        return self._call('select authz.nexloop_evo_diagnosis_open(%s,%s)', (Jsonb(body),))

    def propose(self, diagnosis: str, draft: CandidateDraft) -> dict:
        body = {'diagnosis_id': diagnosis, 'candidate': draft.candidate, 'category': draft.category, 'patch': draft.patch,
                'case_groups': draft.case_groups, 'regroups': draft.regroups, 'source_consumer_ids': draft.source_consumer_ids}
        out = self._call('select authz.nexloop_evo_candidate_propose(%s,%s)', (Jsonb(body),))
        if out.get('candidate'):
            out['candidate'] = Candidate.from_contract(out['candidate']).to_contract()  # the stored value is the contract value
        return out

    def close(self, diagnosis: str) -> dict:
        return self._call('select authz.nexloop_evo_diagnosis_close(%s,%s)', (diagnosis,))

    def read(self, candidate_id: str) -> dict | None:
        return self._call('select authz.nexloop_evo_candidate_read(%s,%s)', (candidate_id,))

    def stop(self, candidate_id: str, reason: str) -> dict:
        return self._call('select authz.nexloop_evo_candidate_stop(%s,%s,%s)', (candidate_id, reason))

    def reserve(self, scope: str, record_id: str, consumption_id: str, amount: str, unit: str) -> dict:
        return self._call('select authz.nexloop_evo_reserve(%s,%s,%s,%s,%s,%s)', (scope, record_id, consumption_id, amount, unit))

    # --- E5 hand-over to NX-039 (design §12.4; migration nx038_isolation_handoff) -------------------------------------------------
    def freeze_bundle(self, candidate_id: str, *, levels: tuple[str, ...] = ('L0',), seed: str) -> dict:
        """Freezes the evaluation bundle of a proposed candidate; SQL builds it from the stored candidate, the fixed hard gates and
        the server-side budget remaining. The same call replays; a changed condition is refused (a new candidate)."""
        return self._call('select authz.nexloop_evo_bundle_freeze(%s,%s,%s,%s)', (candidate_id, list(levels), seed))

    def record_report(self, report: dict) -> dict:
        return self._call('select authz.nexloop_evo_report_record(%s,%s)', (Jsonb(report),))

    def link_review(self, decision_id: str, candidate_id: str, report_digest: str) -> dict:
        """D4: ties an S3 review decision to the candidate and the report digest it relied on."""
        return self._call('select authz.nexloop_evo_review_link(%s,%s,%s,%s)', (decision_id, candidate_id, report_digest))


EVALUATOR_ROLE = 'nexloop_evo_evaluator'
OPEN_REFUSALS = ('bundle_mismatch', 'candidate_not_ready', 'leakage_detected', 'holdout_exhausted', 'dimension_closed', 'dimension_stopped',
                 'budget_exhausted')


class EvaluatorStore:
    """NX-039 V1 (design §2.2, §5.1; D39-1): the evaluator's port. Login role nexloop_evo_evaluator with a service credential whose
    principal is registered as an evaluator (never also a writer: one purpose per principal, enforced in SQL). ``open`` runs the
    seven checks of §2.2 in order; a refusal returns ``opened: False`` and one of OPEN_REFUSALS and changes nothing; an opening turns
    the candidate proposed -> evaluating and records one use of its holdout (at most 5 candidates per holdout, D39-3)."""
    def __init__(self, connect, credential_file):
        from nexloop_eios.private_configuration import read_private_text
        self.connect = connect
        self._digest = lambda: hashlib.sha256(read_private_text(credential_file, maximum=16384).strip().encode()).hexdigest()

    def _call(self, sql: str, params: tuple) -> Any:
        with self.connect() as db:
            if db.execute('select current_user').fetchone()[0] != EVALUATOR_ROLE:
                raise StoreUnavailable('the evolution evaluator role is required')
            return db.execute(sql, (self._digest(), *params)).fetchone()[0]

    def open(self, candidate_id: str, bundle_digest: str) -> dict:
        return self._call('select authz.nexloop_evo_evaluation_open(%s,%s,%s)', (candidate_id, bundle_digest))
