"""One clock reading per issue (L6 S1 after CI seq 186): the issue time plus the validity is exactly the expiry, and the audit (or event)
written with it carries the same instant. The second check fails at once on a definition that reads the clock again for the audit."""


def issued_once(admin, tenant, table, key, value, validity, start='issued_at', end='expires_at', audit=None):
    """`audit` is (event table, its time column, its key column, its key value) of the record written in the same call."""
    admin.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    issued, lasting = admin.execute(f'select {start},{end}-{start} from {table} where {key}=%s', (value,)).fetchone()
    assert lasting == validity, (table, lasting)
    if audit:
        events, at, column, ref = audit
        assert admin.execute(f'select {at} from {events} where {column}=%s', (ref,)).fetchall() == [(issued,)], (table, issued)
