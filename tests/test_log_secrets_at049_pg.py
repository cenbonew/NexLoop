"""AT-049 (M23 S4, 日志秘密): a request whose text contains raw customer words and a key leaves no trace of either in any
ordinary log — every process's stdout/stderr and the PostgreSQL server log — on the success path and on a failure branch.

The actual NX-047 chain runs unchanged (real HTTPS API, relay CLI, Runtime Worker with the real Host/Pi, effect worker, JSON
delivery service, outbound recorder). The customer's message carries a sentinel phrase and a sentinel key; failure branches
send them again through the API (an idempotency conflict raised in SQL, an oversized body, the key as a forged bearer token);
then the reply is delivered and materialized.
The existing process helpers already refuse private tokens in each output; this test adds the two sentinels to that list for
every process the chain starts, and scans the PostgreSQL log at the end. The raw text itself is stored only where it belongs
(the Message and the delivery export, authorized storage), which the test also checks.
"""
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

import test_outbound_messages_pg as chain_module
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401

SENTINEL = '哨兵原话' + secrets.token_hex(6)
KEY = 'sk-sentinel-' + secrets.token_hex(16)


def test_raw_text_and_keys_never_reach_ordinary_logs(assembled_message, admin, tmp_path, monkeypatch):
    seen = []
    original_process, original_once = chain_module.process, chain_module.once

    @contextmanager
    def process(module, arguments, secrets_to_hide=()):
        seen.append(module)
        with original_process(module, arguments, list(secrets_to_hide) + [SENTINEL, KEY]) as child:
            yield child

    def once(module, arguments, secrets_to_hide):
        seen.append(module)
        return original_once(module, arguments, list(secrets_to_hide) + [SENTINEL, KEY])

    monkeypatch.setattr(chain_module, 'process', process)
    monkeypatch.setattr(chain_module, 'once', once)
    body = f'{SENTINEL}：我的账户密钥是 {KEY}，请明天下午前处理。'
    f = assembled_message
    with chain_module.chain(f, admin, tmp_path, body=body) as c:
        assert 'succeeded' in c['runtime_output']
        # Failure branches carrying both sentinels through the API process and SQL errors.
        client, headers, conversation = c['client'], dict(c['headers']), c['conversation_id']
        conflict = client.post('/api/v1/conversations/' + conversation + '/messages', headers={**headers, 'Idempotency-Key': 'nx047-outbound-message-key-1'},
            json={'body': body + ' 改写'})
        assert conflict.status_code == 409 and SENTINEL not in conflict.text
        too_long = client.post('/api/v1/conversations/' + conversation + '/messages', headers={**headers, 'Idempotency-Key': 'at049-too-long-message'},
            json={'body': (body + ' ') * 400})
        assert too_long.status_code == 422 and SENTINEL not in too_long.text
        assert client.get('/api/v1/artifacts/' + 'a' * 32, headers={'Authorization': 'Bearer ' + KEY}).status_code == 401
        material = chain_module.delivery_material(tmp_path)
        with chain_module.delivery_service(c, tmp_path, material):
            assert 'fulfilled' in chain_module.effect_worker(c, tmp_path, material['provider_config'], material['secret'])
        created = c['recorder'].run_once()
        assert len(created) == 1
        tenant = c['tenant']
    assert {'nexloop_eios.http_api', 'nexloop_eios.message_relay_cli', 'nexloop_eios.runtime_worker', 'nexloop_eios.effect_worker'} <= set(seen)
    # The words are stored where they belong (the customer's Message), never in a log.
    stored = admin.execute("select count(*) from runtime.nexloop_conversation_messages where tenant_id=%s and record->>'body' like %s", (tenant, '%' + SENTINEL + '%')).fetchone()[0]
    assert stored >= 1
    host = re.search(r'host=(\S+)', f['original']['pg']).group(1)
    server_log = (Path(host).parent / 'postgres.log').read_text(errors='replace')
    assert server_log and SENTINEL not in server_log and KEY not in server_log
