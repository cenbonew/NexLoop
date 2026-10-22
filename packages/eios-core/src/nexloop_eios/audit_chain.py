"""NX-059 audit hash chain (docs/design/audit-hash-chain.md): the parts that run outside the database.

The database keeps one SHA-256 chain per tenant (0320 runtime.nexloop_audit_chain*). Here: the HMAC seal of a chain head (the key comes
from private configuration and never reaches the database, a log or the output), the export of a tenant's chain with each surviving
source row's text, and the offline verification of such an export. The link and digest formulas mirror 0320 exactly.
"""
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

LINK = 'nexloop-audit-chain/1'
SEAL = 'nexloop-audit-seal/1'
EXPORT_SCHEMA = 'nexloop-audit-export/1'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def genesis(tenant):
    return sha(f'{LINK}|genesis|{tenant}')


def link(tenant, seq, source, key, digest, prev):
    return sha(f'{LINK}|{tenant}|{seq}|{source}|{key}|{digest}|{prev}')


def load_keys(path):
    """A private file (0600, owned by the caller): one 64-hex key per line; the first is the active one. → {key_id: key bytes}."""
    from nexloop_eios.private_configuration import read_private_text
    keys = {}
    for line in read_private_text(path, maximum=4096).split():
        if len(line) != 64 or any(ch not in '0123456789abcdef' for ch in line):
            raise ValueError('seal key file: one 64-hex key per line')
        key = bytes.fromhex(line)
        keys[key_id(key)] = key
    if not keys:
        raise ValueError('seal key file: no key')
    return keys


def key_id(key):
    return hashlib.sha256(key).hexdigest()[:16]


def seal_mac(key, tenant, seq, chain_link, sealed_at):
    return hmac.new(key, f'{SEAL}|{tenant}|{seq}|{chain_link}|{sealed_at}'.encode(), hashlib.sha256).hexdigest()


def _utc_text(value):
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


def seal(connection, tenant, keys):
    """Append what is pending, then seal the tenant's current head with the active key. → the seal (no key material)."""
    active_id, key = next(iter(keys.items()))
    connection.execute('select runtime.nexloop_audit_chain_append(10000)')
    head = connection.execute('select seq,link from runtime.nexloop_audit_chain_heads where tenant_id=%s', (tenant,)).fetchone()
    if head is None:
        return None
    sealed_at = datetime.now(UTC).isoformat()
    mac = seal_mac(key, tenant, head[0], head[1], sealed_at)
    connection.execute('select runtime.nexloop_audit_chain_seal(%s,%s,%s,%s,%s,%s)', (tenant, head[0], head[1], active_id, sealed_at, mac))
    return {'tenant_id': tenant, 'seq': head[0], 'link': head[1], 'key_id': active_id, 'sealed_at': sealed_at, 'mac': mac}


def seals(connection, tenant):
    rows = connection.execute("select seq,link,key_id,sealed_at,mac from runtime.nexloop_audit_chain_seals where tenant_id=%s order by seq,key_id",
                              (tenant,)).fetchall()
    return [{'seq': r[0], 'link': r[1], 'key_id': r[2], 'sealed_at': _utc_text(r[3]), 'mac': r[4]} for r in rows]


def check_seals(tenant, sealed, keys, links):
    """Each seal: a known key, a valid MAC, and the link the chain holds at its position. → problem codes."""
    problems = []
    for s in sealed:
        key = keys.get(s['key_id'])
        if key is None:
            problems.append({'code': 'seal_key_unknown', 'seq': s['seq'], 'key_id': s['key_id']})
        elif not hmac.compare_digest(seal_mac(key, tenant, s['seq'], s['link'], s['sealed_at']), s['mac']):
            problems.append({'code': 'seal_invalid', 'seq': s['seq'], 'key_id': s['key_id']})
        if links.get(s['seq']) != s['link']:
            problems.append({'code': 'seal_mismatch', 'seq': s['seq'], 'key_id': s['key_id']})
    return problems


def verify(connection, tenant, keys):
    """The database's own verification plus the seals' MACs (the key never leaves this process)."""
    result = connection.execute('select runtime.nexloop_audit_chain_verify(%s)', (tenant,)).fetchone()[0]
    links = dict(connection.execute('select seq,link from runtime.nexloop_audit_chain where tenant_id=%s', (tenant,)).fetchall())
    sealed = seals(connection, tenant)
    problems = result['problems'] + check_seals(tenant, sealed, keys, links)
    return {**result, 'seals': len(sealed), 'problems': problems, 'valid': not problems}


def export(connection, tenant, out):
    """chain.jsonl (one entry per line, with the surviving source row's text), seals.jsonl and manifest.json in a new directory."""
    out = Path(out)
    out.mkdir(mode=0o700)
    entries, after = [], 0
    while True:
        rows = [r[0] for r in connection.execute('select runtime.nexloop_audit_chain_export(%s,%s,%s)', (tenant, after, 5000)).fetchall()]
        if not rows:
            break
        entries += rows
        after = rows[-1]['seq']
    sealed = seals(connection, tenant)
    files = {'chain.jsonl': ''.join(json.dumps(e, ensure_ascii=False, sort_keys=True) + '\n' for e in entries),
             'seals.jsonl': ''.join(json.dumps(s, sort_keys=True) + '\n' for s in sealed)}
    for name, text in files.items():
        (out / name).write_text(text)
    manifest = {'schema': EXPORT_SCHEMA, 'tenant_id': tenant, 'entries': len(entries), 'head_seq': entries[-1]['seq'] if entries else 0,
                'head_link': entries[-1]['link'] if entries else genesis(tenant), 'exported_at': datetime.now(UTC).isoformat(),
                'files': {name: sha(text) for name, text in files.items()}}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    return manifest


def verify_export(directory, keys):
    """Offline: file digests, every link from genesis, every surviving row's digest from its exported text, pruned entries, seals."""
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('schema') != EXPORT_SCHEMA:
        raise ValueError('not an audit export')
    tenant, problems = manifest['tenant_id'], []
    texts = {name: (directory / name).read_text() for name in ('chain.jsonl', 'seals.jsonl')}
    for name, text in texts.items():
        if sha(text) != manifest['files'].get(name):
            problems.append({'code': 'file_altered', 'file': name})
    prev, links, pruned = genesis(tenant), {}, 0
    entries = [json.loads(line) for line in texts['chain.jsonl'].splitlines()]
    for expected_seq, e in enumerate(entries, start=1):
        if e['seq'] != expected_seq:
            problems.append({'code': 'sequence_gap', 'seq': e['seq'], 'expected': expected_seq})
        if e['prev_link'] != prev or e['link'] != link(tenant, e['seq'], e['source'], e['event_key'], e['record_digest'], e['prev_link']):
            problems.append({'code': 'link_broken', 'seq': e['seq']})
        if e['record_text'] is None:
            if e['pruned_at'] is None:
                problems.append({'code': 'record_deleted', 'seq': e['seq']})
            pruned += 1
        elif sha(e['record_text']) != e['record_digest']:
            problems.append({'code': 'record_altered', 'seq': e['seq']})
        links[e['seq']] = e['link']
        prev = e['link']
    if (manifest['head_seq'], manifest['head_link']) != ((entries[-1]['seq'], prev) if entries else (0, genesis(tenant))) \
            or manifest['entries'] != len(entries):
        problems.append({'code': 'head_mismatch'})
    sealed = [json.loads(line) for line in texts['seals.jsonl'].splitlines()]
    problems += check_seals(tenant, sealed, keys, links)
    return {'tenant_id': tenant, 'valid': not problems, 'entries': len(entries), 'pruned': pruned, 'seals': len(sealed), 'problems': problems}
