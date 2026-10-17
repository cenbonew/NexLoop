"""NX-037 A-class prep: the controlled interaction executor in the synthetic environment (deterministic provider, loopback
channel; tests/support/nx037_synthetic.py) and the evidence directory. One clean run of the D-i plan; every stop rule and
every pause rule triggered by an injected violation; resume after a pause; the evidence pack holds only allowed values."""
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nexloop_eios import controlled_interactions as X
from support.nx037_synthetic import SyntheticCollector, SyntheticDriver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import evidence_pack as E  # noqa: E402

PLAN = ROOT / 'deploy/configuration/nx037-trial.v1.json'
TRIAL = '37a00000-0000-4000-8000-000000000001'


class Clock:
    """Deterministic time: each reading advances 400 ms."""

    def __init__(self):
        self.now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(milliseconds=400)
        return self.now


def execute(tmp_path, *, inject=None, plan=None, name='trial', **kw):
    driver = SyntheticDriver(inject=inject)
    state = X.run(plan or X.load_plan(PLAN), driver, SyntheticCollector(driver), tmp_path / name, trial_id=TRIAL, clock=Clock(), **kw)
    return state, X.read_records(tmp_path / name), driver


def test_plan_carries_the_d_g_and_d_i_values_and_refuses_bad_ones():
    plan = X.load_plan(PLAN)
    assert plan['cost_caps'] == {'cumulative': Decimal(100), 'per_run': Decimal(2), 'tenant_monthly': Decimal(300)} and plan['currency'] == 'CNY'
    assert plan['mix'] == {'ordinary_reply': 15, 'real_delivery': 4, 'restricted_after_refusal': 4, 'human_takeover': 4, 'unknown_reconciliation': 3}
    assert plan['total'] == 30 and plan['error_window'] == 10 and plan['calibration'] == 3
    order = X.schedule(plan, trial_id=TRIAL)
    assert [i['seq'] for i in order] == list(range(1, 31)) and len({i['interaction_id'] for i in order}) == 30
    assert {k: sum(1 for i in order if i['kind'] == k) for k in X.KINDS} == plan['mix']
    assert set(i['kind'] for i in order[:10]) == set(X.KINDS)  # every kind early, not in blocks
    assert order == X.schedule(plan, trial_id=TRIAL)  # stable ids across restarts
    raw = json.loads(PLAN.read_text())
    for bad in ({**raw, 'cost_caps': {**raw['cost_caps'], 'per_run': '0'}}, {**raw, 'cost_caps': {**raw['cost_caps'], 'per_run': '200'}},
                {**raw, 'plan': {**raw['plan'], 'total': 31}}, {**raw, 'stop': raw['stop'][:-1]}, {**raw, 'schema': 'other'}):
        with pytest.raises(X.PlanInvalid):
            X.validate_plan(bad)


def test_a_clean_synthetic_run_completes_and_records_every_interaction(tmp_path):
    state, records, driver = execute(tmp_path)
    assert state['status'] == 'completed' and state['recorded'] == 30 and state['rules'] == []
    assert [r['seq'] for r in records] == list(range(1, 31)) and all(r['decision'] == {'action': 'continue', 'rules': []} for r in records)
    first = records[0]
    assert first['time_ms']['end_to_end'] == 400 and first['time_ms']['model'] == 120
    assert first['time_ms']['platform'] == 400 - 120 - first['time_ms']['tool']
    delivery = next(r for r in records if r['kind'] == 'real_delivery')
    assert delivery['intents'] == {'fulfilled': 1} and len(delivery['receipts']) == 2 and delivery['tool_calls'] == 1
    unknown = [r for r in records if r['kind'] == 'unknown_reconciliation']
    assert len(unknown) == 3 and all(r['reconciliation'] == 'settled' for r in unknown)
    assert all(not r['receipts'] for r in records if r['kind'] in ('restricted_after_refusal', 'human_takeover'))  # nothing sent
    assert Decimal(state['cumulative_cost']) == sum(Decimal(r['cost']['total']) for r in records) < Decimal(80)
    assert all(n == 1 for n in driver.channel.executions.values())
    # Rerunning a completed trial runs nothing again.
    again, records_again, driver_again = execute(tmp_path)
    assert again['recorded'] == 30 and records_again == records and driver_again.provider.calls == []


STOPS = [('unauthorized_send', 'human_takeover', 'unauthorized_send'), ('wrong_recipient', 'ordinary_reply', 'wrong_recipient'),
         ('contact_after_refusal', 'restricted_after_refusal', 'contact_after_refusal'),
         ('duplicate_external_effect', 'real_delivery', 'duplicate_external_effect'), ('cross_tenant_read', 'ordinary_reply', 'cross_tenant_read'),
         ('per_run_cost', 'ordinary_reply', 'per_run_cost_exceeded'), ('critical_alert', 'ordinary_reply', 'critical_alert')]


@pytest.mark.parametrize('fault,kind,rule', STOPS)
def test_each_stop_rule_stops_at_the_first_occurrence(tmp_path, fault, kind, rule):
    seq = next(i['seq'] for i in X.schedule(X.load_plan(PLAN), trial_id=TRIAL) if i['kind'] == kind and i['seq'] > 3)
    state, records, _ = execute(tmp_path, inject={seq: fault})
    assert state['status'] == 'stopped' and state['rules'] == [rule] and state['at_seq'] == seq
    assert len(records) == seq and records[-1]['decision'] == {'action': 'stop', 'rules': [rule]}  # the evidence is written first
    with pytest.raises(RuntimeError, match='trial stopped'):
        execute(tmp_path)  # a stopped trial never resumes


def test_owner_stop(tmp_path):
    calls = iter([False] * 7 + [True] * 100)
    state, records, _ = execute(tmp_path, owner_stop=lambda: next(calls))
    assert state['status'] == 'stopped' and state['rules'] == ['owner_stop'] and len(records) <= 4


def test_error_rate_pauses_and_resume_continues_without_rerunning(tmp_path):
    state, records, _ = execute(tmp_path, inject={5: 'error'})
    assert state['status'] == 'completed'  # one error in ten is 10%, not above
    state, records, _ = execute(tmp_path, inject={5: 'error', 9: 'error'}, name='two')
    assert state['status'] == 'paused' and state['rules'] == ['error_rate'] and state['at_seq'] == 9
    with pytest.raises(X.TrialPaused):
        execute(tmp_path, name='two')  # a pause needs the owner's explicit resume
    resumed, records, driver = execute(tmp_path, name='two', resume='owner-confirmation:1')
    assert driver.provider.calls and len(driver.provider.calls) == 21 and resumed['status'] == 'completed'
    assert [r['seq'] for r in records] == list(range(1, 31))


def test_cost_pauses_at_80_percent_of_the_cumulative_and_the_monthly_cap(tmp_path):
    plan = X.load_plan(PLAN, cost_caps={'cumulative': '1', 'per_run': '0.5', 'tenant_monthly': '300'})
    state, records, _ = execute(tmp_path, plan=plan)
    assert state['status'] == 'paused' and state['rules'] == ['cumulative_cost']
    assert Decimal(state['cumulative_cost']) >= Decimal('0.8') and sum(Decimal(r['cost']['total']) for r in records[:-1]) < Decimal('0.8')
    state, _, _ = execute(tmp_path, name='month', month_cost_before=Decimal('239.95'))
    assert state['status'] == 'paused' and state['rules'] == ['monthly_cost'] and state['at_seq'] == 1


def test_an_unresolved_unknown_effect_pauses(tmp_path):
    seq = next(i['seq'] for i in X.schedule(X.load_plan(PLAN), trial_id=TRIAL) if i['kind'] == 'unknown_reconciliation')
    state, records, driver = execute(tmp_path, inject={seq: 'unresolved_unknown_effect'})
    assert state['status'] == 'paused' and state['rules'] == ['unresolved_unknown_effect'] and records[-1]['intents'] == {'unknown': 1}
    assert all(n == 1 for n in driver.channel.executions.values())  # never resent


def test_evidence_pack_holds_only_ids_hashes_numbers_and_times(tmp_path):
    execute(tmp_path, inject={5: 'error'})
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'release_id': '20261012T020000Z-' + 'a' * 12, 'commit': 'a' * 40, 'tree': 'b' * 40, 'ci_result_digest': 'c' * 64,
                                    'contracts_digest': 'd' * 64, 'catalog': {'head': '0280', 'digest': 'e' * 64, 'note': '本地路径 /home/x'},
                                    'locks': {'versions.lock.json': 'f' * 64}, 'images': [{'name': 'nexloop-api', 'digest': 'sha256:' + '1' * 64,
                                    'archive_sha256': '2' * 64, 'reference': 'registry.invalid/nexloop-api@sha256:' + '1' * 64}]}))
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'host': 'app-host-name', 'user': 'someone'}))
    notices = tmp_path / 'THIRD_PARTY_NOTICES.md'
    notices.write_text('# notices\n')
    out = tmp_path / 'pack'
    assert E.main(['--out', str(out), '--trial', str(tmp_path / 'trial'), '--item', f'release_manifest={manifest}',
                   '--item', f'inventory_before={inventory}', '--item', f'third_party_notices={notices}']) == 0
    index = json.loads((out / 'index.json').read_text())
    assert index['items']['inventory_after'] == {'status': 'missing'} and index['items']['inventory_before']['status'] == 'present'
    assert index['items']['interactions']['records'] == 30 and index['items']['interactions']['dropped_values'] == 0
    assert not (out / 'inventory_before.json').exists()  # content that may name hosts is never copied
    release = json.loads((out / 'release.json').read_text())
    assert release['commit'] == 'a' * 40 and release['catalog'] == {'digest': 'e' * 64, 'head': '0280', 'note': None}
    assert release['images'] == [{'digest': 'sha256:' + '1' * 64, 'archive_sha256': '2' * 64}]
    summary = json.loads((out / 'interactions' / 'summary.json').read_text())
    assert summary['status'] == 'completed' and summary['by_outcome'] == {'ok': 29, 'error': 1} and summary['by_kind']['ordinary_reply'] == 15
    assert summary['time_ms']['model']['p50'] == 120
    recon = json.loads((out / 'interactions' / 'reconciliation.json').read_text())
    assert len(recon) == 7 and sum(1 for v in recon.values() if v['reconciliation'] == 'settled') == 3
    assert E.check(out) == []
    text = ''.join(p.read_text() for p in out.rglob('*.json'))
    assert 'app-host-name' not in text and 'someone' not in text and '/home' not in text and 'registry.invalid' not in text
    # Anything else is refused: a free-text value written into a pack is found by the check.
    (out / 'extra.json').write_text(json.dumps({'note': 'free text'}))
    assert E.check(out) == ['extra.json']
    assert E.main(['--out', str(out)]) == 2  # never into an existing directory
    with pytest.raises(ValueError):
        E._write(tmp_path / 'x.json', {'host': 'app-host-name'})


def test_costs_are_kept_per_currency_and_a_foreign_currency_stops(tmp_path):
    """D-g option 2: no conversion. A cost in the trial currency counts; a non-zero cost in another currency is recorded apart
    and stops the trial (currency_mismatch); a zero foreign amount is harmless."""
    from nexloop_eios.interaction_drivers import SyntheticCollector

    class Foreign(SyntheticCollector):
        def __init__(self, driver, amount):
            super().__init__(driver)
            self.amount = amount

        def collect(self, run_id):
            evidence = dict(super().collect(run_id))
            evidence['costs'] = {**evidence.get('costs', {}), 'model_usd': {'amount': self.amount, 'currencies': ['USD']}}
            return evidence

    plan = X.load_plan(PLAN)
    assert plan['currency'] == 'CNY'
    driver = SyntheticDriver()
    state = X.run(plan, driver, Foreign(driver, '0'), tmp_path / 'zero', trial_id=TRIAL, clock=Clock())
    assert state['status'] == 'completed'
    assert all('other_currencies' not in r['cost'] for r in X.read_records(tmp_path / 'zero'))
    driver = SyntheticDriver()
    state = X.run(plan, driver, Foreign(driver, '0.01'), tmp_path / 'usd', trial_id=TRIAL, clock=Clock())
    record = X.read_records(tmp_path / 'usd')[-1]
    assert state['status'] == 'stopped' and 'currency_mismatch' in state['rules'] and state['at_seq'] == 1
    assert record['cost']['other_currencies'] == {'model_usd': {'amount': '0.01', 'currencies': ['USD']}}
    assert 'model_usd' not in record['cost']['by_kind']


def test_the_owner_voids_the_last_record_and_it_runs_again_without_editing_the_evidence(tmp_path):
    """10/17 calibration (trial e80e360c): a record judged before its effect evidence was required is withdrawn by the owner:
    voids.jsonl names it, interactions.jsonl keeps every line, read_records leaves it out, and that interaction runs again."""
    state, records, _ = execute(tmp_path, inject={2: 'error'}, plan=X.load_plan(PLAN), calibration_pause=True)
    assert state['status'] == 'paused' and state['at_seq'] == 3 and [r['seq'] for r in records] == [1, 2, 3]
    raw = (tmp_path / 'trial' / 'interactions.jsonl').read_text()
    for bad in ((2, 'owner-void:1'), (3, 'bad ref with spaces')):  # only the last record, only with a confirmation reference
        with pytest.raises(ValueError):
            execute(tmp_path, resume='owner-confirmation:1', void=bad)
    assert not (tmp_path / 'trial' / 'voids.jsonl').exists() and not (tmp_path / 'trial' / 'resumes.jsonl').exists()
    with pytest.raises(X.TrialPaused):
        execute(tmp_path, void=(3, 'owner-void:1'))  # a paused trial still needs the owner's resume
    resumed, records, _ = execute(tmp_path, resume='owner-confirmation:1', void=(3, 'owner-void:1'))
    voids = X.read_voids(tmp_path / 'trial')
    assert len(voids) == 1 and voids[0]['seq'] == 3 and voids[0]['confirmation_ref'] == 'owner-void:1'
    lines = (tmp_path / 'trial' / 'interactions.jsonl').read_text()
    assert lines.startswith(raw) and lines.count('\n') == raw.count('\n') + 28  # nothing rewritten: the redo of 3 and 4..30 appended
    assert [r['seq'] for r in records] == list(range(1, 31)) and resumed['status'] == 'completed' and resumed['voided'] == 1


def test_a_void_retires_the_operator_done_marker_of_that_interaction(tmp_path):
    """10/18: voiding the last record also retires its operator-done marker (renamed with the void time, never deleted), so the
    redo is not completed by the marker of the voided attempt."""
    state, records, _ = execute(tmp_path, plan=X.load_plan(PLAN), calibration_pause=True)
    at = state['at_seq']
    (tmp_path / 'trial' / 'operator-done').mkdir(mode=0o700)
    (tmp_path / 'trial' / 'operator-done' / str(at)).write_text('')
    (tmp_path / 'trial' / 'operator-done' / str(at - 1)).write_text('')  # an earlier interaction's marker stays
    execute(tmp_path, resume='owner-confirmation:1', void=(at, 'owner-void:1'))
    names = sorted(p.name for p in (tmp_path / 'trial' / 'operator-done').iterdir())
    assert str(at) not in names and str(at - 1) in names
    assert any(n.startswith(f'{at}.voided-') for n in names)
