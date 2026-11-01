"""K4a (owner 2026-11-01, dsh intake §3.3, Q-2): run-outcome 1.1 adds an optional question for escalate and needs_information;
1.0 is unchanged. workbench-pending v1 is the home 'waiting for me' read (no alert acknowledgement)."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1]/'packages/contracts'
SCHEMA = json.loads((ROOT/'run-outcome.schema.json').read_text())
V = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
V10 = json.loads((ROOT/'examples/run-outcome.valid.json').read_text())
V11 = json.loads((ROOT/'examples/run-outcome.question.valid.json').read_text())
KINDS = ['schedule_change', 'fulfilment_confirm', 'customer_fact', 'budget', 'contact_release', 'erasure', 'other']
# sha256 of the canonical JSON of run-outcome as released before K4a (main 50415427).
RUN_OUTCOME_1_0 = 'af61636030763035b91293aa8ed6dedcab37a9b61ef0dec8ce8763613dd01b3c'


def canonical(value): return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def test_the_1_0_contract_is_byte_for_byte_the_released_one_once_1_1_is_taken_away():
    schema = copy.deepcopy(SCHEMA)
    assert schema['allOf'].pop() == {'if': {'required': ['question']}, 'then': {'properties': {
        'schema_version': {'const': '1.1'}, 'kind': {'enum': ['escalate', 'needs_information']}}}}
    del schema['properties']['question']
    assert schema['properties']['schema_version'] == {'enum': ['1.0', '1.1']}
    schema['properties']['schema_version'] = {'const': '1.0'}
    schema['description'] = schema['description'].replace(schema['description'][schema['description'].index('Version 1.1'):schema['description'].index('Identity (tenant')], '')
    assert canonical(schema) == RUN_OUTCOME_1_0
    assert 'question' not in SCHEMA['required']


def test_both_versions_validate_and_1_0_never_carries_a_question():
    V.validate(V10); V.validate(V11)
    V.validate(V11 | {'schema_version': '1.1', 'kind': 'needs_information'})
    V.validate({k: v for k, v in V11.items() if k != 'question'})  # 1.1 without a question
    with pytest.raises(ValidationError): V.validate(V11 | {'schema_version': '1.0'})
    with pytest.raises(ValidationError): V.validate(V11 | {'schema_version': '1.2'})


@pytest.mark.parametrize('kind', ['no_action', 'waiting_external', 'action_intent', 'plan_update'])
def test_only_escalate_and_needs_information_may_ask(kind):
    forged = copy.deepcopy(V11) | {'kind': kind}
    if kind == 'waiting_external': forged['reassess_at'] = '2026-11-02T00:00:00Z'
    if kind == 'action_intent': forged['intent_ref'] = '7d1f0c3a-2b4e-4c5d-8e9f-0a1b2c3d4e5f'
    if kind == 'plan_update': forged['plan_update'] = V10['plan_update']
    V.validate({k: v for k, v in forged.items() if k != 'question'})
    with pytest.raises(ValidationError): V.validate(forged)


@pytest.mark.parametrize('change', [
    {'audience': 'reviewer'}, {'question_kind': 'refund'}, {'text': ''}, {'text': '问' * 301},
    {'options': ['a', 'b', 'c', 'd', 'e']}, {'options': ['a', 'a']}, {'options': ['']}, {'options': ['x' * 61]},
    {'subject_ref': 'no reference'}, {'withdrawable': 'yes'}, {'answer_roles': ['operator']}])
def test_a_malformed_question_is_refused(change):
    forged = copy.deepcopy(V11); forged['question'].update(change)
    with pytest.raises(ValidationError): V.validate(forged)


@pytest.mark.parametrize('field', ['audience', 'question_kind', 'text', 'options', 'subject_ref', 'withdrawable'])
def test_every_question_field_is_required(field):
    forged = copy.deepcopy(V11); del forged['question'][field]
    with pytest.raises(ValidationError): V.validate(forged)


def test_the_kinds_and_bounds():
    question = SCHEMA['properties']['question']['properties']
    assert question['question_kind']['enum'] == KINDS and question['audience']['enum'] == ['owner', 'operator']
    assert question['text']['maxLength'] == 300 and question['options']['maxItems'] == 4
    V.validate(copy.deepcopy(V11) | {'question': V11['question'] | {'options': [], 'subject_ref': None}})


PENDING = json.loads((ROOT/'workbench-pending.schema.json').read_text())
P = Draft202012Validator(PENDING, format_checker=FormatChecker())
PENDING_EXAMPLE = json.loads((ROOT/'examples/workbench-pending.valid.json').read_text())


def test_pending_derives_its_question_shape_from_run_outcome():
    item = PENDING['properties']['questions']['items']['properties']
    question = SCHEMA['properties']['question']['properties']
    for field in ('audience', 'question_kind', 'options', 'subject_ref', 'withdrawable'): assert item[field] == question[field]
    assert item['text']['maxLength'] == 120 and PENDING['properties']['questions']['maxItems'] == 20
    assert item['answer']['properties']['operation'] == {'const': 'nexloop.human_question.answer:1'}


def test_pending_categories_are_the_six_and_no_alert_acknowledgement():
    assert PENDING['properties']['categories']['items']['properties']['category']['enum'] == [
        'question', 'approval', 'remediation', 'reply_escalation', 'takeover_expiry', 'candidate_review']
    P.validate(PENDING_EXAMPLE)
    for change in ({'category': 'alert_ack'}, {'count': -1}, {'oldest_waiting_seconds': 1.5}, {'oldest_ref': 'x'}):
        forged = copy.deepcopy(PENDING_EXAMPLE); forged['categories'][1].update(change)
        with pytest.raises(ValidationError): P.validate(forged)
    forged = copy.deepcopy(PENDING_EXAMPLE); forged['categories'].pop()
    with pytest.raises(ValidationError): P.validate(forged)


def test_pending_questions_are_bounded_and_answer_names_one_operation():
    forged = copy.deepcopy(PENDING_EXAMPLE); forged['questions'] = forged['questions'] * 21
    with pytest.raises(ValidationError): P.validate(forged)
    for change in ({'text': '问' * 121}, {'answer': {'operation': 'nexloop.approval.decide:1'}}, {'answer': {}}, {'consumer_name': 'x'}):
        forged = copy.deepcopy(PENDING_EXAMPLE); forged['questions'][0].update(change)
        with pytest.raises(ValidationError): P.validate(forged)
