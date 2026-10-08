"""Actual TLS transport negatives only; no fake authorized EIOS identity.

The concrete authority port has no Backend and cannot authorize any effect.
Successful business delivery requires separately coordinated real PG tests.
"""
from contextlib import contextmanager
import socket
import threading
import httpx
import pytest
from test_agent_host import files, free_port, trusted_tls
from nexloop_eios.local_json_delivery import DeliveryAuthorityPort, DeliveryServerConfiguration, JsonExportStore, make_server


@contextmanager
def delivery_transport(tmp_path):
    root, key = files(tmp_path); port = free_port()
    config = DeliveryServerConfiguration('https://127.0.0.1:' + str(port), tmp_path/'host-cert.pem', tmp_path/'host-key.pem', key, .5)
    store = JsonExportStore(root)
    authority = DeliveryAuthorityPort(None, key, 'a'*64)
    server = make_server(config, authority, store)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        with httpx.Client(base_url=config.origin, verify=trusted_tls(key), trust_env=False, timeout=2) as client:
            yield client, key, root
    finally:
        server.shutdown(); server.server_close(); thread.join(5); store.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('fault', ['wrong_key','browser_origin','wrong_host','duplicate_authorization','wrong_path'])
def test_tls_request_negative_creates_no_product(tmp_path, fault, capsys):
    with delivery_transport(tmp_path) as (client, key, root):
        token = key.read_text(); headers = [('Authorization', 'Bearer ' + token)]
        if fault == 'wrong_key': headers = [('Authorization','Bearer synthetic-incorrect-transport-key')]
        if fault == 'browser_origin': headers.append(('Origin','https://browser.example'))
        if fault == 'wrong_host': headers.append(('Host','different.example'))
        if fault == 'duplicate_authorization': headers.append(('Authorization','Bearer '+token))
        path='/v1/effects/00000000-0000-0000-0000-000000000001' if fault!='wrong_path' else '/redirect'
        response = client.get(path, headers=headers)
        assert response.status_code == 503 and response.json() == {'error':'local_delivery_unavailable'}
        assert list(root.iterdir()) == []
        assert token not in response.text
    captured=capsys.readouterr(); assert token not in captured.out+captured.err


def test_tls_valid_transport_cannot_authorize_without_real_backend(tmp_path):
    with delivery_transport(tmp_path) as (client,key,root):
        response=client.post('/v1/effects',headers={'Authorization':'Bearer '+key.read_text(),'Idempotency-Key':'00000000-0000-0000-0000-000000000001'},
            json={'intent_id':'00000000-0000-0000-0000-000000000001','payload_digest':'a'*64,'parameters':{'message':'not authorized'}})
        assert response.status_code==503 and list(root.iterdir())==[]
