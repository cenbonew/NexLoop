"""Claim-matcher without an active merge configuration (dispatcher 2026-10-18): an explicit idle code, one rate-limited log
line, never a failing tick."""
import logging

import pytest

from nexloop_eios import claim_match_worker as W
from nexloop_eios.candidate_merge import CandidateGluer, MergeConfigurationMissing


class _Feed:
    def claim(self, **unused):return []


class _Gluer:
    def process_staged(self):raise MergeConfigurationMissing()


def _worker(gluer):
    worker = object.__new__(W.ClaimMatchWorker)
    worker.feed, worker.gluer, worker.batch, worker.lease_seconds = _Feed(), gluer, 10, 300
    return worker


def test_missing_configuration_is_idle_and_logged_once_per_interval(caplog, monkeypatch):
    monkeypatch.setattr(W, '_missing_logged_at', None)
    run = W.ClaimMatchWorker.run_once.__wrapped__ if hasattr(W.ClaimMatchWorker.run_once, '__wrapped__') else W.ClaimMatchWorker.run_once
    with caplog.at_level(logging.WARNING, logger=W.__name__):
        first, second = run(_worker(_Gluer())), run(_worker(_Gluer()))
    assert first['glue'] == second['glue'] == 'merge_configuration_missing' and first['glued'] == 0
    assert [r.getMessage().count('merge_configuration_missing') for r in caplog.records] == [1]
    monkeypatch.setattr(W, '_missing_logged_at', W._missing_logged_at - W.MISSING_LOG_SECONDS)
    with caplog.at_level(logging.WARNING, logger=W.__name__):
        run(_worker(_Gluer()))
    assert len(caplog.records) == 2


def test_gluer_raises_only_when_staged_candidates_wait():
    gluer = object.__new__(CandidateGluer)
    reads = {'candidates': [], 'configuration': None}
    gluer._read = lambda payload: reads[payload['verb']]
    gluer.repoint_merged = lambda: []
    assert gluer.process_staged() == []  # nothing staged: no configuration needed
    reads['candidates'] = [{'candidate_id': 'x'}]
    with pytest.raises(MergeConfigurationMissing) as error:
        gluer.process_staged()
    assert isinstance(error.value, LookupError) and error.value.code == 'merge_configuration_missing'
