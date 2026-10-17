"""Synthetic seed records for the workbench smoke stack (layout batch 2 acceptance, 2026-10-18): many customers, multi-row
tables, long conversations. Deterministic (same records every run), synthetic texts only, no real names, numbers or addresses.

The records have the shape tests/load_template.load takes ({index, username, conversations: [{key, messages: [{body}]}]}), so
they are written through the same governed entries as the load template: identities as trusted bootstrap rows, Consumers and
ownership through the registrar, each customer's grants as trusted facts, conversations and messages through each customer's
own ConversationMessagePort.
"""

TOPICS = (
    ('订单一直没有发货', ['我上周下的单到现在还没发货，能帮我查一下吗？', '订单号我截图发过了，就是周二那一单。', '如果今天还发不了，可以先退款吗？']),
    ('想预约上门检查', ['家里的设备最近有点异响，想预约一次上门检查。', '工作日晚上或者周末都可以。', '大概需要多长时间？要提前准备什么吗？']),
    ('咨询保养套餐', ['你们的年度保养套餐包含哪些项目？', '和单次保养相比能便宜多少？', '可以分期吗？']),
    ('退换货', ['收到的配件尺寸不对，需要换一个大一号的。', '包装还在，没有拆封。', '换货需要我自己寄回去吗？']),
    ('发票问题', ['上个月的发票还没有收到。', '抬头写公司名称，税号我稍后发给你。', '电子发票就可以。']),
)
LONG_BODY = ('我想把情况完整说明一下：' + '这台设备是去年买的，一开始用得很好，最近两周开始偶尔会出现异响，尤其是早上刚开机的时候比较明显，'
             '运行一段时间之后又会好一些。我查过说明书，按照上面的步骤清理过一次，但是没有明显改善。' * 6)[:400]
MULTILINE_BODY = '补充几点信息：\n1. 设备型号在背面铭牌上；\n2. 购买渠道是官方门店；\n3. 保修卡还在。\n方便的话请一起记录。'


def large_records(customers=40, long_conversation_turns=60):
    """`customers` customers with 1-3 conversations of 1-6 messages each (index-dependent), customer 1 with one conversation of
    `long_conversation_turns` messages, some long (400 characters) and multi-line bodies."""
    records = []
    for index in range(1, customers + 1):
        topic, lines = TOPICS[index % len(TOPICS)]
        conversations = []
        for c in range(1 + index % 3):
            messages = [{'body': lines[m % len(lines)] + ('' if m < len(lines) else f'（第 {m + 1} 条）')} for m in range(1 + (index + c) % 6)]
            if index % 7 == 0:
                messages.append({'body': LONG_BODY})
            if index % 11 == 0:
                messages.append({'body': MULTILINE_BODY})
            conversations.append({'key': f'seed-{index:03d}-{c}', 'messages': messages})
        if index == 1:
            conversations = [{'key': 'seed-001-long', 'messages': [
                {'body': (TOPICS[m % len(TOPICS)][1][m % 3] if m % 9 else LONG_BODY) + f'（第 {m + 1} 轮）'} for m in range(long_conversation_turns)]}]
        records.append({'index': 9000 + index, 'username': f'seed-customer-{index:03d}', 'topic': topic, 'conversations': conversations})
    return records
