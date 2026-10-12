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
            'duplicate_deliveries': 1, 'duplicate_fulfilled': 0, 'cross_consumer_reads': 0, 'critical_alerts': 0}
    return {**base, **over}


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
