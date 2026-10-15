"""Decision-evaluation layer (branch only, not wired into any runtime path): client, question file, evidence, offline shadow.

Entirely offline: the transport is a recording fake that returns synthetic System One responses (the answer shapes the dispatcher
observed on 2026-10-15: noul probability; choice with choice, confidence, probabilities; score with score, probabilities, legend).
No network, no real credential: the credential file holds a synthetic value and the tests assert it never appears in results."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from nexloop_eios import decision_eval as de

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = de.load_questions(ROOT / 'deploy/configuration/decision-questions.v1.json')
CLAIM, HANDOFF = QUESTIONS['sets']['claim_review'], QUESTIONS['sets']['handoff']
SYNTHETIC_CREDENTIAL = 'synthetic-credential-not-a-secret-0123'
STATE = {'customer': '我下周三想预约上门维修。', 'claim': {'predicate': 'wants_appointment', 'object': 'next Wednesday', 'kind': 'stated_intent'}}


def claim_answers(grounded=0.97, kind='stated_intent', confidence=0.95, injection=0.01):
    options = CLAIM['questions']['kind']['options']
    probs = {o: (confidence if o == kind else round((1 - confidence) / (len(options) - 1), 6)) for o in options}
    return {'answers': {'grounded': {'probability': grounded}, 'kind': {'choice': kind, 'confidence': confidence, 'probabilities': probs},
                        'injection': {'probability': injection}}, 'usage': {'input_tokens': 1012, 'output_tokens': 0}}


def handoff_answers(needs_human=0.05, score=0, p=0.9):
    probs = {str(v): (p if v == score else round((1 - p) / 3, 6)) for v in range(4)}
    return {'answers': {'needs_human': {'probability': needs_human},
                        'risk': {'score': score, 'probabilities': probs, 'legend': HANDOFF['questions']['risk']['legend']}}}


class FakeTransport:
    """Replays scripted outcomes: (status, payload) tuples, or an exception instance to raise."""
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({'url': url, 'headers': dict(headers), 'body': json.loads(body), 'timeout': timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        status, payload = outcome
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def credential(tmp_path):
    path = tmp_path / 'ark-credential'
    path.write_text(SYNTHETIC_CREDENTIAL)
    path.chmod(0o600)
    return path


def client(credential, transport, clock=None, **kw):
    clock = clock or Clock()
    return de.DecisionEvalClient(endpoint='https://decision.example.invalid/api/v1/systemone', credential_file=credential, model='synthetic-model',
                                 transport=transport, sleep=clock.sleep, clock=clock, jitter=lambda: 1.0, **kw)


# Client ------------------------------------------------------------------------------------------------------------------
def test_a_valid_answer_is_ok_with_the_beta_header_and_only_type_instructions_criteria_sent(credential):
    transport = FakeTransport((200, claim_answers()))
    result = client(credential, transport).evaluate(CLAIM, STATE)
    assert result.status == 'ok' and result.attempts == 1 and result.usage == {'input_tokens': 1012, 'output_tokens': 0}
    call = transport.calls[0]
    assert call['headers']['ark-beta-systemone-api'] == 'true' and call['headers']['Authorization'] == 'Bearer ' + SYNTHETIC_CREDENTIAL
    assert call['body']['model'] == 'synthetic-model' and set(call['body']['questions']) == {'grounded', 'kind', 'injection'}
    assert all(set(q) == {'type', 'instructions', 'criteria'} for q in call['body']['questions'].values())
    assert SYNTHETIC_CREDENTIAL not in json.dumps(result.__dict__)
    assert result.cache_key == f'{de.state_digest(STATE)}:claim_review.v1:synthetic-model'


@pytest.mark.parametrize('outcomes,reason,attempts', [
    ([(503, {}), (502, {}), (500, {}), (504, {})], 'http_5xx', 4),
    ([TimeoutError(), TimeoutError(), TimeoutError(), TimeoutError()], 'timeout', 4),
    ([de.TransportError('ConnectionRefusedError')] * 4, 'transport', 4),
    ([(401, {'error': 'unauthorized'})], 'http_4xx', 1),
    ([(429, {})], 'http_4xx', 1),
])
def test_failures_are_uncertain_retried_only_when_transient_with_exponential_backoff(credential, outcomes, reason, attempts):
    clock = Clock()
    transport = FakeTransport(*outcomes)
    result = client(credential, transport, clock, retries=3, backoff=1.0).evaluate(CLAIM, STATE)
    assert result.status == 'uncertain' and result.reason == reason and result.attempts == attempts and result.answers == {}
    assert clock.sleeps == ([1.0, 2.0, 4.0] if attempts == 4 else [])


def test_a_transient_failure_then_success_is_ok(credential):
    clock = Clock()
    result = client(credential, FakeTransport((503, {}), TimeoutError(), (200, claim_answers())), clock).evaluate(CLAIM, STATE)
    assert result.status == 'ok' and result.attempts == 3 and clock.sleeps == [1.0, 2.0]


def test_the_deadline_bounds_the_whole_call(credential):
    clock = Clock()
    def slow(url, headers, body, timeout):
        clock.now += timeout  # every attempt burns its full timeout
        raise TimeoutError()
    result = client(credential, slow, clock, timeout=5.0, retries=5, backoff=1.0).evaluate(CLAIM, STATE)
    assert result.status == 'uncertain' and result.reason in ('timeout', 'deadline')
    assert clock.now <= 5.0 * 6 + 1.0 * 2 ** 5


def mutate(path, value):
    payload = claim_answers()
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return payload


@pytest.mark.parametrize('payload', [
    b'not json', {'answers': []}, {}, mutate(('answers', 'grounded'), KeyError), mutate(('answers', 'grounded', 'probability'), 1.2),
    mutate(('answers', 'grounded', 'probability'), -0.1), mutate(('answers', 'grounded', 'probability'), 'high'),
    mutate(('answers', 'grounded', 'probability'), True), mutate(('answers', 'grounded', 'probability'), float('nan')),
    mutate(('answers', 'kind', 'choice'), 'unknown_kind'), mutate(('answers', 'kind', 'confidence'), 2),
    mutate(('answers', 'kind', 'probabilities', 'hypothesis'), 0.9),          # sum is not 1
    mutate(('answers', 'injection'), {'prob': 0.1}),
])
def test_an_invalid_response_is_uncertain_and_never_cached(credential, payload):
    transport = FakeTransport((200, payload), (200, claim_answers()))
    c = client(credential, transport)
    first = c.evaluate(CLAIM, STATE)
    assert first.status == 'uncertain' and first.reason == 'invalid_response' and first.attempts == 1
    assert c.evaluate(CLAIM, STATE).status == 'ok' and len(transport.calls) == 2


def test_choice_must_be_the_most_probable_and_score_inside_the_scale(credential):
    wrong_choice = claim_answers(kind='stated_intent', confidence=0.95)
    wrong_choice['answers']['kind']['choice'] = 'hypothesis'
    assert client(credential, FakeTransport((200, wrong_choice))).evaluate(CLAIM, STATE).reason == 'invalid_response'
    for bad in ({'score': 4}, {'score': '2'}, {'probabilities': {'0': 1.0}}):
        payload = handoff_answers()
        payload['answers']['risk'].update(bad)
        assert client(credential, FakeTransport((200, payload))).evaluate(HANDOFF, 'x').reason == 'invalid_response'


def test_a_validated_result_is_cached_by_digest_set_version_and_model(credential):
    transport = FakeTransport((200, claim_answers()), (200, claim_answers()))
    c = client(credential, transport)
    assert c.evaluate(CLAIM, STATE).cache_hit is False
    again = c.evaluate(CLAIM, STATE)
    assert again.cache_hit is True and again.status == 'ok' and len(transport.calls) == 1
    c.evaluate(CLAIM, {**STATE, 'customer': '另一句话'})
    assert len(transport.calls) == 2


def test_configuration_problems_are_uncertain_or_refused_without_reading_the_environment(credential, tmp_path, monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'from-the-environment-never-used')
    loose = tmp_path / 'loose'
    loose.write_text(SYNTHETIC_CREDENTIAL)
    loose.chmod(0o644)
    transport = FakeTransport()
    assert client(loose, transport).evaluate(CLAIM, STATE).reason == 'configuration' and transport.calls == []
    assert client(tmp_path / 'absent', transport).evaluate(CLAIM, STATE).reason == 'configuration'
    for endpoint in ('http://decision.example.invalid/x', 'https://user:pw@decision.example.invalid/x', 'https://decision.example.invalid/x?k=v'):
        with pytest.raises(ValueError):
            de.DecisionEvalClient(endpoint=endpoint, credential_file=credential, model='m')
    big = client(credential, transport).evaluate(CLAIM, 'x' * (de.MAX_STATE_BYTES + 1))
    assert big.reason == 'state_too_large' and transport.calls == []


# Questions ---------------------------------------------------------------------------------------------------------------
def test_the_question_file_has_two_versioned_sets_with_the_required_questions():
    assert QUESTIONS['version'] == 1 and set(QUESTIONS['sets']) == {'claim_review', 'handoff'}
    assert {q: v['type'] for q, v in CLAIM['questions'].items()} == {'grounded': 'noul', 'kind': 'choice', 'injection': 'noul'}
    assert 'insufficient_information' in CLAIM['questions']['kind']['options']
    assert {q: v['type'] for q, v in HANDOFF['questions'].items()} == {'needs_human': 'noul', 'risk': 'score'}
    for name, qset in QUESTIONS['sets'].items():
        assert qset['version'] == f'{name}.v1' and all(q['version'] == f'{name}.{qid}.v1' for qid, q in qset['questions'].items())


@pytest.mark.parametrize('change', [
    lambda b: b.update(schema='other'),
    lambda b: b['sets']['claim_review'].update(version='claim_review'),
    lambda b: b['sets']['claim_review']['questions']['grounded'].update(type='yesno'),
    lambda b: b['sets']['claim_review']['questions']['grounded'].update(version='grounded.v1'),
    lambda b: b['sets']['claim_review']['thresholds']['grounded'].update(low=0.9, high=0.2),
    lambda b: b['sets']['claim_review']['questions']['kind'].update(options=['only_one']),
    lambda b: b['sets']['claim_review']['thresholds'].pop('injection'),
    lambda b: b['sets']['handoff']['questions']['risk'].update(scale=[0, 5]),
    lambda b: b['sets']['handoff']['thresholds']['risk'].update(handoff_at=9),
    lambda b: b['sets']['handoff']['questions']['needs_human'].update(extra='x'),
])
def test_an_invalid_question_file_is_refused(change):
    body = copy.deepcopy(QUESTIONS)
    change(body)
    with pytest.raises(de.QuestionsInvalid):
        de.validate_questions(body)


# Decisions and evidence --------------------------------------------------------------------------------------------------
def ok(qset, payload):
    return de.Result(status='ok', set_version=qset['version'], model='m', state_digest='d' * 64, cache_key='k',
                     answers=de.parse_response(qset, payload))


def test_claim_review_decisions_send_doubt_to_a_human():
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers()), extracted_kind='stated_intent') == ('pass', 'grounded')
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers(injection=0.9))) == ('needs_human', 'injection_suspected')
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers(grounded=0.05))) == ('needs_human', 'ungrounded')
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers(grounded=0.5)))[0] == 'uncertain'          # between thresholds
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers(confidence=0.6)))[0] == 'uncertain'        # low confidence
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers(kind='insufficient_information')))[0] == 'uncertain'
    assert de.decide('claim_review', CLAIM, ok(CLAIM, claim_answers()), extracted_kind='hypothesis')[0] == 'uncertain'  # disagrees
    failed = de.Result(status='uncertain', set_version='claim_review.v1', model='m', state_digest='d', cache_key='k', reason='timeout')
    assert de.decide('claim_review', CLAIM, failed) == ('uncertain', 'timeout')


def test_handoff_decisions():
    assert de.decide('handoff', HANDOFF, ok(HANDOFF, handoff_answers(needs_human=0.95))) == ('handoff', 'decision_eval_handoff')
    assert de.decide('handoff', HANDOFF, ok(HANDOFF, handoff_answers(needs_human=0.1, score=3))) == ('handoff', 'decision_eval_handoff')
    assert de.decide('handoff', HANDOFF, ok(HANDOFF, handoff_answers())) == ('continue', 'no_handoff')
    assert de.decide('handoff', HANDOFF, ok(HANDOFF, handoff_answers(needs_human=0.5)))[0] == 'uncertain'
    assert de.decide('handoff', HANDOFF, ok(HANDOFF, handoff_answers(p=0.4)))[0] == 'uncertain'  # unsure score


def test_the_evidence_record_has_the_design_fields_and_no_text_or_credential():
    result = ok(CLAIM, claim_answers())
    record = de.evidence_record(set_name='claim_review', qset=CLAIM, result=result, decision='pass', reason_code='grounded',
                                subject_kind='claim_candidate', subject_id='c-1')
    assert {'state_digest', 'question_set', 'question_versions', 'model_id', 'answers', 'thresholds', 'decision', 'reason_code'} <= set(record)
    assert record['question_set'] == 'claim_review.v1' and record['question_versions']['grounded'] == 'claim_review.grounded.v1'
    assert record['answers']['grounded'] == {'type': 'noul', 'probability': 0.97} and record['thresholds']['values'] == CLAIM['thresholds']
    text = json.dumps(record, ensure_ascii=False)
    assert STATE['customer'] not in text and SYNTHETIC_CREDENTIAL not in text and 'reason' not in record['answers']
    assert de.evidence_record(set_name='claim_review', qset=CLAIM, result=result, decision='pass', reason_code='grounded') == \
        de.evidence_record(set_name='claim_review', qset=CLAIM, result=result, decision='pass', reason_code='grounded')  # pure


# Shadow tool -------------------------------------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location('nx_decision_shadow', ROOT / 'scripts/nx_decision_shadow.py')
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)


@pytest.fixture
def items(tmp_path):
    path = tmp_path / 'items.json'
    path.write_text(json.dumps({'items': [{'id': 'c1', 'set': 'claim_review', 'state': STATE, 'extracted_kind': 'stated_intent'},
                                          {'id': 'h1', 'set': 'handoff', 'state': '我要投诉，请找人工。'}]}, ensure_ascii=False))
    return path


def test_shadow_dry_run_is_the_default_and_never_touches_network_or_credential(items, capsys, monkeypatch):
    monkeypatch.setattr(de, 'https_transport', lambda *a, **k: pytest.fail('network used in a dry run'))
    assert shadow.main(['--input', str(items)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['mode'] == 'dry-run' and out['network'] is False and [r['set'] for r in out['requests']] == ['claim_review.v1', 'handoff.v1']
    assert all(set(r) == {'id', 'set', 'questions', 'state_digest', 'request_bytes'} for r in out['requests'])
    assert STATE['customer'] not in json.dumps(out, ensure_ascii=False)


def test_shadow_live_requires_endpoint_credential_and_model(items, capsys):
    assert shadow.main(['--input', str(items), '--live']) == 2


def test_shadow_live_report_with_a_fake_client(items, credential, tmp_path, capsys):
    transport = FakeTransport((200, claim_answers()), (503, {}), (503, {}), (503, {}), (503, {}))
    clock = Clock()
    factory = lambda **kw: de.DecisionEvalClient(**kw, transport=transport, sleep=clock.sleep, clock=clock, jitter=lambda: 1.0)
    out_file = tmp_path / 'report.json'
    assert shadow.main(['--input', str(items), '--live', '--endpoint', 'https://decision.example.invalid/x', '--credential-file', str(credential),
                        '--model', 'synthetic-model', '--out', str(out_file)], client_factory=factory) == 0
    report = json.loads(out_file.read_text())
    assert (out_file.stat().st_mode & 0o777) == 0o600
    claim, handoff = report['sets']['claim_review'], report['sets']['handoff']
    assert claim['decisions'] == {'pass': 1} and claim['failure_rate'] == 0 and claim['questions']['grounded']['distribution'] == {'0.9': 1}
    assert handoff['decisions'] == {'uncertain': 1} and handoff['failure_rate'] == 1 and handoff['failures'] == {'http_5xx': 1}
    assert handoff['questions']['needs_human']['shares'] == {'uncertain': 1.0}
    assert SYNTHETIC_CREDENTIAL not in out_file.read_text()
