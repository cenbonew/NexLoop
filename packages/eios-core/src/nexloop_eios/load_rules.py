"""NX-031 load lane only: a rule-based extraction provider for the synthetic load data (dispatcher ruling 2026-10-11).

It gives the extraction chain a real load shape during load tests and nothing else: one topic per window, and one commitment
Claim for each enterprise-side sentence of the synthetic promise template (「我会在…」 written by tests/load_template.py); any
other text yields no Claim. It never calls a model or the network and holds no credential. It is selected only as the test
model profile's variant `MODEL_TEST_VARIANT=load-rules`, which nexloop_eios.model_profile accepts only when the caller allows
it (`--mode test`); stage/real refuse it at startup and the doctor fails it. Extraction quality under this provider is not
evidence for any acceptance test.
"""
import json

from nexloop_eios.conversation_extraction import canonical

PROMISE = '我会在'


class LoadRulesExtractionProvider:
    provider, model_id = 'test', 'load-rules'

    def complete(self, system_prompt, user_payload):
        rows = json.loads(user_payload)['conversation_data']
        claims = []
        for row in rows:
            text = row['text']
            if row['speaker'] == '客服' and PROMISE in text:
                start = text.index(PROMISE)
                end = text.find('。', start)
                claims.append({'topic_index': 0, 'message_ref': row['ref'], 'quote': text[start:end if end > 0 else None], 'kind': 'commitment',
                               'subject': {'kind': 'enterprise', 'text': ''}, 'predicate': '回访客户', 'value': {'type': 'string', 'value': '按约定时间回访'}})
        return canonical({'topics': [{'topic': '客户咨询', 'conversation_summary': '合成对话', 'user_valid_reply': True,
                                      'message_refs': [r['ref'] for r in rows]}], 'claims': claims})
