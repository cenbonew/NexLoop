"""NX-061 B5: offline evidence judgement must not turn malformed numbers into a pass.
Synthetic snapshots only; no PG, transport, credentials or runtime target reads.
"""
import pytest

from nexloop_eios.run_outcome_taxonomy import failure_rate_gate


def snapshot(decided=10, rate=0.2):
    return {'outcomes': {'decided': decided, 'failure_rate_gated': rate}}


@pytest.mark.parametrize('decided', [float('nan'), float('inf'), float('-inf'), -1, 10.5])
def test_nonfinite_negative_or_fractional_decided_is_not_measured(decided):
    assert failure_rate_gate(snapshot(decided=decided)) == 'no_data'


@pytest.mark.parametrize('rate', [float('nan'), float('inf'), float('-inf'), -0.1, 1.1])
def test_nonfinite_or_out_of_range_rate_is_not_measured(rate):
    assert failure_rate_gate(snapshot(rate=rate)) == 'no_data'


@pytest.mark.parametrize(('rate', 'expected'), [(0, 'within'), (0.2, 'within'), (0.200001, 'warning'), (1, 'warning')])
def test_valid_rate_boundaries_keep_the_existing_owner_threshold(rate, expected):
    assert failure_rate_gate(snapshot(decided=10.0, rate=rate)) == expected


def test_quiet_hour_and_legacy_missing_gated_field_remain_unjudged():
    assert failure_rate_gate(snapshot(decided=9, rate=None)) == 'insufficient_data'
    assert failure_rate_gate({'outcomes': {'decided': 12, 'failure_rate': 0.5}}) == 'insufficient_data'
