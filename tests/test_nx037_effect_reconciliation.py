"""scripts/nx037/effect_reconciliation.py (NX-037 evidence item effect_reconciliation, read only): each trial intent's state after the
trial, from PostgreSQL in a READ ONLY transaction; ids, codes, counts and times only, valid for the evidence pack."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import effect_reconciliation as C  # noqa: E402
import evidence_pack as E  # noqa: E402


def test_the_item_reports_settled_unresolved_missing_and_determined_intents(tmp_path):
    a, b, c, d = ('00000000-0000-4000-8000-00000000000' + str(n) for n in range(1, 5))
    intents = {x: {'seq': n, 'kind': 'ordinary_reply', 'at_end': 'settled'} for n, x in enumerate((a, b, c, d), 1)}
    rows = [(a, 'fulfilled', 'fulfilled', 'delivered', 1, {'fulfilled': 1}, None, '2026-10-18T05:40:00Z'),
            (b, 'unknown', 'unknown', 'unknown', 1, {'not_found': 2}, None, '2026-10-18T05:41:00Z'),
            (c, 'failed', 'failed', 'failed', 1, {'not_found': 1}, 'confirmed_not_delivered', '2026-10-16T13:00:00Z')]
    item = C.reconcile(rows, intents)
    assert item['totals'] == {'intents': 4, 'settled': 2, 'unresolved': 1, 'missing': 1, 'determined': 1}
    assert item['intents'][b]['status'] == 'unresolved' and item['intents'][d]['status'] == 'missing'
    assert item['intents'][c]['determination'] == 'confirmed_not_delivered'
    E._write(tmp_path / 'r.json', item)  # every value is allowed in the pack
    assert E.violations(json.loads((tmp_path / 'r.json').read_text())) == []
    with pytest.raises(ValueError):
        E._write(tmp_path / 'bad.json', C.reconcile([(a, 'unknown', 'unknown', 'free text state', 1, {}, None, None)], {a: intents[a]}))


from support.effect_provider import effect_provider  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401


def test_the_query_reads_the_ledger_read_only(governed_effect_executor, admin, pg, tmp_path):  # noqa: F811
    from test_effect_dispatch import accepted
    intent = accepted(governed_effect_executor)['intent_id']
    rows = admin.execute(C.QUERY, ([intent],)).fetchall()
    item = C.reconcile(rows, {intent: {'seq': 1, 'kind': 'ordinary_reply', 'at_end': 'unresolved'}})
    assert item['intents'][intent]['state'] == 'accepted' and item['intents'][intent]['outbox_state'] == 'pending' and item['totals']['unresolved'] == 1
    # The command: a trial directory naming the intent, a private DSN; the transaction is read only (a write would fail).
    trial = tmp_path / 'trial'
    import run_interactions as R
    assert R.main(['--driver', 'synthetic', '--trial-id', '37b00000-0000-4000-8000-000000000002', '--out', str(trial)]) == 0
    dsn = tmp_path / 'dsn';dsn.write_text(pg);os.chmod(dsn, 0o600)
    out = tmp_path / 'reconciliation.json'
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/nx037/effect_reconciliation.py'), '--database-url-file', str(dsn), '--trial', str(trial), '--out', str(out)],
                         capture_output=True, text=True, timeout=60, env={**os.environ, 'PYTHONPATH': str(ROOT / 'packages/eios-core/src')})
    assert run.returncode == 0, run.stderr
    summary = json.loads(run.stdout)
    assert summary['ok'] is True and summary['missing'] == summary['intents']  # synthetic trial intents are not in this database
    assert E.violations(json.loads(out.read_text())) == []
