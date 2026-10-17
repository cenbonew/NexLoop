"""NX-037 RealDriver without a database: it prompts the operator, polls the observer until the interaction's Run is terminal
with no open effect, and maps the observed records to the stop-rule facts; no answer in time is a pause. The observer here is
a stand-in returning the shape of authz.nexloop_nx037_interaction_observation (0172); the end-to-end run is
tests/test_nx037_real_driver_pg.py."""
import pytest

from nexloop_eios import controlled_interactions as X
from nexloop_eios import interaction_drivers as D

INTERACTION = {'seq': 1, 'kind': 'ordinary_reply', 'interaction_id': '37d00000-0000-4000-8000-000000000001'}
RUN = '37d00000-0000-4000-8000-0000000000aa'


def seen(**over):
    base = {'message_id': 'm' * 64, 'run_id': RUN, 'task_status': 'succeeded', 'run_terminal': True, 'intents': {'fulfilled': 1}, 'intents_open': 0,
            'intent_ids': ['i-1'], 'outbound_ids': ['o' * 64], 'unauthorized_sends': 0, 'wrong_recipient_sends': 0, 'contacts_after_refusal': 0,
            'duplicate_deliveries': 1, 'duplicate_fulfilled': 0, 'cross_consumer_reads': 0, 'critical_alerts': 0,
            'effects': {'nexloop.service.request': {'states': {'fulfilled': 1}, 'unknown_seen': 0}}}
    return {**base, **over}


def deliver(states, unknown_seen=0):
    """0203 effects of a Run that replied and asked the enterprise to deliver (intent states of nexloop.enterprise.deliver)."""
    return {'nexloop.service.request': {'states': {'fulfilled': 1}, 'unknown_seen': 0},
            'nexloop.enterprise.deliver': {'states': states, 'unknown_seen': unknown_seen}}


class Observer:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def now(self):
        return 'T0'

    def observe(self, since):
        assert since == 'T0'
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Operator:
    def __init__(self):
        self.prompts = []

    def prompt(self, interaction, instruction, deadline_seconds):
        self.prompts.append((interaction['seq'], instruction, deadline_seconds))


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def driver(observer, timeout=10):
    clock = Clock()
    config = type('Config', (), {})()
    return D.RealDriver(config, observer=observer, operator=Operator(), timeout_seconds=timeout, poll_seconds=1, sleep=clock.sleep, monotonic=clock.monotonic)


def test_waits_for_a_terminal_run_with_settled_effects_and_maps_the_facts():
    observer = Observer({'message_id': None}, seen(run_terminal=False, task_status='running'), seen(intents={'observed_fulfilled': 1}, intents_open=1), seen())
    d = driver(observer)
    out = d.interact(INTERACTION)
    assert observer.calls == 4 and d.operator.prompts == [(1, D.INSTRUCTIONS['ordinary_reply'], 10)]
    assert out['run_id'] == RUN and out['outcome'] == 'ok' and out['reconciliation'] == 'settled' and out['receipts'] == ['o' * 64, 'i-1']
    assert out['facts'] == {'unauthorized_sends': 0, 'wrong_recipient_sends': 0, 'contacts_after_refusal': 0, 'max_executions_per_intent': 1,
                            'cross_tenant_reads': 0, 'critical_alerts': 0, 'unresolved_unknown_effects': 0}


@pytest.mark.parametrize('field,rule', [('unauthorized_sends', 'unauthorized_send'), ('wrong_recipient_sends', 'wrong_recipient'),
                                        ('contacts_after_refusal', 'contact_after_refusal'), ('duplicate_deliveries', 'duplicate_external_effect'),
                                        ('duplicate_fulfilled', 'duplicate_external_effect'), ('cross_consumer_reads', 'cross_tenant_read'),
                                        ('critical_alerts', 'critical_alert')])
def test_each_observed_violation_reaches_its_stop_rule(field, rule):
    value = 2 if field == 'duplicate_deliveries' else 1
    out = driver(Observer(seen(**{field: value}))).interact(INTERACTION)
    record = {'facts': out['facts'], 'cost': {'total': '0'}, 'outcome': out['outcome']}
    plan = X.load_plan(X.Path(__file__).resolve().parents[1] / 'deploy/configuration/nx037-trial.v1.json')
    decision, rules = X.evaluate(record, [], plan, cumulative=X.Decimal(0), monthly=X.Decimal(0))
    assert decision == 'stop' and rules == [rule]


def test_unknown_effect_failed_run_and_timeout():
    unknown = driver(Observer(seen(intents={'unknown': 1}))).interact(INTERACTION)
    assert unknown['reconciliation'] == 'unresolved' and unknown['facts']['unresolved_unknown_effects'] == 1
    failed = driver(Observer(seen(task_status='failed', intents={}, intent_ids=[]))).interact(INTERACTION)
    assert failed['outcome'] == 'error' and failed['error_code'] == 'run_failed' and failed['reconciliation'] == 'not_applicable'
    late = driver(Observer({'message_id': None, 'critical_alerts': 0}), timeout=5).interact(INTERACTION)
    assert late == {'run_id': None, 'outcome': 'error', 'error_code': 'interaction_timeout', 'receipts': [], 'reconciliation': 'not_applicable',
                    'facts': {'interaction_timeout': 1, 'critical_alerts': 0}}
    record = {'facts': late['facts'], 'cost': {'total': '0'}, 'outcome': 'error'}
    plan = X.load_plan(X.Path(__file__).resolve().parents[1] / 'deploy/configuration/nx037-trial.v1.json')
    assert X.evaluate(record, [], plan, cumulative=X.Decimal(0), monthly=X.Decimal(0)) == ('pause', ['interaction_timeout'])


def test_console_operator_writes_codes_only(tmp_path):
    import io
    stream = io.StringIO()
    D.ConsoleOperator(tmp_path, stream).prompt(INTERACTION, D.INSTRUCTIONS['human_takeover'], 900)
    assert '第 1 次交互' in stream.getvalue() and (tmp_path / 'prompts.jsonl').read_text().strip() == (
        '{"seq": 1, "interaction_id": "37d00000-0000-4000-8000-000000000001", "kind": "ordinary_reply"}')
    with pytest.raises(ValueError):
        D.RealDriver(None, observer=None, operator=None, timeout_seconds=0)


class MultiObserver:
    """The 0196 message list plus each message's 0172 observation (by its acceptance time)."""

    def __init__(self, views, senders=None):
        self.views = views  # [(message_id, accepted_at, observation)]
        self.senders = senders or {}  # message_id -> consumer id (0207); default: the planned account 'a' * 64

    def now(self):
        return 'T0'

    def messages(self, since):
        assert since == 'T0'
        return [{'message_id': m, 'accepted_at': at, 'consumer_id': self.senders.get(m, 'a' * 64)} for m, at, _ in self.views]

    def observe(self, since):
        return next(v for _, at, v in self.views if at == since)


def turn(n, **over):
    return (str(n) * 64, f'T{n}', seen(message_id=str(n) * 64, run_id=f'37d00000-0000-4000-8000-0000000000{n:02d}', intent_ids=[f'i-{n}'],
                                       outbound_ids=[str(n) * 63 + 'o'], **over))


def test_a_long_conversation_is_judged_on_every_turn():
    """Plan v3 long_conversation: every customer message of the interaction and its Run belong to it."""
    out = driver(MultiObserver([turn(1), turn(2), turn(3)])).interact({**INTERACTION, 'variant': 'long_conversation', 'consumer': 'consumer:' + 'a' * 64})
    assert out['outcome'] == 'ok' and out['messages'] == 3 and out['run_ids'] == [f'37d00000-0000-4000-8000-0000000000{n:02d}' for n in (1, 2, 3)]
    assert out['run_id'] == out['run_ids'][0] and out['receipts'] == [r for n in (1, 2, 3) for r in (str(n) * 63 + 'o', f'i-{n}')]


def test_a_failed_middle_turn_makes_the_interaction_an_error():
    out = driver(MultiObserver([turn(1), turn(2, task_status='failed'), turn(3)])).interact(INTERACTION)
    assert out['outcome'] == 'error' and out['error_code'] == 'run_failed' and len(out['run_ids']) == 3


def test_a_contact_after_refusal_in_a_later_turn_stops_the_trial():
    out = driver(MultiObserver([turn(1), turn(2), turn(3, contacts_after_refusal=1)])).interact(INTERACTION)
    assert out['facts']['contacts_after_refusal'] == 1
    plan = X.load_plan(X.Path(__file__).resolve().parents[1] / 'deploy/configuration/nx037-trial.v3.json')
    assert X.evaluate({'facts': out['facts'], 'cost': {'total': '0'}, 'outcome': out['outcome']}, [], plan,
                      cumulative=X.Decimal(0), monthly=X.Decimal(0)) == ('stop', ['contact_after_refusal'])


def test_a_turn_still_open_keeps_the_interaction_waiting_and_a_taken_over_message_settles_on_its_staff_reply():
    waiting = driver(MultiObserver([turn(1), turn(2, run_terminal=False, task_status='running')]), timeout=3).interact(INTERACTION)
    assert waiting['error_code'] == 'interaction_timeout'
    during = ('9' * 64, 'T9', seen(message_id='9' * 64, run_id=None, task_status=None, run_terminal=False, intents={}, intents_open=0,
                                   intent_ids=[], outbound_ids=['s' * 64]))
    out = driver(MultiObserver([turn(1), during])).interact(INTERACTION)
    assert out['outcome'] == 'ok' and out['run_ids'] == ['37d00000-0000-4000-8000-000000000001'] and out['messages'] == 2
    unanswered = (during[0], during[1], {**during[2], 'outbound_ids': []})
    assert driver(MultiObserver([turn(1), unanswered]), timeout=3).interact(INTERACTION)['error_code'] == 'interaction_timeout'


def test_the_instruction_carries_the_variant_and_the_account():
    text = D.instruction({**INTERACTION, 'variant': 'repeated_refusal', 'consumer': 'consumer:' + 'r' * 64})
    assert text.startswith(D.INSTRUCTIONS['ordinary_reply']) and D.VARIANTS['repeated_refusal'] in text and 'consumer:' + 'r' * 64 in text


def test_the_console_operator_waits_for_the_done_mark(tmp_path):
    import io
    clock = Clock()
    operator = D.ConsoleOperator(tmp_path, io.StringIO(), wait=True, sleep=clock.sleep, monotonic=clock.monotonic)
    assert operator.prompt(INTERACTION, 'x', 5) is False
    assert clock.t == 5  # not marked: waited until the deadline
    (tmp_path / 'operator-done' / '1').write_text('')
    clock.t = 0
    assert operator.prompt(INTERACTION, 'x', 5) is True
    assert clock.t == 0


class _Unmarked:
    """The agent playing the customer sent its message but never created the done marker before the deadline."""

    def __init__(self):
        self.prompts = []

    def prompt(self, interaction, instruction, deadline_seconds):
        self.prompts.append(interaction['seq'])
        return False


def test_no_done_marker_by_the_deadline_is_interaction_timeout_even_when_the_turns_settled():
    """10/17 calibration (trial e80e360c): the operator waited out the 900 s without the marker; the runner then judged the
    settled turn. It is a timeout pause, never a verdict."""
    d = D.RealDriver(type('Config', (), {})(), observer=Observer(seen()), operator=_Unmarked(), timeout_seconds=10, poll_seconds=1,
                     sleep=lambda s: None, monotonic=lambda: 0)
    got = d.interact(INTERACTION)
    assert got['outcome'] == 'error' and got['error_code'] == 'interaction_timeout' and got['facts']['interaction_timeout'] == 1


@pytest.mark.parametrize('kind,effects,code', [
    ('real_delivery', {'nexloop.service.request': {'states': {'fulfilled': 1}, 'unknown_seen': 0}}, 'delivery_missing'),
    ('real_delivery', deliver({'failed': 1}), 'delivery_missing'),
    ('real_delivery', deliver({'fulfilled': 1}), None),
    ('unknown_reconciliation', deliver({'fulfilled': 1}), 'reconciliation_missing'),
    ('unknown_reconciliation', {'nexloop.service.request': {'states': {'fulfilled': 1}, 'unknown_seen': 0}}, 'reconciliation_missing'),
    ('unknown_reconciliation', deliver({'fulfilled': 1}, unknown_seen=1), None),
    ('ordinary_reply', deliver({'fulfilled': 1}), 'unexpected_delivery'),
    ('ordinary_reply', {'nexloop.service.request': {'states': {'fulfilled': 1}, 'unknown_seen': 0}}, None),
    ('real_delivery', None, 'effect_evidence_unavailable'),
])
def test_each_kind_requires_its_effect_evidence(kind, effects, code):
    """10/17 calibration: a real_delivery whose Run only replied (no enterprise delivery intent) was recorded ok. Each kind's
    effect evidence is required; missing, the interaction is an error with a named code and the trial pauses."""
    view = seen(effects=effects) if effects is not None else {k: v for k, v in seen().items() if k != 'effects'}
    got = driver(Observer(view)).interact({**INTERACTION, 'kind': kind})
    if code is None:
        assert got['outcome'] == 'ok' and got['error_code'] is None and 'effect_evidence_missing' not in got['facts']
    else:
        assert got['outcome'] == 'error' and got['error_code'] == code and got['facts']['effect_evidence_missing'] == 1
        assert 'effect_evidence_missing' in X.PAUSE_RULES


def test_the_prompt_line_tells_the_agent_what_to_play_and_where_to_mark_done(tmp_path):
    import io
    import json
    clock = Clock()
    operator = D.ConsoleOperator(tmp_path, io.StringIO(), wait=True, sleep=clock.sleep, monotonic=clock.monotonic)
    (tmp_path / 'operator-done').mkdir()
    (tmp_path / 'operator-done' / '7').write_text('')
    item = {**INTERACTION, 'seq': 7, 'kind': 'restricted_after_refusal', 'variant': 'repeated_refusal', 'consumer': 'consumer:' + 'r' * 64}
    operator.prompt(item, D.instruction(item), 900)
    line = json.loads((tmp_path / 'prompts.jsonl').read_text().strip())
    assert line == {'seq': 7, 'interaction_id': INTERACTION['interaction_id'], 'kind': 'restricted_after_refusal', 'variant': 'repeated_refusal',
                    'consumer': 'consumer:' + 'r' * 64, 'done_file': str(tmp_path / 'operator-done' / '7'), 'deadline_seconds': 900}


def test_a_message_from_an_account_outside_the_plan_makes_the_interaction_an_error():
    """10/18 trial 4f5d795c: interaction 1 planned customer-1; customer-2 wrote first. Every message's sender is checked (0207):
    the other account's message is counted (offplan_messages) and the interaction is an error (offplan_sender), never dropped."""
    planned = {**INTERACTION, 'variant': 'long_conversation', 'consumer': 'consumer:' + 'a' * 64}
    first, second = turn(1), turn(2)
    out = driver(MultiObserver([first, second], senders={first[0]: 'b' * 64})).interact(planned)
    assert out['outcome'] == 'error' and out['error_code'] == 'offplan_sender' and out['facts']['offplan_messages'] == 1
    assert out['messages'] == 1 and out['run_ids'] == [second[2]['run_id']]  # the planned account's own turn is still judged
    # Only the other account wrote: still an error, not a wait for a message that will never come.
    out = driver(MultiObserver([first], senders={first[0]: 'b' * 64})).interact(planned)
    assert out['error_code'] == 'offplan_sender' and out['facts']['offplan_messages'] == 1 and out['messages'] == 0
    # Without a per-interaction consumer the planned accounts of the trial are the allowed senders.
    d = driver(MultiObserver([first, second], senders={first[0]: 'b' * 64}));d.consumers = frozenset({'a' * 64})
    assert d.interact(INTERACTION)['error_code'] == 'offplan_sender'
    d = driver(MultiObserver([first, second]));d.consumers = frozenset({'a' * 64})
    assert d.interact(INTERACTION)['outcome'] == 'ok'


def test_offplan_sender_is_in_the_evidence_vocabulary():
    import sys
    sys.path.insert(0, str(X.Path(__file__).resolve().parents[1] / 'scripts/nx037'))
    import evidence_pack as E
    assert E.allowed('offplan_sender') and E.violations({'error_code': 'offplan_sender', 'facts': {'offplan_messages': 1}}) == []
