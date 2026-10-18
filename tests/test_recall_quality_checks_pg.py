"""docs/release/recall-quality-checks.sql (owner 2026-10-19, stage-embedding sheet appendix): every query runs read-only on a
catalog database with an active profile and indexed definitions (deterministic test embedding), and returns its columns."""
from pathlib import Path

from test_claim_matching_pg import env  # noqa: F401

SQL = (Path(__file__).resolve().parents[1] / 'docs/release/recall-quality-checks.sql').read_text()


def statements(text, tenant):
    body = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('--')).replace(':T', "'" + tenant + "'")
    return [s.strip() for s in body.split(';') if s.strip()]


def test_the_recall_quality_queries_run_read_only(env):  # noqa: F811
    f = env;admin = f['admin']
    queries = statements(SQL, f['tenant'])
    assert len(queries) == 4
    with admin.transaction():
        admin.execute('set transaction read only')
        coverage = admin.execute(queries[0]).fetchall()
        assert {row[0] for row in coverage} == {'definition', 'instance'} and all(row[3] == 0 for row in coverage)
        admin.execute(queries[1]).fetchall()
        totals = admin.execute(queries[2]).fetchone()
        assert totals[0] > 0 and totals[1] <= totals[0] and totals[2] <= totals[1]
        assert admin.execute(queries[3]).fetchall() == []  # the probe texts are not in the synthetic schema
