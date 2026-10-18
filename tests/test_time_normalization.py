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
    assert len(flags) == 1 and flags[0].startswith('time_source:')  # the flags carry the source only
    return flags[0].split(':', 1)[1]


def plain(valid_time):
    """valid_time without its normalization record (what the rules alone would read)."""
    return {k: v for k, v in valid_time.items() if k != 'normalization'}


def refused(got, rule, flags, code):
    assert plain(got) == rule and source(flags) == 'rule' and got['normalization']['verdict'] == code
    return True


def test_friday_deadline_the_rules_cannot_read_becomes_the_nearest_friday():
    rule, got, flags = run('周五前', WED, {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-17'), 'granularity': 'day',
                                        'timezone': TZ, 'assumptions': ['周五指本周五']})
    assert rule['status'] == 'unresolved' and source(flags) == 'llm'
    # A day-level deadline leaves inclusive/exclusive open, as the rules read one: ambiguous, its latest bound that Friday.
    assert got['kind'] == 'deadline' and got['status'] == 'ambiguous' and got['end'] == day('2026-10-17')
    assert got['latest_bound_window'] == [day('2026-10-16'), day('2026-10-17')] and got['start'] == '2026-10-14T10:00:00+08:00'
    n = got['normalization']
    assert n['verdict'] == 'accepted' and n['rule'] is None and n['assumptions'] == ['周五指本周五']
    assert n['llm'] == {'kind': 'deadline', 'start': '2026-10-14T10:00:00+08:00', 'end': day('2026-10-17'), 'granularity': 'day'}
    # Another Friday than the nearest one is refused by name; the rules' reading (unresolved) stands.
    rule, got, flags = run('周五前', WED, {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-24'), 'granularity': 'day'})
    assert refused(got, rule, flags, 'time_llm_weekday') and got['normalization']['llm']['end'] == day('2026-10-24')  # kept for audit


def test_friday_said_on_a_saturday_is_next_week_s_friday():
    _, got, flags = run('周五前', SAT, {'kind': 'deadline', 'start': '2026-10-17T10:00+08:00', 'end': day('2026-10-24'), 'granularity': 'day'})
    assert source(flags) == 'llm' and got['end'] == day('2026-10-24')
    _, got, flags = run('周五前', SAT, {'kind': 'deadline', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert got['normalization']['verdict'] == 'time_llm_anchor'  # the past Friday: before the anchor


def test_tomorrow_afternoon_agrees_with_the_rules_and_an_invented_hour_is_refused():
    rule, got, flags = run('明天下午', WED, {'kind': 'interval', 'start': '2026-10-15T12:00+08:00', 'end': '2026-10-15T18:00+08:00', 'granularity': 'part_of_day'})
    assert plain(got) == rule and rule['status'] == 'ambiguous' and source(flags) == 'llm+rule' and got['normalization']['verdict'] == 'agreed'
    rule, got, flags = run('明天下午', WED, {'kind': 'point', 'start': '2026-10-15T14:00+08:00', 'end': '2026-10-15T15:00+08:00', 'granularity': 'hour'})
    assert refused(got, rule, flags, 'time_llm_invented_minute')


def test_next_wednesday_morning_read_as_this_week_is_a_conflict_with_both_readings_named():
    rule, got, flags = run('下周三上午', WED, {'kind': 'interval', 'start': '2026-10-14T06:00+08:00', 'end': '2026-10-14T12:00+08:00', 'granularity': 'part_of_day'})
    assert (rule['start'], rule['end']) == ('2026-10-21T06:00:00+08:00', '2026-10-21T12:00:00+08:00')
    # Neither is chosen silently: the rules' reading stays, ambiguous, and both readings are in the flags.
    assert plain(got) == rule | {'status': 'ambiguous'} and source(flags) == 'conflict'
    n = got['normalization']
    assert n['verdict'] == 'conflict' and (n['rule']['start'], n['llm']['start']) == ('2026-10-21T06:00:00+08:00', '2026-10-14T06:00:00+08:00')
    _, got, flags = run('下周三上午', WED, {'kind': 'interval', 'start': '2026-10-21T06:00+08:00', 'end': '2026-10-21T12:00+08:00', 'granularity': 'part_of_day'})
    assert source(flags) == 'llm+rule'


def test_end_of_month_across_the_month_and_three_days_later_across_the_year():
    rule, got, flags = run('月底前', MONTH_END, {'kind': 'deadline', 'start': '2026-10-30T10:00+08:00', 'end': day('2026-11-01'), 'granularity': 'period'})
    assert plain(got) == rule and rule['end'] == day('2026-11-01') and source(flags) == 'llm+rule'
    rule, got, flags = run('三天后', YEAR_END, {'kind': 'point', 'start': day('2027-01-02'), 'end': day('2027-01-03'), 'granularity': 'day'})
    assert plain(got) == rule and rule['status'] == 'resolved' and rule['start'] == day('2027-01-02') and source(flags) == 'llm+rule'


def test_in_a_couple_of_days_is_read_by_the_model_within_a_week_only():
    rule, got, flags = run('过两天', WED, {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert rule['status'] == 'unresolved' and got['status'] == 'resolved' and (got['start'], got['end']) == (day('2026-10-16'), day('2026-10-17'))
    assert source(flags) == 'llm' and got['kind'] == 'point' and got['anchor'] == rule['anchor'] and got['expression'] == '过两天'
    _, got, flags = run('过两天', WED, {'kind': 'point', 'start': day('2026-10-30'), 'end': day('2026-10-31'), 'granularity': 'day'})
    assert got['status'] == 'unresolved' and refused(got, rule, flags, 'time_llm_anchor')


def test_the_tenant_timezone_day_not_the_utc_day():
    """01:30 local on Saturday is still Friday in UTC: tomorrow is Sunday 10-18 here, never Saturday 10-17."""
    rule, got, flags = run('明天', LATE_UTC, {'kind': 'point', 'start': '2026-10-17T00:00Z', 'end': '2026-10-18T00:00Z', 'granularity': 'day'})
    assert rule['start'] == day('2026-10-18')
    # Its UTC midnights are not local day bounds: an invented time, refused.
    assert refused(got, rule, flags, 'time_llm_invented_minute')
    rule, got, flags = run('明天', LATE_UTC, {'kind': 'point', 'start': day('2026-10-17'), 'end': day('2026-10-18'), 'granularity': 'day'})
    assert plain(got) == rule | {'status': 'ambiguous'} and source(flags) == 'conflict'
    rule, got, flags = run('明天', LATE_UTC, {'kind': 'point', 'start': day('2026-10-18'), 'end': day('2026-10-19'), 'granularity': 'day', 'timezone': 'UTC'})
    assert refused(got, rule, flags, 'time_llm_timezone')


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
    assert refused(got, rule, flags, code)


def test_no_reading_or_no_expression():
    rule, got, flags = run('过两天', WED, None)
    assert got == rule and flags == []
    rule, got, flags = run('', WED, {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert refused(got, rule, flags, 'time_llm_without_expression')


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
    assert 'time_source:llm' in claim['guard_flags'] and not any(f.startswith('time_llm') for f in claim['guard_flags'])
    assert claim['valid_time']['normalization']['source'] == 'llm' and claim['valid_time']['normalization']['assumptions'] == ['周五指本周五']
    # The Claim (with its normalization) conforms to claim.schema.json.
    from test_claim_contract import EXAMPLE, valid
    assert valid(dict(EXAMPLE, time_expression=claim['time_expression'], valid_time=claim['valid_time'], guard_flags=claim['guard_flags']))


# PR #5 review (P1): the span must be the one the kind and granularity say (an interval's end becomes a commitment's due time).
from nexloop_eios.time_normalization import DEADLINE, Refused, check  # noqa: E402


def checked(expression, time, anchor=WED):
    return check(time, expression=expression, anchor=anchor, timezone=TZ)


@pytest.mark.parametrize('expression,time', [
    ('周五', {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'}),
    ('周五下午3点', {'kind': 'point', 'start': '2026-10-16T15:00+08:00', 'end': '2026-10-16T16:00+08:00', 'granularity': 'hour'}),
    ('周五下午3点20', {'kind': 'point', 'start': '2026-10-16T15:20+08:00', 'end': '2026-10-16T15:21+08:00', 'granularity': 'minute'}),
    ('明天下午', {'kind': 'interval', 'start': '2026-10-15T12:00+08:00', 'end': '2026-10-15T18:00+08:00', 'granularity': 'part_of_day'}),
    ('周五到下周一', {'kind': 'interval', 'start': day('2026-10-16'), 'end': day('2026-10-20'), 'granularity': 'day'}),
    ('下个月', {'kind': 'point', 'start': day('2026-11-01'), 'end': day('2026-12-01'), 'granularity': 'month'}),
    ('周五前', {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-17'), 'granularity': 'day'}),
])
def test_a_span_that_matches_its_kind_and_granularity_is_accepted(expression, time):
    assert checked(expression, time)[0] == time['kind']


@pytest.mark.parametrize('expression,time', [
    # 周三说“周五”: a point day stretched to two weeks later.
    ('周五', {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-30'), 'granularity': 'day'}),
    ('周五', {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-16'), 'granularity': 'day'}),  # zero length
    ('周五下午3点', {'kind': 'point', 'start': '2026-10-16T15:00+08:00', 'end': '2026-10-16T17:00+08:00', 'granularity': 'hour'}),
    ('周五下午3点', {'kind': 'point', 'start': '2026-10-16T15:30+08:00', 'end': '2026-10-16T16:30+08:00', 'granularity': 'hour'}),
    ('周五下午3点20', {'kind': 'point', 'start': '2026-10-16T15:20+08:00', 'end': '2026-10-16T15:50+08:00', 'granularity': 'minute'}),
    ('明天下午', {'kind': 'point', 'start': '2026-10-15T12:00+08:00', 'end': '2026-10-16T18:00+08:00', 'granularity': 'part_of_day'}),
    ('明天下午', {'kind': 'interval', 'start': '2026-10-15T12:00+08:00', 'end': '2026-10-15T12:00+08:00', 'granularity': 'part_of_day'}),
    ('周五到下周一', {'kind': 'interval', 'start': day('2026-10-16'), 'end': day('2026-10-16'), 'granularity': 'day'}),  # empty interval
    ('周五到下周一3点', {'kind': 'interval', 'start': day('2026-10-16'), 'end': '2026-10-19T03:30+08:00', 'granularity': 'day'}),  # off the day boundary
    ('下个月', {'kind': 'point', 'start': day('2026-11-01'), 'end': day('2026-11-30'), 'granularity': 'month'}),
    ('下个月', {'kind': 'interval', 'start': day('2026-11-02'), 'end': day('2026-12-01'), 'granularity': 'month'}),
    ('周五前', {'kind': 'deadline', 'start': day('2026-10-17'), 'end': day('2026-10-17'), 'granularity': 'day'}),  # empty deadline
])
def test_a_span_that_disagrees_with_its_kind_or_granularity_is_refused(expression, time):
    with pytest.raises(Refused) as refusal:
        checked(expression, time)
    assert refusal.value.code == 'time_llm_span'


def test_the_wrong_span_is_refused_through_merge_and_the_rule_reading_stands():
    rule, got, flags = run('周五', WED, {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-30'), 'granularity': 'day'})
    assert refused(got, rule, flags, 'time_llm_span')


# PR #5 review (P2): a leading 截止/截止到 makes a deadline too; a non-time use does not.
@pytest.mark.parametrize('expression', ['周五前', '周五之前', '周五截止', '截止周五', '截止到周五', '截至下周一', '截止到月底', '截止10号', '最晚周五'])
def test_deadline_words_before_or_after_the_time(expression):
    assert DEADLINE.search(expression)


@pytest.mark.parametrize('expression', ['截止目前', '截至现在', '截止日期', '截止时间', '周五', '周五下午', '目前'])
def test_non_time_or_non_deadline_uses_are_not_deadlines(expression):
    assert not DEADLINE.search(expression)


def test_a_leading_deadline_word_is_accepted_as_a_deadline():
    time = {'kind': 'deadline', 'start': '2026-10-14T10:00+08:00', 'end': day('2026-10-17'), 'granularity': 'day'}
    for expression in ('截止周五', '截止到周五'):
        assert checked(expression, time)[0] == 'deadline'
    with pytest.raises(Refused) as refusal:
        checked('截止周五', {'kind': 'point', 'start': day('2026-10-16'), 'end': day('2026-10-17'), 'granularity': 'day'})
    assert refusal.value.code == 'time_llm_deadline_mismatch'
