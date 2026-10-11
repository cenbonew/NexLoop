"""NX-035 (0250): a verified restore journal export, as the backup recorder leaves it after exporting the journal up to its current
sequence (authz.nexloop_record_backup, kind 'journal'). The deletion records sweep only removes what an export covers."""


def record_journal_export(admin, up_to=None):
    """Returns the exported sequence. up_to: the journal sequence covered (default: everything journaled so far)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        seq = up_to if up_to is not None else admin.execute('select coalesce(max(seq),0) from control.nexloop_restore_journal').fetchone()[0]
        admin.execute("""insert into runtime.nexloop_backup_manifests(backup_id,repo,kind,started_at,completed_at,system_identifier,timeline,catalog_revision,
            catalog_digest,deletion_watermark,journal_seq,verify_status,verified_at)
            values('journal-'||%s||'-'||gen_random_uuid(),'repo1','journal',now(),now(),'1',1,'0001',repeat('a',64),0,%s,'verified',now())""", (seq, seq))
    return seq
