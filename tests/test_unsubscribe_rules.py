"""Unsubscribe rules v1 (deploy/configuration/unsubscribe-rules.v1.json; outreach design §4.4, plan O4 and appendix A, owner
approved 2026-10-20): the file's shape, its patterns as plain literals (valid PostgreSQL ARE and Python re alike), and the
approved entries and exclusions on the owner's examples, matched as the inbound check will (normalize, exclusions first).
The file is not loaded into PostgreSQL yet (outreach slice 7)."""
import json
import re
import unicodedata
from pathlib import Path

RULES = json.loads((Path(__file__).resolve().parents[1] / 'deploy/configuration/unsubscribe-rules.v1.json').read_text())


def normalize(text):
    return re.sub(r'^[\s\W_]+|[\s\W_]+$', '', unicodedata.normalize('NFKC', text).strip().lower())


def unsubscribes(text):
    t = normalize(text)
    if any(re.search(x['pattern'], t, re.I) for x in RULES['exclusions']):
        return None
    return next((x['id'] for x in RULES['unsubscribe'] if re.search(x['pattern'], t, re.I)), None)


def test_shape_and_append_only_metadata():
    assert set(RULES) == {'schema', 'version', 'decision', 'window_days', 'topic', 'unsubscribe', 'exclusions'}
    assert RULES['schema'] == 'nexloop-unsubscribe-rules/1' and RULES['version'] == 1 and RULES['window_days'] == 30 and RULES['topic'] == 'marketing'
    rows = RULES['unsubscribe'] + RULES['exclusions']
    assert len(RULES['unsubscribe']) == 17 and len(RULES['exclusions']) == 6
    assert len({r['id'] for r in rows}) == len(rows) and all(set(r) == {'id', 'match', 'text', 'pattern'} for r in rows)
    for r in rows:
        re.compile(r['pattern'])
        assert r['match'] in ('exact', 'contains') and (r['pattern'] == '^' + r['text'].lower() + '$' if r['match'] == 'exact' else r['pattern'] == re.escape(r['text']))


def test_the_approved_entries_and_exclusions():
    for text in ('退订', 'TD', 'td。', '  ＴＤ ', '退订营销信息', '别给我推活动了', '以后不要发广告', '不想收到活动消息', '请关闭营销消息', '我不接收营销信息'):
        assert unsubscribes(text), text
    for text in ('怎么退订？', '如何退订', '退订了吗', '我不要退订', '我要退款，也退订', '我想退订单', '我想要 TD 的说明书', '谢谢'):
        assert unsubscribes(text) is None, text
