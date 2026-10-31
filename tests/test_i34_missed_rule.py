"""I34 (b): the evolution side counts a semantic read as missed when its statement fell back to the whole sentence (extraction
'fallback', I34 a) or its coverage is below 1; only extraction 'terms' with coverage 1 is covered. Records older than the extraction
column take L3's derived value (I34 c, {access_id, extraction} only); a recorded value is never overridden. Pure rules, no database."""
import json
import uuid

import pytest

from nexloop_eios.evolution.attribution import attribute, load_extraction, read_missed, with_extraction
from test_evolution_e4_pg import REF, OTHER_REF, TERM, access, sample
from test_evolution_traces_pg import hexid


def test_the_missed_rule():
    covered = {'kind': 'access', 'coverage': 1.0, 'extraction': 'terms'}
    assert read_missed(covered) is False
    assert read_missed({**covered, 'extraction': 'fallback'}) is True    # a fuzzy hit of the whole sentence (S6 dc7645a9)
    assert read_missed({**covered, 'coverage': 0.5}) is True             # a resolve that left a mention unresolved
    assert read_missed({**covered, 'coverage': 0}) is True               # a browse that found nothing
    assert read_missed({'kind': 'access', 'coverage': 1.0}) is False     # an old record without extraction: coverage alone
    assert read_missed({'kind': 'use', 'coverage': None, 'extraction': 'fallback'}) is False


def test_a_fallback_hit_is_a_retrieval_miss_and_a_term_hit_is_none():
    runs = [str(uuid.uuid4()) for _ in range(3)]
    samples = [sample(r, hexid('c' + r)) for r in runs]
    fallback = {**access(runs[0], refs=[REF]), 'coverage': 1.0, 'extraction': 'fallback'}
    terms = {**access(runs[1], refs=[REF]), 'coverage': 1.0, 'extraction': 'terms'}
    partial = {**access(runs[2], operation='resolve', terms=[TERM, hexid('x')], refs=[OTHER_REF], unresolved=1), 'coverage': 0.5, 'extraction': 'terms'}
    got = {a.run_id: a.category for a in attribute(samples, [fallback, terms, partial]).attributions}
    assert got == {runs[0]: 'retrieval_miss', runs[1]: 'none', runs[2]: 'retrieval_miss'}


def test_the_derived_file_fills_old_records_only():
    old, recorded, other = (str(uuid.uuid4()) for _ in range(3))
    derived = load_extraction(json.dumps([{'access_id': old, 'extraction': 'fallback'}, {'access_id': recorded, 'extraction': 'fallback'}]))
    assert load_extraction(json.dumps({'accesses': [{'access_id': old, 'extraction': 'fallback'}]})) == {old: 'fallback'}
    rows = [{'access_id': old, 'kind': 'access', 'coverage': 1.0}, {'access_id': recorded, 'kind': 'access', 'coverage': 1.0, 'extraction': 'terms'},
            {'access_id': other, 'kind': 'access', 'coverage': 1.0}]
    out = with_extraction(rows, derived)
    assert [r.get('extraction') for r in out] == ['fallback', 'terms', None] and [read_missed(r) for r in out] == [True, False, False]
    assert 'extraction' not in rows[0]  # the input is not changed


@pytest.mark.parametrize('bad', [
    {'rows': []}, [{'access_id': 'x', 'extraction': 'fallback'}], [{'access_id': str(uuid.uuid4()), 'extraction': 'maybe'}],
    [{'access_id': str(uuid.uuid4()), 'extraction': 'terms', 'text': '原句'}],
    [{'access_id': 'a6f0c0de-0000-4000-8000-000000000001', 'extraction': 'terms'}, {'access_id': 'a6f0c0de-0000-4000-8000-000000000001', 'extraction': 'fallback'}]])
def test_a_bad_derived_file_is_refused(bad):
    with pytest.raises(ValueError):
        load_extraction(json.dumps(bad))
