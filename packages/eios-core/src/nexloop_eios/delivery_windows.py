"""Claim → Consumer delivery_windows (design docs/implementation/claim-consumer-delivery-windows-design.md §1, §4.1; owner 2026-11-02,
plan §10 item 2): the deterministic normalization of a delivery-time statement into [{weekday, part}].

Pure: a rule table in code, no model. The matcher may only choose the value this function returns for a Claim (Layer 4); anything
it cannot normalize is not written and the Claim stays needs_resolution. No clock times are stored: a statement with one ("after
9", "6pm") is refused, never converted. Negations, questions, hypotheticals, tentative wording and unmapped times of day ("中午",
"midnight") are refused too. Habitual wording ("一般", "通常", "usually") is accepted as an ordinary statement: a delivery window is a
habit, not a promise. "只 / 仅" limiting the statement is accepted; "不仅 / 不只 / not only" is not a negation.

The value is canonical: non-empty, duplicates removed, ordered by the vocabularies below (the SQL value check,
ontology.nexloop_property_value_valid, requires exactly this order).
"""
from __future__ import annotations

import re
from itertools import product

WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'any')
PARTS = ('morning', 'daytime', 'afternoon', 'evening', 'any')
MAX_ITEMS = 40
DESCRIPTOR = {'kind': 'array', 'max_items': MAX_ITEMS, 'element': {'kind': 'struct', 'fields': [
    {'name': 'weekday', 'required': True, 'descriptor': {'kind': 'string', 'enum': list(WEEKDAYS)}},
    {'name': 'part', 'required': True, 'descriptor': {'kind': 'string', 'enum': list(PARTS)}}]}}
DISPLAY_NAME = '送货时间'
DESCRIPTION = '顾客说过方便收货的星期与时段（如“周五白天”），由对话中的陈述经规范化规则表得出；不含具体钟点。'

_WORKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri')
_CN_DAY = {'一': 'mon', '二': 'tue', '三': 'wed', '四': 'thu', '五': 'fri', '六': 'sat', '日': 'sun', '天': 'sun'}
_EN_DAY = {'monday': 'mon', 'mon': 'mon', 'tuesday': 'tue', 'tue': 'tue', 'tues': 'tue', 'wednesday': 'wed', 'wed': 'wed',
           'thursday': 'thu', 'thu': 'thu', 'thurs': 'thu', 'friday': 'fri', 'fri': 'fri', 'saturday': 'sat', 'sat': 'sat',
           'sunday': 'sun', 'sun': 'sun'}

# Rule table: (pattern, kind, values). Order matters only for overlapping spellings (longest first).
DAY_RULES = (
    (r'(?:周|星期|礼拜)([一二三四五六日天])', 'cn_day', None),
    (r'工作日|周一到周五|平日', 'days', _WORKDAYS),
    (r'周末|双休日?', 'days', ('sat', 'sun')),
    (r'每天|天天|每日|哪天都', 'days', ('any',)),
    (r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|fri|sat|sun)s?\b', 'en_day', None),
    (r'\bweekdays?\b', 'days', _WORKDAYS),
    (r'\bweekends?\b', 'days', ('sat', 'sun')),
    (r'\b(?:every ?day|daily|any day)\b', 'days', ('any',)),
)
PART_RULES = (
    (r'上午|早上|早晨|清早', 'morning'),
    (r'白天|日间', 'daytime'),
    (r'下午', 'afternoon'),
    (r'晚上|傍晚|夜里|晚间', 'evening'),
    (r'全天|整天|一整天', 'any'),
    (r'\bmornings?\b', 'morning'),
    (r'\b(?:during the day|daytime|in the day)\b', 'daytime'),
    (r'\bafternoons?\b', 'afternoon'),
    (r'\b(?:evenings?|tonight|at night|nights)\b', 'evening'),
    (r'\ball day\b', 'any'),
)
ANYTIME = re.compile(r'随时|任何时候|什么时候都|都可以|都行|\bany ?time\b|\bwhenever\b', re.I)
# Refused outright.
CLOCK = re.compile(r'\d{1,2}\s*(?:点|[:：]\d|时|am\b|pm\b|a\.m\.|p\.m\.|o\'?clock)|[一二三四五六七八九十两]+点|\bnoon\b|\bmidnight\b', re.I)
UNMAPPED = re.compile(r'中午|凌晨|半夜|午夜|夜间?\d', re.I)
QUESTION = re.compile(r'[?？]|吗|\bwhat\b|\bwhen\b|\bcan you\b', re.I)
HYPOTHETICAL = re.compile(r'如果|要是|假如|万一|\bif\b|\bunless\b', re.I)
TENTATIVE = re.compile(r'可能|也许|大概|说不定|不确定|\bmaybe\b|\bmight\b|\bperhaps\b|\bprobably\b', re.I)
# Not negations: "不仅 / 不只 / not only" (removed before the negation check).
NOT_ONLY = re.compile(r'不仅仅|不仅|不只|\bnot only\b|\bnot just\b', re.I)
NEGATION = re.compile(r'不|没|别|甭|\bnot\b|n\'t\b|\bnever\b|\bno\b|\bexcept\b|除了', re.I)
# Ignorable modifiers: limiting "只 / 仅" (never after 不 / 不仅) and habitual wording.
LIMITING = re.compile(r'(?<!不)(?<!不仅)(只|仅)')
HABITUAL = re.compile(r'一般|通常|基本上|平时|平常|\busually\b|\bnormally\b|\bgenerally\b|\btypically\b|\bmostly\b', re.I)


def canonical(windows) -> list[dict]:
    """Deduplicated and ordered by the vocabularies."""
    seen = {(w['weekday'], w['part']) for w in windows}
    return [{'weekday': d, 'part': p} for d, p in sorted(seen, key=lambda x: (WEEKDAYS.index(x[0]), PARTS.index(x[1])))]


def _tokens(text: str) -> list[tuple[int, str, tuple]]:
    out = []
    for pattern, kind, values in DAY_RULES:
        for m in re.finditer(pattern, text, re.I):
            days = (_CN_DAY[m.group(1)],) if kind == 'cn_day' else (_EN_DAY[m.group(1).lower()],) if kind == 'en_day' else values
            out.append((m.start(), m.end(), 'day', days))
    for pattern, part in PART_RULES:
        for m in re.finditer(pattern, text, re.I):
            out.append((m.start(), m.end(), 'part', (part,)))
    out.sort(key=lambda x: (x[0], -x[1]))
    kept, end = [], -1
    for start, stop, kind, values in out:  # overlapping spellings: the earlier, longer one wins
        if start >= end:
            kept.append((start, kind, values));end = stop
    return kept


def normalize(text) -> list[dict] | None:
    """[{weekday, part}] for a delivery-time statement, or None when it cannot be normalized deterministically."""
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        return None
    t = text.strip()
    if CLOCK.search(t) or UNMAPPED.search(t) or QUESTION.search(t) or HYPOTHETICAL.search(t) or TENTATIVE.search(t):
        return None
    t = NOT_ONLY.sub(' ', t)
    if NEGATION.search(t):
        return None
    t = HABITUAL.sub(' ', LIMITING.sub(' ', t))
    anytime = bool(ANYTIME.search(t))
    tokens = _tokens(t)
    if not tokens:
        return [{'weekday': 'any', 'part': 'any'}] if anytime else None
    windows, pending, last_days = [], [], ()
    for _, kind, values in tokens:
        if kind == 'day':
            pending.extend(values)
            continue
        days = tuple(pending) or last_days or ('any',)
        windows += [{'weekday': d, 'part': p} for d, p in product(days, values)]
        last_days, pending = days, []
    if pending:
        windows += [{'weekday': d, 'part': 'any'} for d in pending]
    out = canonical(windows)
    return out if 0 < len(out) <= MAX_ITEMS else None
