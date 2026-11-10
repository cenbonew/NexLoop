"""NX-062 M5c (design §11; L6 V-1, V-4) over the real workbench HTTP application and real PostgreSQL: a member adds a note through the
workbench action channel (CSRF, Origin and the member's role), and the object page reads the notes of one object - the text exactly as
stored (plain values; the page renders text nodes only), each marked with its author kind; a machine note names its client."""
import json
import secrets

import pytest

from support.tenants import TENANT_A as TENANT
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_reads_pg import hexid
from test_object_notes_pg import limits, note, notes  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ORIGIN, browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

HTML = '<img src=x onerror=alert(1)> [链接](javascript:alert(1)) 客户说周五前收货'


def body(w, text=HTML, obj=None):
    return {'type_name': 'Commitment', 'object_id': obj or w['object'], 'text': text}


def test_members_add_and_read_notes_through_the_workbench(notes):
    w = notes
    admin = w['admin']
    limits(admin)
    note(w, 'machine-note-1', '机器写的')
    with w['client']() as client:
        csrf = owner(client, w)
        added = act(client, csrf, 'object_note_add', body(w))
        assert added.status_code == 200, added.text
        # V-4: no CSRF token, a wrong one, another Origin: refused, nothing written.
        path = '/api/v1/workbench/actions/object_note_add'
        for headers in ({'Origin': ORIGIN}, {'Origin': ORIGIN, 'X-CSRF-Token': 'wrong'}, {'Origin': 'https://elsewhere.invalid', 'X-CSRF-Token': csrf}):
            refused = client.post(path, json=body(w, '不应写入'), headers={**headers, 'Idempotency-Key': 'wb-' + secrets.token_hex(12)})
            assert refused.status_code in (401, 403), (headers, refused.status_code)  # the existing CSRF and Origin checks refuse
        gone = act(client, csrf, 'object_note_add', body(w, obj=hexid()))
        assert gone.status_code in (404, 409, 422) and gone.status_code != 200
        read = client.get(f"/api/v1/workbench/objects/Commitment/{w['object']}/notes")
        assert read.status_code == 200 and read.headers['content-type'].startswith('application/json')
        items = read.json()['notes']
        texts = {(n['author_kind'], n['client_id'], n['text']) for n in items}
        assert ('human', None, HTML) in texts and ('machine', 'acme-agent', '机器写的') in texts  # the text verbatim, never transformed
        assert all(n['author_principal_id'] is None for n in items if n['author_kind'] == 'machine')
        assert '不应写入' not in read.text
        assert client.get(f'/api/v1/workbench/objects/Commitment/{hexid()}/notes').status_code == 404
        assert client.get(f"/api/v1/workbench/objects/Consumer/{w['object']}/notes").status_code == 404
        assert client.get(f"/api/v1/workbench/objects/Commitment/{w['object']}/notes?x=1").status_code == 422
    with w['client']() as client:
        login = workbench_login(client, w['operator'])
        assert login.status_code == 200
        assert act(client, login.json()['csrf_token'], 'object_note_add', body(w, '操作员的备注')).status_code == 200
    with w['client']() as client:  # no session: 401 for the read
        assert client.get(f"/api/v1/workbench/objects/Commitment/{w['object']}/notes").status_code == 401
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    assert admin.execute("select count(*) from runtime.nexloop_object_notes where author_kind='human'").fetchone()[0] == 2
    audit = [p for (p,) in admin.execute("select payload from runtime.audit_events where action='object.note.add'").fetchall()]
    assert len(audit) == 3 and HTML not in json.dumps(audit, ensure_ascii=False)
