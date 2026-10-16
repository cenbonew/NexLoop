"""10/16 stage (release 5): with the WebChat provider answering 503, an unknown intent was re-queried about once a second and
wrote one unknown event each time (4015 in 45 minutes). The effect worker's ledger wrapper (_FreshExecutor) did not expose the
0174 back-off or the expired-origin stop, so the dispatcher's getattr found neither. Now, through the worker's own wrapper and a
provider that always answers 503: an unknown result is offered again only after the back-off (no second claim at once, one event
per attempt), and after the maximum query window (0199 temporary, 6 hours) automatic re-query stops. Nothing is resent."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from effect_execution_fixture import execution_plan, governed_effect_executor  # noqa: F401
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import EffectProviderConfiguration, HttpEffectProvider
from nexloop_eios.effect_worker import _FreshExecutor
from test_effect_dispatch import accepted


@pytest.fixture
def unavailable_provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self):
            requests.append(self.command)
            length = int(self.headers.get('Content-Length') or 0)
            if length:
                self.rfile.read(length)
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()
        do_GET = do_POST = _reply
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f'http://127.0.0.1:{server.server_address[1]}'
        yield HttpEffectProvider(EffectProviderConfiguration(origin, test_loopback_http=True, timeout=1)), requests
    finally:
        server.shutdown()


def unknown_events(admin, intent):
    return admin.execute("select count(*) from runtime.nexloop_effect_events where intent_id=%s and event_kind='unknown'", (intent,)).fetchone()[0]


def test_the_worker_wrapper_backs_off_unknown_and_stops_after_the_query_window(governed_effect_executor, admin, tmp_path, unavailable_provider):  # noqa: F811
    fixture = governed_effect_executor
    provider, requests = unavailable_provider
    intent = accepted(fixture)['intent_id']
    config = fixture.executor_configuration()
    token = tmp_path / 'executor-token'
    token.write_text(config.pop('service_token'))
    token.chmod(0o600)
    world = config.pop('world')
    with open_backend(**config) as backend:
        ledger = _FreshExecutor(backend, token, world)
        assert callable(ledger.back_off_unknown) and callable(ledger.settle_expired_origin)
        dispatcher = EffectDispatcher(ledger, provider, lease_seconds=30)
        assert dispatcher.run_once()['status'] == 'unknown'  # POST 503: unknown, never resent
        assert unknown_events(admin, intent) == 1 and requests == ['POST']
        for _ in range(5):  # the worker ticks every 0.25 s: nothing is offered again before the back-off ends
            assert dispatcher.run_once() == {'claimed': False}
        assert unknown_events(admin, intent) == 1 and requests == ['POST']
        admin.execute('update runtime.nexloop_effect_outbox set available_at=clock_timestamp() where intent_id=%s', (intent,))
        assert dispatcher.run_once()['status'] == 'unknown'  # reconcile: GET 503, query first, still unknown
        assert requests == ['POST', 'GET'] and unknown_events(admin, intent) == 2
        assert admin.execute('select available_at>clock_timestamp()+interval \'1 second\' from runtime.nexloop_effect_outbox where intent_id=%s',
                             (intent,)).fetchone() == (True,)  # second unknown: 2 s
        # The maximum query window: the first unknown result is older than 6 hours, automatic re-query stops.
        admin.execute("update runtime.nexloop_effect_events set created_at=created_at-interval '7 hours' where intent_id=%s", (intent,))
        assert ledger.back_off_unknown(intent_id=intent) == -1
        assert admin.execute("select state,available_at='infinity'::timestamptz from runtime.nexloop_effect_outbox where intent_id=%s",
                             (intent,)).fetchone() == ('unknown', True)
        assert dispatcher.run_once() == {'claimed': False} and requests == ['POST', 'GET']
