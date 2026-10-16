"""Owner 2026-10-17: relative times become absolute at extraction, through the model, checked here deterministically.

The model's reading (`time` on each extracted Claim) is injected by a fake provider; no real model is called. The anchor is
the cited message's acceptance time in the tenant timezone (Asia/Shanghai, +08:00)."""
from datetime import UTC, datetime
import hashlib

import pytest

from nexloop_eios.conversation_extraction import (DeterministicExtractionProvider, ExtractionContext, SourceMessage, SYSTEM_PROMPT,
                                                  build_user_payload, extract, resolve_time)
from nexloop_eios.time_normalization import merge
from support.tenants import TENANT_A

TZ = 'Asia/Shanghai'
WED = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)      # Wed 2026-10-14 10:00 local
SAT = datetime(2026, 10, 17, 2, 0, tzinfo=UTC)      # Sat 2026-10-17 10:00 local
MONTH_END = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)
YEAR_END = datetime(2026, 12, 30, 2, 0, tzinfo=UTC)
LATE_UTC = datetime(2026, 10, 16, 17, 30, tzinfo=UTC)  # Sat 2026-10-17 01:30 local, still Friday in UTC


def day(d):
    return f'{d}T00:00:00+08:00'


def run(expression, anchor, time):
    rule = resolve_time(expression, anchor, TZ)
    return rule, *merge(rule, time, expression=expression, anchor=anchor, timezone=TZ)


def source(flags):
    return next(f for f in flags if f.startswith('time_source:')).split(':', 1)[1]


def test_friday_deadline_the_rules_cannot_read_becomes_the_nearest_friday():
    rule, got, flags = run('周五前', WED, {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-17'), 'granularity': 'day',
                                        'timezone': TZ, 'assumptions': ['周五指本周五']})
    assert rule['status'] == 'unresolved' and source(flags) == 'llm'
    # A day-level deadline leaves inclusive/exclusive open, as the rules read one: ambiguous, its latest bound that Friday.
    assert got['kind'] == 'deadline' and got['status'] == 'ambiguous' and got['end'] == day('2026-10-17')
    assert got['latest_bound_window'] == [day('2026-10-16'), day('2026-10-17')] and got['start'] == '2026-10-14T10:00:00+08:00'
    assert 'time_llm_assumptions:1' in flags and 'time_llm:deadline:2026-10-14T10:00+08:00/2026-10-17T00:00+08:00' in flags
    # Another Friday than the nearest one is refused by name; the rules' reading (unresolved) stands.
    rule, got, flags = run('周五前', WED, {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-24'), 'granularity': 'day'})
    assert got == rule and flags == ['time_llm_refused:time_llm_weekday']


def test_friday_said_on_a_saturday_is_next_week_s_friday():
    _, got, flags = run('周五前', SAT, {'kind': 'deadline', 'start': '2026-10-17T10:00+08:00', 'end': day('2026-10-24'), 'granularity': 'day'})
    assert source(flags) == 'llm' and got['end'] == day('2026-10-24')
    _, got, flags = run('周五前', SAT, {'kind': 'deadline', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert flags == ['time_llm_refused:time_llm_anchor']  # the past Friday: before the anchor


def test_tomorrow_afternoon_agrees_with_the_rules_and_an_invented_hour_is_refused():
    rule, got, flags = run('明天下午', WED, {'kind': 'interval', 'start': '2026-10-15T12:00+08:00', 'end': '2026-10-15T18:00+08:00', 'granularity': 'part_of_day'})
    assert got == rule and rule['status'] == 'ambiguous' and source(flags) == 'llm+rule'
    rule, got, flags = run('明天下午', WED, {'kind': 'point', 'start': '2026-10-15T14:00+08:00', 'end': '2026-10-15T15:00+08:00', 'granularity': 'hour'})
    assert got == rule and flags == ['time_llm_refused:time_llm_invented_minute']


def test_next_wednesday_morning_read_as_this_week_is_a_conflict_with_both_readings_named():
    rule, got, flags = run('下周三上午', WED, {'kind': 'interval', 'start': '2026-10-14T06:00+08:00', 'end': '2026-10-14T12:00+08:00', 'granularity': 'part_of_day'})
    assert (rule['start'], rule['end']) == ('2026-10-21T06:00:00+08:00', '2026-10-21T12:00:00+08:00')
    # Neither is chosen silently: the rules' reading stays, ambiguous, and both readings are in the flags.
    assert got == rule | {'status': 'ambiguous'} and source(flags) == 'conflict'
    assert 'time_rule:interval:2026-10-21T06:00+08:00/2026-10-21T12:00+08:00' in flags
    assert 'time_llm:interval:2026-10-14T06:00+08:00/2026-10-14T12:00+08:00' in flags
    _, got, flags = run('下周三上午', WED, {'kind': 'interval', 'start': '2026-10-21T06:00+08:00', 'end': '2026-10-21T12:00+08:00', 'granularity': 'part_of_day'})
    assert source(flags) == 'llm+rule'


def test_end_of_month_across_the_month_and_three_days_later_across_the_year():
    rule, got, flags = run('月底前', MONTH_END, {'kind': 'deadline', 'start': '2026-10-30T10:00+08:00', 'end': day('2026-11-01'), 'granularity': 'period'})
    assert got == rule and rule['end'] == day('2026-11-01') and source(flags) == 'llm+rule'
    rule, got, flags = run('三天后', YEAR_END, {'kind': 'point', 'start': day('2027-01-02'), 'end': day('2027-01-03'), 'granularity': 'day'})
    assert got == rule and rule['status'] == 'resolved' and rule['start'] == day('2027-01-02') and source(flags) == 'llm+rule'


def test_in_a_couple_of_days_is_read_by_the_model_within_a_week_only():
    rule, got, flags = run('过两天', WED, {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert rule['status'] == 'unresolved' and got['status'] == 'resolved' and (got['start'], got['end']) == (day('2026-10-16'), day('2026-10-17'))
    assert source(flags) == 'llm' and got['kind'] == 'point' and got['anchor'] == rule['anchor'] and got['expression'] == '过两天'
    _, got, flags = run('过两天', WED, {'kind': 'point', 'start': day('2026-10-30'), 'end': day('2026-10-31'), 'granularity': 'day'})
    assert got['status'] == 'unresolved' and flags == ['time_llm_refused:time_llm_anchor']


def test_the_tenant_timezone_day_not_the_utc_day():
    """01:30 local on Saturday is still Friday in UTC: tomorrow is Sunday 10-18 here, never Saturday 10-17."""
    rule, got, flags = run('明天', LATE_UTC, {'kind': 'point', 'start': '2026-10-17T00:00Z', 'end': '2026-10-18T00:00Z', 'granularity': 'day'})
    assert rule['start'] == day('2026-10-18')
    # Its UTC midnights are not local day bounds: an invented time, refused.
    assert got == rule and flags == ['time_llm_refused:time_llm_invented_minute']
    rule, got, flags = run('明天', LATE_UTC, {'kind': 'point', 'start': day('2026-10-17'), 'end': day('2026-10-18'), 'granularity': 'day'})
    assert got == rule | {'status': 'ambiguous'} and source(flags) == 'conflict'
    _, _, flags = run('明天', LATE_UTC, {'kind': 'point', 'start': day('2026-10-18'), 'end': day('2026-10-19'), 'granularity': 'day', 'timezone': 'UTC'})
    assert flags == ['time_llm_refused:time_llm_timezone']


@pytest.mark.parametrize('time,code', [
    ({'kind': 'point', 'start': '2026-10-16', 'end': day('2026-10-17'), 'granularity': 'day'}, 'time_llm_iso'),
    ({'kind': 'point', 'start': '2026-10-16T00:00:00', 'end': day('2026-10-17'), 'granularity': 'day'}, 'time_llm_iso'),  # no offset
    ({'kind': 'point', 'start': day('2026-10-17'), 'end': day('2026-10-16'), 'granularity': 'day'}, 'time_llm_order'),
    ({'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day', 'note': 'x'}, 'time_llm_shape'),
    ({'kind': 'soon', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'}, 'time_llm_shape'),
    ({'kind': 'deadline', 'start': day('2026-10-14'), 'end': day('2026-10-17'), 'granularity': 'day'}, 'time_llm_deadline_mismatch'),
    ('2026-10-16', 'time_llm_shape'),
])
def test_an_illegal_reading_is_refused_by_name(time, code):
    rule, got, flags = run('过两天', WED, time)
    assert got == rule and flags == ['time_llm_refused:' + code]


def test_no_reading_or_no_expression():
    rule, got, flags = run('过两天', WED, None)
    assert got == rule and flags == []
    rule, got, flags = run('', WED, {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert got == rule and flags == ['time_llm_without_expression']


def test_extraction_carries_the_reading_into_the_claim_and_the_payload_carries_the_weekday():
    """End to end through extract(): the fake provider answers with a time reading; the Claim's valid_time is the checked absolute
    reading (what a Commitment's due date is computed from) and its guard flags say where it came from."""
    message = SourceMessage(message_id=hashlib.sha256(b'time-normalization:1').hexdigest(), sequence=1, speaker='consumer', body='我周五前能到店里', accepted_at=WED)
    context = ExtractionContext(tenant_id=TENANT_A, world='real', conversation_id='c' * 64, consumer_id='d' * 64, timezone=TZ)
    payload = build_user_payload([message], context)
    assert '"weekday":"周三"' in payload and '"time":"2026-10-14T10:00+08:00"' in payload
    assert 'ISO 8601' in SYSTEM_PROMPT and '绝不编造分钟' in SYSTEM_PROMPT
    response = {'topics': [{'topic': '到店', 'conversation_summary': '顾客周五前到店', 'user_valid_reply': True, 'message_refs': [1]}],
                'claims': [{'topic_index': 0, 'message_ref': 1, 'quote': '我周五前能到店里', 'kind': 'intent', 'explicit': True,
                            'subject': {'kind': 'consumer', 'text': ''}, 'predicate': '到店', 'value': {'type': 'boolean', 'value': True},
                            'polarity': 'affirmed', 'modality': 'asserted', 'condition': '', 'time_expression': '周五前',
                            'time': {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-17'), 'granularity': 'day',
                                     'timezone': TZ, 'assumptions': ['周五指本周五']},
                            'confidence': 0.8, 'derived_from': []}]}
    provider = DeterministicExtractionProvider({hashlib.sha256(payload.encode()).hexdigest(): response})
    [claim] = extract([message], context, provider).claims
    assert claim['time_expression'] == '周五前' and claim['valid_time']['end'] == day('2026-10-17') and claim['valid_time']['kind'] == 'deadline'
    assert 'time_source:llm' in claim['guard_flags'] and all(len(f) <= 80 for f in claim['guard_flags'])
