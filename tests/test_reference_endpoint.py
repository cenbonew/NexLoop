"""NX-037: the reference enterprise delivery endpoint (an external stand-in, not NexLoop). Local TLS only, synthetic data."""
import importlib.util
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import ssl

import httpx
import pytest
from nexloop_eios.effect_provider import (EffectProviderConfiguration, EffectProviderQueryUnavailable, EffectProviderUnknown,
                                          HttpEffectProvider)

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


endpoint_module = load(ROOT / 'apps/reference-endpoint/reference_endpoint.py', 'reference_endpoint')
conformance = load(ROOT / 'scripts/nx037/endpoint_conformance.py', 'endpoint_conformance')


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


@contextmanager
def running(tmp_path):
    cert, key = tmp_path / 'endpoint-cert.pem', tmp_path / 'endpoint-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    cert.chmod(0o600)
    key.chmod(0o600)
    service, staff = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    data = tmp_path / 'endpoint-data'
    server = endpoint_module.make_server(data_dir=data, listen='127.0.0.1:0', service_token=service, staff_token=staff,
                                        tls_cert_file=str(cert), tls_key_file=str(key), tenant_label='示例企业')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = 'https://127.0.0.1:' + str(server.server_address[1])
    credential = private(tmp_path / 'service-credential', service)
    try:
        yield {'origin': origin, 'cert': cert, 'service': service, 'staff': staff, 'credential': credential, 'data': data, 'server': server}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def client(e, timeout=2.0):
    return HttpEffectProvider(EffectProviderConfiguration(e['origin'], credential_file=str(e['credential']), ca_file=str(e['cert']),
                                                          timeout=timeout))


def http(e):
    return httpx.Client(base_url=e['origin'], verify=ssl.create_default_context(cafile=str(e['cert'])), trust_env=False, timeout=10, follow_redirects=False)


def request(parameters=None):
    parameters = {'summary': '合成交付请求'} if parameters is None else parameters
    return {'intent_id': str(uuid.uuid4()), 'payload_digest': endpoint_module.digest(parameters), 'parameters': parameters}


def bearer(e):
    return {'Authorization': 'Bearer ' + e['service']}


def test_conformance_check_passes_every_item(tmp_path, capsys):
    with running(tmp_path) as e:
        report = conformance.run(EffectProviderConfiguration(e['origin'], credential_file=str(e['credential']), ca_file=str(e['cert'])))
    assert report['ok'] is True, [c for c in report['checks'] if not c['ok']]
    assert len(report['checks']) == 7
    output = capsys.readouterr()
    assert e['service'] not in output.out + output.err


def test_idempotent_replay_conflict_and_not_found(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        body = request()
        first = h.post('/v1/effects', json=body, headers={**bearer(e), 'Idempotency-Key': body['intent_id']})
        assert first.status_code == 200 and first.json()['state'] == 'fulfilled' and first.json()['payload_digest'] == body['payload_digest']
        replay = h.post('/v1/effects', json=body, headers={**bearer(e), 'Idempotency-Key': body['intent_id']})
        assert replay.status_code == 200 and replay.json() == first.json()
        other = request({'summary': '另一个请求'})
        conflict = {**other, 'intent_id': body['intent_id']}
        refused = h.post('/v1/effects', json=conflict, headers={**bearer(e), 'Idempotency-Key': body['intent_id']})
        assert refused.status_code == 409 and refused.json() == first.json()
        assert h.get('/v1/effects/' + body['intent_id'], headers=bearer(e)).json() == first.json()
        absent = str(uuid.uuid4())
        missing = h.get('/v1/effects/' + absent, headers=bearer(e))
        assert missing.status_code == 404 and missing.json() == {'intent_id': absent, 'payload_digest': None, 'state': 'not_found',
                                                                  'provider_reference': None}
        # Key must equal the intent; digest must match the parameters.
        wrong_key = h.post('/v1/effects', json=request(), headers={**bearer(e), 'Idempotency-Key': str(uuid.uuid4())})
        bad = request()
        bad['payload_digest'] = '0' * 64
        wrong_digest = h.post('/v1/effects', json=bad, headers={**bearer(e), 'Idempotency-Key': bad['intent_id']})
        assert wrong_key.status_code == 400 and wrong_digest.status_code == 400
        # Only the nexloop.enterprise.deliver:1 parameters are accepted.
        for parameters in ({}, {'details': 'x'}, {'summary': ''}, {'summary': 'x' * 201}, {'summary': 'x', 'details': 'y' * 2001},
                           {'summary': 'x', 'tenant_id': 'y'}, {'summary': 1}, {'summary': 'x', 'consumer_ref': 'r' * 201}):
            invalid = request(parameters)
            answer = h.post('/v1/effects', json=invalid, headers={**bearer(e), 'Idempotency-Key': invalid['intent_id']})
            assert answer.status_code == 400 and answer.json() == {'error': 'parameters_invalid'}, parameters
        full = request({'summary': 's', 'details': 'd' * 2000, 'consumer_ref': 'c' * 200})
        assert h.post('/v1/effects', json=full, headers={**bearer(e), 'Idempotency-Key': full['intent_id']}).status_code == 200
    with endpoint_module.Store(e['data']).connect() as db:
        assert db.execute('select count(*) from effects').fetchone()[0] == 2
        assert db.execute('pragma journal_mode').fetchone()[0] == 'wal'


def test_service_authentication_is_required(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        body = request()
        for headers in ({}, {'Authorization': 'Bearer ' + secrets.token_urlsafe(32)}, {'Authorization': 'Bearer ' + e['staff']}):
            assert h.post('/v1/effects', json=body, headers={**headers, 'Idempotency-Key': body['intent_id']}).status_code == 401
            assert h.get('/v1/effects/' + body['intent_id'], headers=headers).status_code == 401
        assert endpoint_module.Store(e['data']).get(body['intent_id']) is None


def staff_session(e, h):
    login = h.post('/staff/login', data={'token': e['staff']})
    assert login.status_code == 303 and 'HttpOnly' in login.headers['set-cookie'] and 'Secure' in login.headers['set-cookie']
    page = h.get('/staff')
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    return page, csrf.group(1) if csrf else None


def test_staff_marks_stay_in_the_endpoint_and_get_stays_fulfilled(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        handled = request({'summary': '为客户安排一次上门服务', 'details': '周三下午', 'consumer_ref': 'consumer-ref-opaque-1'})
        failed = request({'summary': '第二个请求'})
        provider = client(e)
        for body in (handled, failed):
            assert provider.dispatch(**body).state == 'fulfilled'
        assert h.post('/staff/login', data={'token': e['service']}).status_code == 401
        assert h.post('/staff/mark', data={'csrf': 'x', 'intent_id': handled['intent_id'], 'state': 'fulfilled'}).status_code == 401
        page, csrf = staff_session(e, h)
        assert handled['intent_id'] in page.text and '待处理' in page.text
        assert '为客户安排一次上门服务' in page.text and '周三下午' in page.text and 'consumer-ref-opaque-1' in page.text
        assert '示例企业' in page.text  # source tenant = the endpoint's configured label
        assert h.post('/staff/mark', data={'csrf': 'wrong', 'intent_id': handled['intent_id'], 'state': 'fulfilled'}).status_code == 403
        assert h.post('/staff/mark', data={'csrf': csrf, 'intent_id': handled['intent_id'], 'state': 'fulfilled'}).status_code == 400
        assert h.post('/staff/mark', data={'csrf': csrf, 'intent_id': handled['intent_id'], 'state': 'fulfilled', 'handled_by': '员工甲',
                                           'note': '已电话确认'}).status_code == 303
        assert h.post('/staff/mark', data={'csrf': csrf, 'intent_id': failed['intent_id'], 'state': 'failed', 'handled_by': '员工乙'}).status_code == 303
        page = h.get('/staff?view=all').text  # handled tickets are not in the default view (pending customer tickets)
        for text in ('请求摘要', '来源租户', '收到时间（北京时间）', '处理人', '员工甲', '已电话确认', '处理失败'):
            assert text in page
        # The staff result is the enterprise's business outcome, not an effect state: GET stays fulfilled for both.
        for body in (handled, failed):
            assert provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest']).state == 'fulfilled'
            assert 'failed' not in h.get('/v1/effects/' + body['intent_id'], headers=bearer(e)).text
        # A mark is final and kept in the endpoint's own store.
        h.post('/staff/mark', data={'csrf': csrf, 'intent_id': failed['intent_id'], 'state': 'fulfilled', 'handled_by': '员工甲'})
        tickets = {row[0]: row for row in e['server'].store.listing()}
        assert tickets[failed['intent_id']][1] == 'failed' and tickets[failed['intent_id']][4] == '员工乙'
        assert tickets[handled['intent_id']][1:2] == ('fulfilled',) and tickets[handled['intent_id']][5] == '已电话确认'


def test_drill_delayed_post_is_persisted_and_unknown_then_the_query_finds_it(tmp_path):
    with running(tmp_path) as e:
        provider = client(e)
        state = endpoint_module.drill(e['data'], delay_next_posts=1, delay_seconds=3.0)
        assert state['delay_posts'] == 1
        body = request()
        started = time.monotonic()
        with pytest.raises(EffectProviderUnknown):
            provider.dispatch(**body)
        assert time.monotonic() - started < 2.6
        stored = endpoint_module.Store(e['data']).get(body['intent_id'])
        assert stored is not None and stored['state'] == 'fulfilled'
        assert provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest']).provider_reference == stored['provider_reference']
        # The drill was used once; the next new request is answered at once.
        assert provider.dispatch(**request({'summary': '之后的请求'})).state == 'fulfilled'


def test_drill_get_unavailable_window(tmp_path):
    with running(tmp_path) as e:
        provider = client(e)
        body = request()
        provider.dispatch(**body)
        endpoint_module.drill(e['data'], get_unavailable_seconds=1)
        with pytest.raises(EffectProviderQueryUnavailable):
            provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest'])
        time.sleep(1.2)
        assert provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest']).state == 'fulfilled'


def test_drill_only_from_the_local_cli(tmp_path, capsys):
    with running(tmp_path) as e, http(e) as h:
        for path in ('/drill', '/v1/drill', '/staff/drill'):
            assert h.post(path, json={'delay_next_posts': 1}, headers=bearer(e)).status_code == 404
        assert endpoint_module.main(['drill', '--data-dir', str(e['data']), '--delay-next-posts', '2']) == 0
        assert json.loads(capsys.readouterr().out)['delay_posts'] == 2
        assert endpoint_module.main(['drill', '--data-dir', str(e['data']), '--clear']) == 0
        capsys.readouterr()
        control = e['data'] / 'drill-control.json'
        assert control.stat().st_mode & 0o777 == 0o600
        control.chmod(0o644)
        assert endpoint_module.main(['drill', '--data-dir', str(e['data']), '--status']) == 2
        with pytest.raises(ValueError):
            control.chmod(0o600)
            endpoint_module.drill(e['data'], delay_next_posts=11)


def test_cli_refuses_shared_tokens_and_non_private_files(tmp_path):
    same = secrets.token_urlsafe(32)
    a, b = private(tmp_path / 'a', same), private(tmp_path / 'b', same)
    key = private(tmp_path / 'key', 'unused')
    args = ['serve', '--data-dir', str(tmp_path / 'd'), '--listen', '127.0.0.1:0', '--tls-cert-file', str(key), '--tls-key-file', str(key),
            '--service-token-file', str(a), '--staff-token-file', str(b)]
    assert endpoint_module.main(args) == 2
    loose = tmp_path / 'loose'
    loose.write_text(secrets.token_urlsafe(32))
    loose.chmod(0o644)
    assert endpoint_module.main(args[:-4] + ['--service-token-file', str(loose), '--staff-token-file', str(b)]) == 2


def test_deployment_template_is_parameterised_and_non_root():
    compose = (ROOT / 'deploy/stage/reference-endpoint/compose.example.yaml').read_text()
    dockerfile = (ROOT / 'apps/reference-endpoint/Dockerfile').read_text()
    env = (ROOT / 'deploy/stage/reference-endpoint/compose.env.example').read_text()
    assert 'user: "10002:10002"' in compose and 'read_only: true' in compose and 'cap_drop: [ALL]' in compose
    assert 'no-new-privileges:true' in compose and 'nexloop' not in compose.split('services:')[1].split('volumes:')[0].replace('NexLoop', '')
    assert 'USER 10002:10002' in dockerfile and 'pip install' not in dockerfile
    for line in env.splitlines():
        if line and not line.startswith('#'):
            assert re.fullmatch(r'[A-Z_]+=<[A-Z_]+>', line), line
    assert not re.search(r'\b\d{1,3}(\.\d{1,3}){3}\b', compose + env)


def test_ticket_fields_survive_existing_data(tmp_path):
    store = endpoint_module.Store(tmp_path / 'd')
    body = request({'summary': '<script>x</script>'})
    store.submit(body['intent_id'], body['payload_digest'], body['parameters'])
    again = endpoint_module.Store(tmp_path / 'd')  # reopening keeps the added ticket columns
    assert again.mark(body['intent_id'], 'fulfilled', '员工甲', '完成') is True
    row = again.listing()[0]
    assert row[4:6] == ('员工甲', '完成') and row[6]
    assert endpoint_module.ticket_fields('not json') == ('not json', '', '')
    with pytest.raises(ValueError):
        again.mark(body['intent_id'], 'fulfilled', '')


def test_the_staff_page_style_is_inline_readable_and_fits_a_desktop_window():
    """2026-10-17: the staff page's text was small and its table wider than a 1024 px window with squeezed columns. The style is
    one inline block (the CSP allows inline style only): viewport meta, a readable base size, a full-width fixed-layout table
    whose six columns share the width, ids that wrap instead of widening it."""
    style = endpoint_module.PAGE_STYLE
    assert 'font:16px/1.6' in style and 'table{width:100%' in style and 'table-layout:fixed' in style and 'overflow-wrap:anywhere' in style
    widths = [int(w) for w in re.findall(r'th:nth-child\(\d\)\{width:(\d+)%\}', style)]
    assert len(widths) == 6 and sum(widths) == 100
    assert 'code{font-size:13px' in style and 'small{font-size:14px' in style
    assert not re.search(r'url\(|@import|<', style)  # nothing the CSP would block, nothing that closes the style element


def test_staff_page_time_is_beijing_with_utc_on_hover():
    """Owner 2026-10-17: Beijing time `YYYY-MM-DD HH:MM:SS` (fixed +08:00), the stored UTC time in the tooltip; empty stays
    empty, an unparsable value is shown as stored."""
    assert endpoint_module.display_time('2026-10-17T14:34:51.370+00:00') == ('2026-10-17 22:34:51', '2026-10-17T14:34:51+00:00')
    assert endpoint_module.display_time('2026-10-17T16:30:05+00:00')[0] == '2026-10-18 00:30:05'  # across midnight
    assert endpoint_module.display_time('2026-10-17T01:00:00')[0] == '2026-10-17 09:00:00'  # naive is UTC
    assert endpoint_module.display_time(None) == ('', '') and endpoint_module.display_time('not a time') == ('not a time', '')
    assert endpoint_module.time_cell('2026-10-17T14:34:51+00:00') == '<span title="UTC 2026-10-17T14:34:51+00:00">2026-10-17 22:34:51</span>'


def test_staff_page_views_keep_conformance_tickets_apart_and_show_pending_customer_tickets_first(tmp_path):
    """Owner 2026-10-17: the default view lists pending customer tickets only (newest first); all customer tickets and the
    conformance check tickets have their own views with counts; status badges; the mark form keeps the view."""
    with running(tmp_path) as e, http(e) as h:
        first, second = request({'summary': '客户工单甲'}), request({'summary': '客户工单乙'})
        check = request({'summary': 'NexLoop conformance check synthetic-run'})
        noop = request({'summary': '端点一致性检查'})
        provider = client(e)
        for body in (first, second, check, noop):
            assert provider.dispatch(**body).state == 'fulfilled'
            time.sleep(0.01)  # distinct created_at, so "newest first" is observable
        page, csrf = staff_session(e, h)
        assert h.post('/staff/mark', data={'csrf': csrf, 'intent_id': first['intent_id'], 'state': 'fulfilled', 'handled_by': '员工甲',
                                           'view': 'pending'}).headers['location'] == '/staff?view=pending'
        pending = h.get('/staff').text
        assert '客户工单乙' in pending and '客户工单甲' not in pending and 'conformance check' not in pending and '端点一致性检查' not in pending
        assert '待处理的客户工单（1）' in pending and '全部客户工单（2）' in pending and '一致性检查工单（2）' in pending
        assert '<span class="badge s-accepted">待处理</span>' in pending and '<form class="mark"' in pending
        everything = h.get('/staff?view=all').text
        assert everything.index('客户工单乙') < everything.index('客户工单甲')  # newest first
        assert '<span class="badge s-fulfilled">已处理</span>' in everything
        checks = h.get('/staff?view=checks').text
        assert 'NexLoop conformance check synthetic-run' in checks and '端点一致性检查' in checks and '客户工单甲' not in checks and '客户工单乙' not in checks
        assert h.get('/staff?view=unknown').text.count('客户工单乙') == 1  # an unknown view falls back to the default


def test_staff_page_dark_theme_and_phone_cards():
    """Layout audit 2026-10-18 batch 3: colours are variables redefined under prefers-color-scheme: dark (badges included); at
    800 px and below the table becomes one card per ticket, each cell naming its column (data-label), with 40 px targets."""
    style = endpoint_module.PAGE_STYLE
    light, dark = style.split('@media (prefers-color-scheme:dark)')[0], style.split('@media (prefers-color-scheme:dark)')[1].split('}}')[0]
    for name in ('--bg', '--surface', '--text', '--muted', '--border', '--pending-bg', '--done-bg', '--fail-bg'):
        assert name + ':' in light and name + ':' in dark, name
    assert 'color:var(--text);background:var(--bg)' in style and 'background:var(--pending-bg)' in style
    phone = style.split('@media (max-width:800px)')[1]
    assert 'table,tbody,tr,td{display:block' in phone and 'tr:first-child{display:none}' in phone
    assert 'content:attr(data-label)' in phone and 'min-height:40px' in phone


def test_staff_page_cells_name_their_columns(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        assert client(e).dispatch(**request({'summary': '客户工单'})).state == 'fulfilled'
        page, _ = staff_session(e, h)
        page = h.get('/staff').text
        for label in ('请求摘要', '来源租户 / 意图 id', '收到时间（北京时间）', '处理人', '状态', '处理结果与备注'):
            assert f'<td data-label="{label}">' in page, label
