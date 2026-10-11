"""NX-031 (dispatcher 2026-10-11): an unanswered guard is not a denial. Actual chain (HTTPS consumer message → relay → Pi Run on
the deterministic Host → runtime worker guard), with a TLS interposer between the Host and the guard that holds the Run's first
model authorization past the Host's 2 s limit (ADR-022 §4) and then drops it. The Host retries the same authorization (the guard
records it idempotently) and the Run succeeds; nothing is denied. The interposer only forwards bytes; it holds no credential."""
import http.client
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_outbound_messages_pg import chain, free_port
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


class Interposer:
    """Forwards Host → guard requests; holds the first `hold` model authorizations beyond the Host's limit, then drops them."""

    def __init__(self, tls, upstream_port, *, hold=1, seconds=2.6):
        self.seen, self.held, self.lock = [], 0, threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                operation = json.loads(body).get('operation')
                with outer.lock:
                    outer.seen.append(operation)
                    drop = operation == 'model' and outer.held < hold
                    outer.held += drop
                if drop:
                    time.sleep(seconds)
                    self.close_connection = True
                    return
                context = ssl.create_default_context(cafile=str(tls / 'host-cert.pem'))
                upstream = http.client.HTTPSConnection('127.0.0.1', upstream_port, context=context, timeout=10)
                try:
                    upstream.request('POST', self.path, body, {k: v for k, v in self.headers.items() if k.lower() in ('authorization', 'content-type', 'content-length')})
                    response = upstream.getresponse()
                    data = response.read()
                finally:
                    upstream.close()
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.port = free_port()
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls / 'host-cert.pem'), str(tls / 'host-key.pem'))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def test_unanswered_model_authorization_is_retried_and_the_run_succeeds(assembled_message, admin, tmp_path):
    f = assembled_message
    proxies = []

    def interpose(cfg):
        guard_port = int(cfg['guard_url'].split(':')[2].split('/')[0])
        tls = __import__('pathlib').Path(cfg['guard_ca_file']).parent
        proxy = Interposer(tls, guard_port)
        proxies.append(proxy)
        cfg['guard_url'] = cfg['guard_url'].replace(':%d/' % guard_port, ':%d/' % proxy.port)

    try:
        with chain(f, admin, tmp_path, body='请问能开发票吗？', host_update=interpose) as c:
            (proxy,) = proxies
            assert proxy.held == 1, proxy.seen
            # The held model authorization was asked again (the retry) and the rest of the Run went through the guard.
            assert proxy.seen.count('model') >= 2, proxy.seen
            (status, result) = admin.execute("select j.status,j.result from runtime.jobs j join runtime.nexloop_message_routes m on m.task_id=j.job_id where m.message_id=%s",
                                              (c['consumer']['id'],)).fetchone()
            assert status == 'succeeded' and result['runtime_outcome'] == 'succeeded' and 'code' not in result, result
    finally:
        for proxy in proxies:
            proxy.close()


def test_guard_that_never_answers_ends_the_task_as_unavailable_not_denied(assembled_message, admin, tmp_path):
    f = assembled_message
    proxies = []

    def interpose(cfg):
        guard_port = int(cfg['guard_url'].split(':')[2].split('/')[0])
        proxy = Interposer(__import__('pathlib').Path(cfg['guard_ca_file']).parent, guard_port, hold=100, seconds=2.2)
        proxies.append(proxy)
        cfg['guard_url'] = cfg['guard_url'].replace(':%d/' % guard_port, ':%d/' % proxy.port)

    try:
        with chain(f, admin, tmp_path, body='请问能开发票吗？', host_update=interpose) as c:
            (proxy,) = proxies
            assert proxy.held == 3, proxy.seen  # bounded: three attempts of the same authorization, then the Run fails
            (status, result) = admin.execute("select j.status,j.result from runtime.jobs j join runtime.nexloop_message_routes m on m.task_id=j.job_id where m.message_id=%s",
                                              (c['consumer']['id'],)).fetchone()
            assert status == 'failed' and result['runtime_outcome'] == 'failed' and result['code'] == 'runtime_authorization_unavailable'
            assert result['failure_code'] == 'runtime_authorization_unavailable', result
    finally:
        for proxy in proxies:
            proxy.close()
