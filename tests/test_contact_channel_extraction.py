"""Contact channel statements reach the 0228 vocabulary (plan docs/implementation/claim-to-consumer-property-plan.md §8.4): the prompt asks for
predicate contact_channel and a channel code, and a channel statement the model still wrote as free text ("联系方式": "站内消息") is given the
standard predicate and code when its value names exactly one channel. Other statements are untouched. Synthetic data, no model call."""
from test_conversation_extraction import materialize

import pytest

from nexloop_eios.conversation_extraction import EXTRACTOR_VERSION, PROMPT_VERSION, SYSTEM_PROMPT, contact_channel, extract


def case(body, claims):
    return {'id': 'contact-channel', 'timezone': 'Asia/Shanghai', 'messages': [{'speaker': 'consumer', 'body': body}],
            'provider_response': {'topics': [{'topic': '联系方式', 'conversation_summary': '联系渠道', 'user_valid_reply': True, 'message_refs': [1]}],
                                  'claims': [{'topic_index': 0, 'message_ref': 1, 'explicit': True, 'subject': {'kind': 'consumer', 'text': ''}, **c} for c in claims]}}


def run(body, claims):
    messages, context, provider = materialize(case(body, claims))
    return extract(messages, context, provider).claims


def S(value):
    return {'type': 'string', 'value': value}


def test_versions_and_prompt_name_the_vocabulary():
    assert EXTRACTOR_VERSION == 'nx019-extractor/4' and PROMPT_VERSION == 'nx019-conversation-extraction-prompt/4'
    assert 'predicate 固定写 contact_channel' in SYSTEM_PROMPT and 'native.webchat' in SYSTEM_PROMPT
    assert 'requested 只用于顾客请企业做一件事或提问' in SYSTEM_PROMPT
    assert '只有排他的说法' in SYSTEM_PROMPT and 'contact_channel_allowed' in SYSTEM_PROMPT


@pytest.mark.parametrize('kind,predicate,value,expected', [
    ('preference', '联系方式', '站内消息', 'native.webchat'), ('preference', '通知渠道', '只接收站内消息', 'native.webchat'),
    ('constraint', '联系方式', '电话', 'phone'), ('constraint', '联系渠道', '短信', 'sms'), ('preference', 'contact channel', 'here', 'native.webchat'),
    ('preference', 'contact_channel', 'native.webchat', 'native.webchat'), ('preference', 'preferred contact method', 'email', 'email'),
    ('preference', '沟通方式', '微信', 'wechat')])
def test_single_channel_statements_get_the_standard_predicate_and_code(kind, predicate, value, expected):
    assert contact_channel(kind, predicate, S(value)) == ('contact_channel', S(expected))


@pytest.mark.parametrize('kind,predicate,value', [
    ('constraint', '联系方式', '电话和短信'),  # two channels: the model must split them, nothing is guessed
    ('preference', '付款方式', '花呗'), ('intent', '联系方式', '站内'), ('preference', '联系方式', '随便'), ('preference', '送货时间', '周五白天')])
def test_other_statements_are_untouched(kind, predicate, value):
    assert contact_channel(kind, predicate, S(value)) is None
    assert contact_channel('preference', '联系方式', {'type': 'boolean', 'value': True}) is None


def test_free_text_channel_claims_are_normalized_in_extraction():
    claims = run('以后我只想通过站内消息收到通知，不要电话和短信', [
        {'quote': '以后我只想通过站内消息收到通知', 'kind': 'preference', 'predicate': '通知方式', 'value': S('站内消息'), 'modality': 'asserted'},
        {'quote': '不要电话和短信', 'kind': 'constraint', 'predicate': '联系方式', 'value': S('电话'), 'polarity': 'negated'},
        {'quote': '不要电话和短信', 'kind': 'constraint', 'predicate': 'contact_channel', 'value': S('sms'), 'polarity': 'negated'}])
    got = sorted((c['predicate'], c['value']['value'], c['polarity'], 'contact_channel_normalized' in c['guard_flags']) for c in claims)
    assert got == [('contact_channel', 'native.webchat', 'affirmed', True), ('contact_channel', 'phone', 'negated', True),
                   ('contact_channel', 'sms', 'negated', False)]


def test_an_agent_sentence_or_another_preference_is_not_rewritten():
    claims = run('我一般用花呗付款', [{'quote': '我一般用花呗付款', 'kind': 'preference', 'predicate': '常用付款方式', 'value': S('花呗')}])
    assert [(c['predicate'], c['value']['value']) for c in claims] == [('常用付款方式', '花呗')]


def test_an_allowance_never_stops_the_other_channels():
    """0228 reads an affirmed channel as "only these channels"; "可以用微信联系我" is an allowance and keeps a predicate outside that vocabulary."""
    claims = run('可以用微信联系我，也可以用电话', [
        {'quote': '可以用微信联系我', 'kind': 'preference', 'predicate': '联系方式', 'value': S('微信')},
        {'quote': '也可以用电话', 'kind': 'preference', 'predicate': 'contact_channel', 'value': S('phone')}])
    assert sorted((c['predicate'], c['value']['value'], 'contact_channel_not_exclusive' in c['guard_flags']) for c in claims) == [
        ('contact_channel_allowed', 'phone', True), ('contact_channel_allowed', 'wechat', True)]


def test_exclusive_and_negated_statements_keep_the_0228_predicate():
    claims = run('只用微信联系我，不要电话', [
        {'quote': '只用微信联系我', 'kind': 'preference', 'predicate': '联系方式', 'value': S('微信')},
        {'quote': '不要电话', 'kind': 'constraint', 'predicate': 'contact_channel', 'value': S('phone'), 'polarity': 'negated'}])
    assert sorted((c['predicate'], c['value']['value'], c['polarity']) for c in claims) == [
        ('contact_channel', 'phone', 'negated'), ('contact_channel', 'wechat', 'affirmed')]


@pytest.mark.parametrize('quote', ['不只用微信联系我', '不仅可以用微信', '不仅仅用微信', 'not only wechat'])
def test_not_only_is_not_exclusive(quote):
    claims = run(quote, [{'quote': quote, 'kind': 'preference', 'predicate': '联系方式', 'value': S('微信')}])
    assert [(c['predicate'], 'contact_channel_not_exclusive' in c['guard_flags']) for c in claims] == [('contact_channel_allowed', True)]
