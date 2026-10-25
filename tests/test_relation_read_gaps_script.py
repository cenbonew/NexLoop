"""NX-054 P4a: scripts/acceptance/relation_read_gaps.py exit codes and evidence (no database: a scripted connection), and alert
rules v19 = v18 plus timer_stale_relation_read_gaps for the DATA_HOST timer the examples list."""
import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import relation_read_gaps as G  # noqa: E402

from nexloop_eios.observability import load_rules, validate_rules  # noqa: E402

NOW = datetime(2026, 10, 26, 3, 40, tzinfo=UTC)


class Conn:
    def __init__(self, doc):
        self.doc, self.sql, self.closed = doc, [], False

    def transaction(self):
        conn = self

        class T:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False
        return T()

    def execute(self, sql, params=()):
        self.sql.append(sql)
        doc = self.doc

        class R:
            def fetchone(self):
                return [doc]
        return R()

    def close(self):
        self.closed = True


def doc(gaps, erasing=0):
    return {'tenant_id': 't', 'gaps': gaps, 'gaps_erasing': erasing, 'relation_only': 0,
            'rows': [{'world': 'real', 'type_name': 'Goal', 'objects': 3, 'gaps': gaps, 'gaps_erasing': erasing, 'relation_only': 0}]}


def run(tmp_path, d, evidence=None):
    conn, out, err = Conn(d), io.StringIO(), io.StringIO()
    code = G.main(['--dsn-file', str(tmp_path / 'dsn'), '--tenant', 't', '--evidence-dir', str(evidence or tmp_path)],
                  connect=lambda path: conn, clock=lambda: NOW, out=out, err=err)
    return code, conn, json.loads(out.getvalue()), err.getvalue()


def test_no_gap_exits_0_and_writes_the_days_evidence(tmp_path):
    code, conn, got, err = run(tmp_path, doc(0, erasing=2))  # erasing gaps never fail the check (P5 looks at gaps only)
    assert code == 0 and err == '' and conn.closed and conn.sql[0] == 'set transaction read only'
    path = tmp_path / '2026-10-26.json'
    assert got['evidence'] == str(path) and json.loads(path.read_text())['gaps_erasing'] == 2
    assert path.stat().st_mode & 0o777 == 0o640 and not [p for p in tmp_path.iterdir() if p.name.startswith('.')]


def test_gaps_exit_1_with_a_journal_line(tmp_path):
    code, _, got, err = run(tmp_path, doc(3))
    assert code == 1 and err.strip() == '<3>nexloop-relation-read-gaps gaps=3 worlds=1'
    assert json.loads((tmp_path / '2026-10-26.json').read_text())['gaps'] == 3


def test_unwritten_evidence_exits_2(tmp_path):
    code, _, got, err = run(tmp_path, doc(0), evidence=tmp_path / 'missing')
    assert code == 2 and got['evidence'] is None and 'evidence_unwritten' in err
    ro = tmp_path / 'ro'
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        if os.access(ro, os.W_OK):
            pytest.skip('running as a user that ignores directory permissions')
        assert run(tmp_path, doc(0), evidence=ro)[0] == 2
    finally:
        os.chmod(ro, 0o700)


def test_a_refused_login_exits_2(tmp_path):
    def refuse(path):
        raise G.RR.Refused('--dsn-file: the read-only nexloop_metrics login only')
    out = io.StringIO()
    code = G.main(['--dsn-file', str(tmp_path / 'dsn'), '--tenant', 't', '--evidence-dir', str(tmp_path)], connect=refuse, out=out, err=io.StringIO())
    assert code == 2 and 'refused' in json.loads(out.getvalue())


def test_rules_v19_are_v18_plus_the_relation_read_gaps_timer_and_the_api_read_p95():
    rules = ROOT / 'deploy/configuration'
    v18, v19 = json.loads((rules / 'alert-rules.v18.json').read_text()), load_rules(rules / 'alert-rules.v19.json')
    assert v19['version'] == 19 and v19['rules'][:len(v18['rules'])] == v18['rules'] and len(v19['rules']) == len(v18['rules']) + 2 == 76
    added, p95 = v19['rules'][-2:]
    assert (added['rule_id'], added['metric'], added['selector'], added['condition'], added['threshold']) == (
        'timer_stale_relation_read_gaps', 'host_health.timer_success_age_seconds', 'data_host:relation-read-gaps', 'gt', 93600)
    # NX-061 E4 (owner decision): the read p95, in the same version so the rules do not fork.
    assert (p95['rule_id'], p95['metric'], p95['condition'], p95['threshold'], p95['for_seconds'], p95['severity']) == (
        'api_read_p95_high', 'api.classes.read.p95_ms', 'gt', 500, 600, 'warning') and 'selector' not in p95 and 'NX-061 E4' in p95['purpose']
    validate_rules(v19)
    failure = next(r for r in v19['rules'] if r['rule_id'] == 'timer_scheduled_failure')
    assert failure['selector'] == '*' and failure['condition'] == 'gt' and failure['threshold'] == 0  # the gaps alarm (exit 1)
    install = json.loads((ROOT / 'deploy/examples/host-health-install.data-host.example.json').read_text())
    assert {'name': 'relation-read-gaps', 'timer': 'nexloop-relation-read-gaps.timer', 'service': 'nexloop-relation-read-gaps.service'} in install['probe']['timers']
    assert (ROOT / 'deploy/stage/systemd/nexloop-relation-read-gaps.timer.example').exists()
