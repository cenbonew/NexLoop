"""Owner 2026-10-17: relative times ("周五前", "过两天") become absolute times at extraction, through the model.

The extraction model proposes, per Claim, an absolute reading of its time expression (`time`: kind, start, end, granularity,
timezone, assumptions), anchored on the cited message's acceptance time in the tenant timezone. The proposal is untrusted:
it is checked here deterministically and merged with the rule reading (conversation_extraction.resolve_time):

- the same reading as the rules: the rules' valid_time stands, source llm+rule;
- the rules cannot read it and the proposal passes every check: the proposal becomes the valid_time, source llm;
- both read it differently: neither is chosen; the rules' reading is kept with status ambiguous and both readings are named;
- a proposal that fails a check is refused by name and the rules' reading stands.

The audit goes into valid_time.normalization (claim.schema.json, owner-approved 2026-10-17): source, verdict (accepted, agreed,
conflict or the refusal code), both readings and the model's stated assumptions; guard_flags carry time_source only. Minutes are never invented: an expression without a
clock time is read at most to the hour range of a part of day, and a plain day is the whole day [00:00, next 00:00).
"""
from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

KINDS = ('point', 'interval', 'deadline')
GRANULARITIES = ('minute', 'hour', 'day', 'part_of_day', 'period', 'month')
KEYS = {'kind', 'start', 'end', 'granularity', 'timezone', 'assumptions'}
ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})')
CLOCK = re.compile(r'(\d{1,2}|[一二两三四五六七八九十]{1,3})(点|:|：)')
# A deadline word at the end ("周五前", "周五截止") or a leading 截止/截至(到) before a time word ("截止周五", "截止到月底"); not
# "截止目前/截至现在" (so far) or "截止日期/截止时间" (a noun).
DEADLINE = re.compile(r'(?<!目)(前|之前|以前)$|(截止|为止|最晚|最迟)$|^(最晚|最迟)|^截[止至]到?(?!目前|现在|日期|时间)(?=[周星礼下本这明后今月\d一二三四五六七八九十])')
PAST = re.compile(r'(前天|昨天|昨日|上周|上星期|上个月|去年|刚才|之前那|[零一二两三四五六七八九十\d]{1,3}(个)?(天|日|周|星期|礼拜|月|年)(前|之前|以前)$)')
BARE_WEEKDAY = re.compile(r'(?<![下本这上每])(周|星期|礼拜)([一二三四五六日天])')
WEEKDAY = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
SOON = re.compile(r'过两天|过几天')
HORIZON = timedelta(days=400)


class Refused(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _instant(value, code):
    if type(value) is not str or not ISO.fullmatch(value):
        raise Refused(code)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise Refused(code) from None
    if parsed.tzinfo is None:
        raise Refused(code)
    return parsed


def _midnight(t):
    return (t.hour, t.minute, t.second, t.microsecond) == (0, 0, 0, 0)


def _next_day(t):
    return datetime.combine(t.date() + timedelta(days=1), time(), tzinfo=t.tzinfo)


def _next_month(t):
    return datetime(t.year + (t.month == 12), t.month % 12 + 1, 1, tzinfo=t.tzinfo)


def _on(granularity, t):
    """t lies on a boundary of the granularity (wall clock in the tenant zone)."""
    if granularity == 'day':
        return _midnight(t)
    if granularity == 'month':
        return _midnight(t) and t.day == 1
    if granularity == 'hour':
        return (t.minute, t.second, t.microsecond) == (0, 0, 0)
    if granularity == 'minute':
        return (t.second, t.microsecond) == (0, 0)
    return True


def _span(kind, granularity, start, end):
    """PR #5 review (P1): the span must be the one the kind and granularity say. An interval's end is read downstream as a
    commitment's due time, so a point is exactly one unit (a day [00:00, next 00:00), an hour, a minute, a month; a part of day
    at most one day), an interval is non-empty and on its granularity's boundaries, and a deadline is non-empty and ends on one."""
    if end <= start:
        raise Refused('time_llm_span')
    if kind == 'deadline':
        ok = _on(granularity, end)
    elif kind == 'point':
        ok = {'day': _midnight(start) and end == _next_day(start),
              'month': _on('month', start) and end == _next_month(start),
              'hour': _on('hour', start) and end == start + timedelta(hours=1),
              'minute': _on('minute', start) and end == start + timedelta(minutes=1),
              'part_of_day': end <= _next_day(start) and end - start <= timedelta(days=1),
              'period': end - start <= timedelta(days=31)}[granularity]
    else:
        ok = _on(granularity, start) and _on(granularity, end) and (granularity != 'part_of_day' or end - start <= timedelta(days=1))
    if not ok:
        raise Refused('time_llm_span')


def check(proposed, *, expression, anchor, timezone):
    """The model's reading, checked; returns (kind, start, end, granularity, assumptions) in the tenant zone or raises Refused."""
    zone = ZoneInfo(timezone)
    if type(proposed) is not dict or not {'kind', 'start', 'end', 'granularity'} <= set(proposed) or not set(proposed) <= KEYS:
        raise Refused('time_llm_shape')
    kind, granularity = proposed['kind'], proposed['granularity']
    if kind not in KINDS or granularity not in GRANULARITIES:
        raise Refused('time_llm_shape')
    if proposed.get('timezone') not in (None, timezone):
        raise Refused('time_llm_timezone')
    assumptions = proposed.get('assumptions', [])
    if type(assumptions) is not list or len(assumptions) > 8 or any(type(a) is not str or len(a) > 200 for a in assumptions):
        raise Refused('time_llm_shape')
    start = _instant(proposed['start'], 'time_llm_iso').astimezone(zone)
    end = _instant(proposed['end'], 'time_llm_iso').astimezone(zone)
    if start > end:
        raise Refused('time_llm_order')
    local = anchor.astimezone(zone)
    # No invented minutes: without a clock time in the expression, day or part-of-day bounds only.
    if not CLOCK.search(expression):
        if granularity in ('minute', 'hour'):
            raise Refused('time_llm_invented_minute')
        if granularity in ('day', 'period', 'month') and any((t.hour, t.minute, t.second, t.microsecond) != (0, 0, 0, 0) for t in (end,) + (() if kind == 'deadline' else (start,))):
            raise Refused('time_llm_invented_minute')
        if granularity == 'part_of_day' and any((t.minute, t.second, t.microsecond) != (0, 0, 0) for t in (start, end)):
            raise Refused('time_llm_invented_minute')
    # "前/之前" is a deadline, and only such words make one.
    if bool(DEADLINE.search(expression)) != (kind == 'deadline'):
        raise Refused('time_llm_deadline_mismatch')
    _span(kind, granularity, start, end)
    # Relation to the anchor: a future expression ends after the anchor and stays within the horizon.
    if not PAST.search(expression):
        if end <= local or start < datetime(local.year, local.month, local.day, tzinfo=zone) or end > local + HORIZON:
            raise Refused('time_llm_anchor')
    day = (end - timedelta(microseconds=1)).date() if kind == 'deadline' else start.date()
    bare = BARE_WEEKDAY.search(expression)
    if bare:
        # "周五" with no 下/本/这: the nearest such weekday on or after the anchor day.
        offset = (WEEKDAY[bare.group(2)] - local.weekday()) % 7
        if day != local.date() + timedelta(days=offset):
            raise Refused('time_llm_weekday')
    if SOON.search(expression) and not local.date() < day <= local.date() + timedelta(days=7):
        raise Refused('time_llm_anchor')
    return kind, start, end, granularity, assumptions


def _reading(valid_time, zone):
    """(kind, start, end) of a rule reading in the tenant zone."""
    start = datetime.fromisoformat(valid_time['start']).astimezone(zone)
    end = datetime.fromisoformat(valid_time['end']).astimezone(zone)
    return valid_time['kind'], start, end


def _iso(value):
    return value.isoformat()


def _proposal_reading(proposed, timezone):
    """The model's reading as it was proposed, when it has the reading's shape (else None): kept even when refused."""
    if type(proposed) is not dict or proposed.get('kind') not in KINDS:
        return None
    try:
        zone = ZoneInfo(timezone)
        start = _instant(proposed.get('start'), 'x').astimezone(zone)
        end = _instant(proposed.get('end'), 'x').astimezone(zone)
    except Refused:
        return None
    return {'kind': proposed['kind'], 'start': _iso(start), 'end': _iso(end),
            **({'granularity': proposed['granularity']} if proposed.get('granularity') in GRANULARITIES else {})}


def _rule_reading(rule):
    if rule.get('kind') not in KINDS or not rule.get('start') or not rule.get('end'):
        return None
    return {'kind': rule['kind'], 'start': rule['start'], 'end': rule['end'], **({'granularity': rule['granularity']} if rule.get('granularity') else {})}


def _assumptions(proposed):
    values = proposed.get('assumptions') if type(proposed) is dict else None
    return [a for a in values if type(a) is str][:8] if type(values) is list else []


def merge(rule, proposed, *, expression, anchor, timezone):
    """(valid_time, guard flags) from the rule reading and the model's proposal (None when it gave none). valid_time carries
    normalization whenever the model proposed something; the flags are time_source only."""
    if proposed is None:
        return rule, []

    def out(value, source, verdict):
        return value | {'normalization': {'source': source, 'verdict': verdict, 'llm': _proposal_reading(proposed, timezone),
                                          'rule': _rule_reading(rule), 'assumptions': [a[:200] for a in _assumptions(proposed)]}}, ['time_source:' + source]
    if not expression:
        return out(rule, 'rule', 'time_llm_without_expression')
    try:
        kind, start, end, granularity, _ = check(proposed, expression=expression, anchor=anchor, timezone=timezone)
    except Refused as refused:
        return out(rule, 'rule', refused.code)
    zone = ZoneInfo(timezone)
    if rule.get('kind') == 'past_reference':
        return out(rule, 'rule', 'past_reference')
    if rule['status'] in ('resolved', 'ambiguous'):
        rule_kind, rule_start, rule_end = _reading(rule, zone)
        same = rule_end == end if 'deadline' in (rule_kind, kind) else (rule_start, rule_end) == (start, end)
        if same and (rule_kind == 'deadline') == (kind == 'deadline'):
            return out(rule, 'llm+rule', 'agreed')
        return out(rule | {'status': 'ambiguous'}, 'conflict', 'conflict')
    local = anchor.astimezone(zone)
    vague = granularity in ('part_of_day', 'period', 'month')
    if kind == 'deadline':
        # As the rules read a deadline: from the anchor to the bound; a day-level bound leaves inclusive/exclusive open.
        window = [(end - timedelta(days=1)).isoformat(), end.isoformat()] if granularity == 'day' else [start.isoformat(), end.isoformat()]
        value = {'kind': 'deadline', 'status': 'ambiguous' if vague or granularity == 'day' else 'resolved', 'granularity': granularity,
                 'start': local.isoformat(), 'end': end.isoformat(), 'latest_bound_window': window}
    else:
        value = {'kind': kind, 'status': 'ambiguous' if vague else 'resolved', 'granularity': granularity,
                 'start': start.isoformat(), 'end': end.isoformat()}
    return out({k: rule[k] for k in ('expression', 'timezone', 'anchor')} | value, 'llm', 'accepted')
