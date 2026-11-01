"""K4b (D1, Q-2): the trusted human-question kinds and the workbench role manifest agree. A kind only the owner may answer is one whose
Actions are in the owner-only set of workbench-roles; a kind the operator may answer is one whose Actions the operator role holds. The
question kinds are the contract's (run-outcome 1.1). Pure file reading."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KINDS = json.loads((ROOT / 'deploy/configuration/human-question-kinds.v1.json').read_text())
ROLES = json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text())
CONTRACT = json.loads((ROOT / 'packages/contracts/run-outcome.schema.json').read_text())
# The workbench Actions each kind of question is about (other: anything unclassified, owner by default).
KIND_ACTIONS = {
    'schedule_change': ['Commitment.extend'],
    'fulfilment_confirm': ['Commitment.attest', 'Commitment.condition_met'],
    'customer_fact': ['nexloop.contact.read', 'nexloop.conversation.takeover'],
    'budget': ['nexloop.budget.loosen', 'nexloop.incentive.issue'],
    'contact_release': ['Contact.release'],
    'erasure': ['nexloop.consumer.erase', 'nexloop.message.erase'],
    'other': [],
}


def resource(name): return f'eios:action:{name}:1'


def test_the_kinds_are_the_contract_kinds_and_roles_are_owner_or_operator():
    assert list(KINDS['kinds']) == CONTRACT['properties']['question']['properties']['question_kind']['enum'] == list(KIND_ACTIONS)
    assert all(roles in (['owner'], ['owner', 'operator']) for roles in KINDS['kinds'].values())
    assert KINDS['expire_after_seconds'] == 7 * 24 * 3600 and KINDS['schema'] == 'nexloop-human-question-kinds/1' and KINDS['version'] == 1


def test_owner_only_kinds_match_the_owner_only_set_of_the_roles_manifest():
    owner, operator, reviewer = (set(ROLES['roles'][r]) for r in ('owner', 'operator', 'reviewer'))
    owner_only = owner - operator - reviewer
    for kind, roles in KINDS['kinds'].items():
        actions = {resource(a) for a in KIND_ACTIONS[kind]}
        if roles == ['owner']:
            assert actions <= owner_only, kind
        else:
            assert actions and actions <= operator, kind
    # The answer Action itself is held by owner and operator, never the reviewer (D6); the handler narrows by kind.
    answer = resource('nexloop.human_question.answer')
    assert answer in owner and answer in operator and answer not in reviewer and ROLES['slices']['human_questions'] == ['nexloop.human_question.answer']


def test_a_question_audience_names_only_roles_that_hold_the_answer_action():
    audience = CONTRACT['properties']['question']['properties']['audience']['enum']
    assert all(resource('nexloop.human_question.answer') in ROLES['roles'][role] for role in audience)
