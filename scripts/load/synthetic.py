"""NX-031 slice 0: the fixed-seed synthetic data manifest (design docs/implementation/NX-031-design.md §2).

`generate(seed, consumers)` is a pure function: the same seed, consumer count and code give byte-identical JSON Lines, and the
digest of those lines is recorded with every template and report. Text comes only from the fixed Chinese template lists below
plus numbers; nothing resembles a real person. The proportions are the design's §2.2 values (assumptions until business data
exists, owner D2). Usage:

    python scripts/load/synthetic.py --seed 31 --consumers 1000 --out manifest.jsonl   # prints {"digest":..., "consumers":...}
"""
import argparse
import hashlib
import json
import random
import sys

SCHEMA = 'nexloop.load-manifest.v1'
TENANT = 'synthetic-a'  # the fixtures' synthetic tenant inside a disposable cluster (D1: world real, synthetic data only)
# §2.2 proportions (D2).
P_RESTRICTED = 0.08
P_COMMITMENT = 0.20
P_ACTIVE_PLAN = 0.30
P_COMMERCIAL = 0.40
MEAN_HISTORY = 20

PRODUCTS = ('会员年卡', '月度套餐', '家庭套装', '入门课程', '进阶课程', '配件礼盒', '季度订阅', '体验装')
ASKS = ('请问{p}的续费价格是多少？', '{p}什么时候发货？', '我想把{p}换成别的款式。', '{p}的使用说明在哪里看？', '上次买的{p}还没收到。',
        '能不能帮我查一下订单{n}的进度？', '{p}可以开发票吗？', '我对{p}还有点疑问，方便解释一下吗？')
FOLLOWUPS = ('好的，谢谢。', '明白了。', '那我再等等。', '收到，麻烦了。', '可以，就这样吧。', '我再考虑一下。')
COMMITMENT_ASKS = ('请在{d}天内给我回个电话。', '麻烦明天之前把发票发给我。', '能不能下周一前把{p}寄出来？')
REFUSALS = ('以后不要再给我发消息了。', '请不要再联系我。', '别再给我打电话了。')
COMMERCIAL_KINDS = ('payment', 'payment', 'payment', 'refund')


def _text(rng, kind, index):
    product = PRODUCTS[rng.randrange(len(PRODUCTS))]
    if kind == 'ask':
        return ASKS[rng.randrange(len(ASKS))].format(p=product, n=f'S{index:06d}')
    if kind == 'commitment_ask':
        return COMMITMENT_ASKS[rng.randrange(len(COMMITMENT_ASKS))].format(p=product, d=rng.randint(1, 5))
    if kind == 'refusal':
        return REFUSALS[rng.randrange(len(REFUSALS))]
    return FOLLOWUPS[rng.randrange(len(FOLLOWUPS))]


def generate(seed, consumers):
    """Yield one JSON object per consumer, then a trailer with the totals. Deterministic in (seed, consumers)."""
    if type(seed) is not int or type(consumers) is not int or not 1 <= consumers <= 100000:
        raise ValueError('integer seed and 1..100000 consumers required')
    rng = random.Random(f'{SCHEMA}:{seed}:{consumers}')
    totals = {'consumers': consumers, 'conversations': 0, 'messages': 0, 'restricted': 0, 'commitment': 0, 'active_plan': 0, 'commercial_events': 0}
    for index in range(1, consumers + 1):
        restricted = rng.random() < P_RESTRICTED
        commitment = rng.random() < P_COMMITMENT
        plan = rng.random() < P_ACTIVE_PLAN
        conversations = []
        history = max(1, round(rng.expovariate(1 / MEAN_HISTORY)))
        count = rng.choice((1, 1, 2, 3))
        split = sorted(rng.sample(range(1, history), min(count - 1, history - 1))) if history > 1 else []
        bounds = [0, *split, history]
        sequence = 0
        for c in range(len(bounds) - 1):
            messages = []
            for m in range(bounds[c + 1] - bounds[c]):
                sequence += 1
                kind = 'ask' if m == 0 else 'followup'
                if commitment and c == 0 and m == 1:
                    kind = 'commitment_ask'
                messages.append({'kind': kind, 'body': _text(rng, kind, index * 1000 + sequence)})
            if commitment and c == 0 and not any(m['kind'] == 'commitment_ask' for m in messages):
                sequence += 1  # a commitment consumer always asks for something the enterprise can promise (slice 1 loads the promise)
                messages.append({'kind': 'commitment_ask', 'body': _text(rng, 'commitment_ask', index * 1000 + sequence)})
            conversations.append({'key': f'c{index:05d}-{c + 1}', 'messages': messages})
        if restricted:  # the refusal is the consumer's latest message in its last conversation (0109 restricts from there)
            conversations[-1]['messages'].append({'kind': 'refusal', 'body': _text(rng, 'refusal', index)})
        events = []
        if rng.random() < P_COMMERCIAL:
            for e in range(rng.randint(1, 4)):
                kind = COMMERCIAL_KINDS[rng.randrange(len(COMMERCIAL_KINDS))] if e else 'payment'
                events.append({'kind': kind, 'event_key': f'evt-{index:05d}-{e + 1}', 'amount_minor': rng.choice((990, 1990, 4990, 9900, 19900)), 'currency': 'CNY'})
        record = {'schema': SCHEMA, 'index': index, 'username': f'load-c{index:05d}', 'restricted': restricted, 'commitment': commitment,
                  'active_plan': plan, 'conversations': conversations, 'commercial_events': events}
        totals['conversations'] += len(conversations)
        totals['messages'] += sum(len(c['messages']) for c in conversations)
        totals['restricted'] += restricted
        totals['commitment'] += commitment
        totals['active_plan'] += plan
        totals['commercial_events'] += len(events)
        yield record
    yield {'schema': SCHEMA, 'trailer': True, 'seed': seed, 'totals': totals}


def lines(seed, consumers):
    return [json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')) for record in generate(seed, consumers)]


def digest(text_lines):
    return hashlib.sha256(('\n'.join(text_lines) + '\n').encode()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--consumers', type=int, default=1000)
    parser.add_argument('--out', required=True)
    a = parser.parse_args(argv)
    text = lines(a.seed, a.consumers)
    with open(a.out, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(text) + '\n')
    trailer = json.loads(text[-1])
    print(json.dumps({'digest': digest(text), **trailer['totals']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
