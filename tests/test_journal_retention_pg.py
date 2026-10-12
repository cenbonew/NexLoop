"""NX-035 (0156) on clean catalog PostgreSQL: the deletion records sweep never goes past the exported restore journal, and the
journal keeps every entry until it is exported and its days are over. The sweep is the retention keeper's 'records' class
(runtime.nexloop_retention_sweep_records, called as the owner as the keeper's port does); exports are recorded as the backup
recorder leaves them. Synthetic data only."""
from datetime import UTC, datetime, timedelta

import pytest

from nexloop_eios.bootstrap import bootstrap
from support.journal_export import record_journal_export
from support.tenants import TENANT_A, TENANT_B

OLD, YOUNG = 400, 10  # days; deletion_ledger_days is 365


@pytest.fixture
def db(admin):
    bootstrap(admin)
    # NX-052 batch 4b: ledger rows reference an existing tenant.
    for tenant in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    return admin


def ledger_entry(admin, tenant, ref, days):
    """A deletion ledger entry (its journal entry is written by the 0146 trigger), then aged."""
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        seq = admin.execute("""insert into control.nexloop_deletion_ledger(seq,tenant_id,world,kind,subject_ref,request_ref)
            values(nextval('control.nexloop_deletion_watermark'),%s,'real','message',%s,%s) returning seq""", (tenant, 'f' * 64, ref)).fetchone()[0]
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        at = datetime.now(UTC) - timedelta(days=days)
        admin.execute('update control.nexloop_deletion_ledger set recorded_at=%s where seq=%s', (at, seq))
        admin.execute("update control.nexloop_restore_journal set recorded_at=%s where kind='deletion' and (entry->>'seq')::bigint=%s", (at, seq))
    return seq


def sweep(admin, tenant=TENANT_A):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute("select runtime.nexloop_retention_sweep_records(%s,'real','records',200,'retention:test','synthetic-keeper')", (tenant,)).fetchone()[0]


def refs(admin):
    return sorted(r for (r,) in admin.execute('select request_ref from control.nexloop_deletion_ledger').fetchall())


def journal(admin):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return [(s, e['request_ref']) for s, e in admin.execute("select seq,entry from control.nexloop_restore_journal where kind='deletion' order by seq").fetchall()]


def exported_since(admin, after):
    """What the recorder exports after a sequence (authz.nexloop_restore_journal_since, as the recorder role calls it)."""
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute("select coalesce(jsonb_agg(jsonb_build_object('seq',seq,'kind',kind,'entry',entry) order by seq),'[]'::jsonb) from control.nexloop_restore_journal where seq>%s",
                             (after,)).fetchone()[0]


def test_nothing_leaves_before_an_export_and_unexported_entries_never_leave(db):
    admin = db
    ledger_entry(admin, TENANT_A, 'old-1', OLD)
    ledger_entry(admin, TENANT_A, 'old-2', OLD)
    ledger_entry(admin, TENANT_A, 'young', YOUNG)
    out = sweep(admin)
    assert out['counts'] == {'ledger': 0, 'tombstones': 0, 'journal': 0, 'journal_exported': False}  # never exported: fail closed
    assert refs(admin) == ['old-1', 'old-2', 'young'] and [r for _, r in journal(admin)] == ['old-1', 'old-2', 'young']
    # Exported up to old-1 only: old-1 (ledger and journal) leaves; old-2, not exported, stays although its days are over.
    first = journal(admin)[0][0]
    record_journal_export(admin, up_to=first)
    out = sweep(admin)
    assert out['counts']['ledger'] == 1 and out['counts']['journal'] == 1
    assert refs(admin) == ['old-2', 'young'] and [r for _, r in journal(admin)] == ['old-2', 'young']
    # Everything exported: old-2 leaves; the young entry stays in both (its days are not over).
    record_journal_export(admin)
    sweep(admin)
    assert refs(admin) == ['young'] and [r for _, r in journal(admin)] == ['young']


def test_the_exported_segments_and_the_remaining_journal_are_contiguous(db):
    """Restore replays what the exports hold plus what the restored database has: no entry may fall between them."""
    admin = db
    for i in range(3):
        ledger_entry(admin, TENANT_A, f'a-{i}', OLD)
    original = exported_since(admin, 0)
    cut = original[1]['seq']
    segment_1 = exported_since(admin, 0)[:2]  # the recorder exported the first two entries
    record_journal_export(admin, up_to=cut)
    ledger_entry(admin, TENANT_A, 'a-later', OLD)
    sweep(admin)
    remaining = exported_since(admin, 0)
    assert all(e['seq'] > cut for e in remaining)  # what the database still has starts right after the export
    replayable = segment_1 + remaining
    assert [e['seq'] for e in replayable] == sorted(e['seq'] for e in replayable)
    assert [e['entry']['request_ref'] for e in replayable] == ['a-0', 'a-1', 'a-2', 'a-later']  # nothing lost, nothing twice
    assert [e for e in replayable[:3]] == original  # unchanged entries


def test_other_tenants_are_not_swept_by_this_tenants_pass_and_a_replay_stops_it(db):
    admin = db
    ledger_entry(admin, TENANT_A, 'a-old', OLD)
    ledger_entry(admin, TENANT_B, 'b-old', OLD)
    record_journal_export(admin)
    sweep(admin, TENANT_A)
    assert refs(admin) == ['b-old'] and [r for _, r in journal(admin)] == ['b-old']
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into control.nexloop_deletion_replays(restore_ref,from_seq,state) values('restore-x',0,'replaying')")
    assert sweep(admin, TENANT_B)['counts'] == {'replay_open': True}
    assert refs(admin) == ['b-old']
