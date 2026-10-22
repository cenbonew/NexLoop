"""NX-059 audit hash chain (docs/design/audit-hash-chain.md) on PostgreSQL: a tampered, deleted or inserted audit row, a removed or
recomputed chain entry and a forged seal are each found; concurrent audit writes with concurrent appends leave one complete valid chain;
retention keeps the chain verifiable (pruned entries); the export verifies offline and a changed export does not. Synthetic tenants and
audit rows; the admin connection writes the audit rows (as any governed Action would) and does the tampering."""
import json
import secrets
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios import audit_chain as A
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ops'))
import audit_chain as tool  # noqa: E402


def audit(c, tenant=TENANT_A, action='nexloop.synthetic.audit', payload=None, created=None):
    event = str(uuid.uuid4())
    with c.transaction():
        c.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        c.execute('insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                  (event, action, tenant, 'synthetic-actor', 't-' + event[:8], 'r-' + event[:8], Jsonb(payload or {'n': 1}), created or datetime.now(UTC)))
    return event


def as_tenant(c, tenant, sql, params=()):
    with c.transaction():
        c.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        c.execute('set local session_replication_role=replica')  # the tamperer: triggers off
        return c.execute(sql, params)


def verify(c, tenant=TENANT_A):
    return c.execute('select runtime.nexloop_audit_chain_verify(%s)', (tenant,)).fetchone()[0]


def codes(result):
    return sorted({p['code'] for p in result['problems']})


@pytest.fixture
def db(admin, tmp_path):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    keyfile = tmp_path / 'seal-keys'
    keyfile.write_text(secrets.token_hex(32) + '\n')
    keyfile.chmod(0o600)
    return {'admin': admin, 'keys': A.load_keys(keyfile), 'keyfile': keyfile}


@pytest.fixture
def chained(db):
    c = db['admin']
    db['events'] = [audit(c, payload={'n': i}) for i in range(5)] + [audit(c, TENANT_B)]
    sealed = A.seal(c, TENANT_A, db['keys'])
    assert sealed['seq'] == 5
    return db


def test_a_clean_chain_verifies_with_its_seal(chained):
    c = chained['admin']
    result = A.verify(c, TENANT_A, chained['keys'])
    assert result['valid'] and (result['entries'], result['seals'], result['pending']) == (5, 1, 1 - 1), result
    assert A.verify(c, TENANT_B, chained['keys'])['valid']  # its own chain (appended by the seal call), unsealed
    entries = c.execute('select seq,source,prev_link,link from runtime.nexloop_audit_chain where tenant_id=%s order by seq', (TENANT_A,)).fetchall()
    assert [e[0] for e in entries] == [1, 2, 3, 4, 5] and entries[0][2] == A.genesis(TENANT_A)
    assert all(entries[i][2] == entries[i - 1][3] for i in range(1, 5))


def test_a_changed_row_is_found(chained):
    c = chained['admin']
    as_tenant(c, TENANT_A, "update runtime.audit_events set payload='{\"n\":99}' where event_id=%s", (chained['events'][2],))
    result = verify(c)
    assert codes(result) == ['record_altered'] and result['problems'][0]['seq'] == 3


def test_a_deleted_row_is_refused_and_a_forced_delete_is_found(chained):
    c = chained['admin']
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='governed retention or erasure'), c.transaction():
        c.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        c.execute('delete from runtime.audit_events where event_id=%s', (chained['events'][1],))
    as_tenant(c, TENANT_A, 'delete from runtime.audit_events where event_id=%s', (chained['events'][1],))
    assert codes(verify(c)) == ['record_deleted']


def test_an_inserted_row_is_found(chained):
    c = chained['admin']
    ghost = str(uuid.uuid4())
    as_tenant(c, TENANT_A, "insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,created_at) values(%s,'x',%s,'a','t','r',now())",
              (ghost, TENANT_A))
    result = verify(c)
    assert codes(result) == ['record_unchained'] and result['problems'][0]['event_key'] == ghost


def test_a_removed_or_recomputed_chain_entry_is_found(chained):
    c = chained['admin']
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append only'):
        c.execute('delete from runtime.nexloop_audit_chain where tenant_id=%s and seq=2', (TENANT_A,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append only'):
        c.execute('update runtime.nexloop_audit_chain set record_digest=%s where tenant_id=%s and seq=2', ('0' * 64, TENANT_A))
    # The tamperer with triggers off removes entry 2: sequence, links and head no longer agree.
    as_tenant(c, TENANT_A, 'delete from runtime.nexloop_audit_chain where tenant_id=%s and seq=2', (TENANT_A,))
    assert {'sequence_gap', 'link_broken', 'head_mismatch', 'record_unchained'} <= set(codes(verify(c)))


def test_a_rewritten_chain_fails_on_its_seal(chained):
    """The tamperer changes row 3 and recomputes the whole chain and head consistently (no key needed for SHA-256): the database's
    own checks pass, the stored seal no longer names a link of the chain, and a forged seal fails its MAC."""
    c = chained['admin']
    as_tenant(c, TENANT_A, "update runtime.audit_events set payload='{\"n\":42}' where event_id=%s", (chained['events'][2],))
    rows = c.execute('select seq,source,event_key from runtime.nexloop_audit_chain where tenant_id=%s order by seq', (TENANT_A,)).fetchall()
    prev = A.genesis(TENANT_A)
    for seq, source, key in rows:
        row = c.execute('select runtime.nexloop_audit_chain_source(%s,%s,%s)::text', (TENANT_A, source, key)).fetchone()[0]
        digest = A.sha(row)
        new = A.link(TENANT_A, seq, source, key, digest, prev)
        as_tenant(c, TENANT_A, 'update runtime.nexloop_audit_chain set record_digest=%s,prev_link=%s,link=%s where tenant_id=%s and seq=%s', (digest, prev, new, TENANT_A, seq))
        prev = new
    as_tenant(c, TENANT_A, 'update runtime.nexloop_audit_chain_heads set link=%s where tenant_id=%s', (prev, TENANT_A))
    result = A.verify(c, TENANT_A, chained['keys'])
    assert codes(result) == ['seal_mismatch'], result
    # The existing seal cannot be replaced (append only), and a new seal over the rewritten chain needs the key: forged, its MAC fails.
    audit(c)
    c.execute('select runtime.nexloop_audit_chain_append(100)')
    seq, head = c.execute('select seq,link from runtime.nexloop_audit_chain_heads where tenant_id=%s', (TENANT_A,)).fetchone()
    c.execute('select runtime.nexloop_audit_chain_seal(%s,%s,%s,%s,%s,%s)', (TENANT_A, seq, head, next(iter(chained['keys'])), datetime.now(UTC).isoformat(), '0' * 64))
    assert codes(A.verify(c, TENANT_A, chained['keys'])) == ['seal_invalid', 'seal_mismatch']


def test_retention_prunes_rows_and_the_chain_stays_valid(chained):
    c = chained['admin']
    old = audit(c, created=datetime.now(UTC) - timedelta(days=4000))
    with c.transaction():  # the governed retention path: an erasure scope in this transaction (0129), then the delete
        c.execute("select runtime.nexloop_erasure_open('control.nexloop_control_events',null,true,'retention:synthetic')")
        c.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        c.execute('delete from runtime.audit_events where event_id=%s', (old,))
    c.execute('select runtime.nexloop_audit_chain_append(100)')
    result = A.verify(c, TENANT_A, chained['keys'])
    assert result['valid'] and result['pruned'] == 1, result
    assert c.execute('select pruned_ref from runtime.nexloop_audit_chain where event_key=%s', (old,)).fetchone()[0] == 'retention:synthetic'


def test_concurrent_writes_and_appends_leave_one_complete_chain(db, pg):
    c = db['admin']
    errors = []

    def writer(n):
        try:
            with psycopg.connect(pg, autocommit=True) as w:
                for i in range(25):
                    audit(w, payload={'w': n, 'i': i})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def appender():
        try:
            with psycopg.connect(pg, autocommit=True) as a:
                for _ in range(15):
                    a.execute('select runtime.nexloop_audit_chain_append(7)')
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)] + [threading.Thread(target=appender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c.execute('select runtime.nexloop_audit_chain_append(1000)')
    result = verify(c)
    assert result['valid'] and result['entries'] == 100 and result['pending'] == 0, result


def test_the_export_verifies_offline_and_a_changed_export_does_not(chained, tmp_path, capsys):
    c = chained['admin']
    url = tmp_path / 'owner-dsn'
    url.write_text(c.info.dsn)
    url.chmod(0o600)
    out = tmp_path / 'export'
    assert tool.main(['export', '--database-url-file', str(url), '--tenant', TENANT_A, '--out', str(out)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest['entries'] == 5
    assert tool.main(['verify-export', '--key-file', str(chained['keyfile']), '--dir', str(out)]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)['valid'] and not any(k.hex() in printed for k in chained['keys'].values())
    # A row's text changed in the export (manifest digest recomputed by the forger): the row digest no longer matches.
    lines = (out / 'chain.jsonl').read_text().splitlines()
    entry = json.loads(lines[1])
    entry['record_text'] = entry['record_text'].replace('"n": 1', '"n": 7')
    lines[1] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    text = '\n'.join(lines) + '\n'
    (out / 'chain.jsonl').write_text(text)
    assert A.verify_export(out, chained['keys'])['problems'][0]['code'] == 'file_altered'
    m = json.loads((out / 'manifest.json').read_text())
    m['files']['chain.jsonl'] = A.sha(text)
    (out / 'manifest.json').write_text(json.dumps(m))
    assert [p['code'] for p in A.verify_export(out, chained['keys'])['problems']] == ['record_altered']
    # Another key cannot verify the seal.
    assert 'seal_key_unknown' in [p['code'] for p in A.verify_export(out, {A.key_id(b'x' * 32): b'x' * 32})['problems']]


def test_the_cli_verifies_and_reports_problems_by_code(chained, tmp_path, capsys):
    c = chained['admin']
    url = tmp_path / 'owner-dsn'
    url.write_text(c.info.dsn)
    url.chmod(0o600)
    args = ['verify', '--database-url-file', str(url), '--key-file', str(chained['keyfile']), '--tenant', TENANT_A]
    assert tool.main(args) == 0
    capsys.readouterr()
    as_tenant(c, TENANT_A, "update runtime.audit_events set actor_id='someone-else' where event_id=%s", (chained['events'][0],))
    assert tool.main(args) == 1
    assert json.loads(capsys.readouterr().out)['problems'][0]['code'] == 'record_altered'


@pytest.mark.parametrize('context', ['no_tenant', 'other_tenant', 'same_tenant'])
def test_an_audit_write_never_fails_because_of_its_chain(db, context):
    """21st batch CI (test_retention_expiry_pg, commitment-service): the capture trigger wrote its queue row under the writer's
    eios.tenant_id and RLS refused it when the session carried none or another tenant - the audit write itself failed. The queue row
    (and a prune mark) is written under the audit row's own tenant; the writer's setting is left as it was; the chain stays whole."""
    c = db['admin']
    setting = {'no_tenant': '', 'other_tenant': TENANT_B, 'same_tenant': TENANT_A}[context]
    events = []
    with c.transaction():
        c.execute("select set_config('eios.tenant_id',%s,true)", (setting,))
        for source in ('a', 'b'):
            event = f'ctx-{context}-{source}-' + uuid.uuid4().hex[:8]
            c.execute("insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at) values(%s,'x',%s,'a','t','r','{}',now())",
                      (event, TENANT_A))
            events.append(event)
        assert c.execute("select current_setting('eios.tenant_id',true)").fetchone()[0] == setting
    with c.transaction():  # a governed retention batch running under no tenant removes one of them: marked pruned, not refused
        c.execute("select runtime.nexloop_erasure_open('control.nexloop_control_events',null,true,'retention:ctx')")
        c.execute("select set_config('eios.tenant_id',%s,true)", (setting,))
        c.execute('delete from runtime.audit_events where event_id=%s', (events[0],))
        assert c.execute("select current_setting('eios.tenant_id',true)").fetchone()[0] == setting
    c.execute('select runtime.nexloop_audit_chain_append(100)')
    result = verify(c)
    assert result['valid'] and result['entries'] == 2 and result['pruned'] == 1, result
