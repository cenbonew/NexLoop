"""ADR-023 degraded fallback (draft, owner 2026-10-18): when the pinned sources alone exceed the bound input size, the fallback
reply Run's Context keeps every constraint, contact limit and refusal, names the pinned open work by reference
(mandatory_exceeds_budget, open_work) and leaves the rest out; when the contact limits alone do not fit, no Context at all."""
import pytest

from nexloop_eios.context_artifacts import degraded_fallback_items
from nexloop_eios.context_engine.budget import Item
from nexloop_eios.context_engine.pack import assemble_v6
from context_v6_example import D, inputs


def limit(n, size=10):
    return Item('evidence', 'claim_evidence', f'claim:{n:064x}', 'nx019-extractor/2', {'quote': '不要联系' * size}, 'user_statement', D,
                relevance=0.8, at='2026-10-09T02:00:00Z', tags={'contact_limit'})


def intent(n):
    return Item('open_work', 'intents', f'nexloop:intent:00000000-0000-4000-8000-{n:012x}', 'unknown', {'action': 'nexloop.service.request:1', 'state': 'unknown',
                'since': '2026-10-16T00:00:00Z'}, 'execution_state', D, relevance=1.0, at='2026-10-16T00:00:00Z', tags={'unconfirmed'})


def build(items, extra):
    case = inputs()
    case['items'] = [i for i in case['items'] if i.section == 'constraints'] + items
    return assemble_v6(**{**case, 'extra_insufficient': extra})


def test_the_degraded_context_keeps_every_contact_limit_and_names_the_open_work():
    turn = Item('evidence', 'conversation', 'eios:object:Message/' + '7' * 64, '9', {'speaker': 'consumer', 'body': '退款到账了吗', 'sequence': 9,
                'accepted_at': '2026-10-09T04:00:00Z'}, 'conversation', D, relevance=0.5, at='2026-10-09T04:00:00Z', tags={'latest_consumer_turn'})
    loose = Item('evidence', 'claim_evidence', 'claim:' + 'e' * 64, 'nx019-extractor/2', {'quote': '随便聊聊'}, 'user_statement', D, relevance=0.9, at='2026-10-09T02:00:00Z')
    items = [limit(n) for n in range(5)] + [intent(n) for n in range(300)] + [turn, loose]
    with pytest.raises(ValueError, match='context_pack_too_large'):
        build(items, [])  # the whole set cannot be bound
    candidates, extra = degraded_fallback_items(items)
    assert extra == [{'code': 'mandatory_exceeds_budget', 'section': 'open_work', 'refs': [intent(n).ref for n in range(300)]}]
    assert all(sum('contact_limit' in i.tags for i in c) == 5 for c in candidates) and turn in candidates[0] and turn not in candidates[1]
    assert loose not in candidates[0] and not any(i.section == 'open_work' for c in candidates for i in c)
    body, _ = build(candidates[0], extra)
    assert sum('contact_limit' in i['tags'] for i in body['evidence']) == 5 and body['open_work'] == []
    assert body['insufficient'][-1]['code'] == 'mandatory_exceeds_budget'


def test_contact_limits_alone_over_the_bound_never_fit_so_no_degraded_context():
    candidates, extra = degraded_fallback_items([limit(n, size=2000) for n in range(12)])
    for items in candidates:
        with pytest.raises(ValueError, match='context_pack_too_large'):
            build(items, extra)  # the producer then raises fallback_context_unsafe: the owner is escalated, no reply
