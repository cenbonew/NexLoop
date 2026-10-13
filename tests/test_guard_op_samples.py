"""AT-060: the runtime guard's latency per operation (kind guard_op, 0430). The overall per-process sample (kind guard) is
unchanged; each call is also counted under the operation the Host named, with fixed latency buckets. Labels only: the guard
answers exactly as before. Actual HTTPS guard with a fake worker; the SQL accepts numbers and known operations only."""
import threading

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.observability import GUARD_OPERATIONS, GuardTimings
from test_agent_host import files
from test_runtime_guard_transport import PATH, BODY, client


def test_overall_sample_is_unchanged_and_each_operation_gets_its_own():
    timings = GuardTimings()
    for ms in [10] * 98 + [1999, 2500]:
        timings.observe(ms, operation='model')
    timings.observe(40, operation='inspect', error=True)
    timings.observe(30, operation='not-an-operation')
    timings.drop()
    sample, _, by_operation = timings.drain_with_operations()
    assert sample == {'calls': 103, 'timeouts': 1, 'errors': 2, 'p50_ms': 10.0, 'p95_ms': 10.0, 'max_ms': 2500.0}
    assert set(by_operation) == {'model', 'inspect', 'other'}
    assert by_operation['model'] == {'calls': 100, 'timeouts': 1, 'errors': 0, 'p50_ms': 10.0, 'p95_ms': 10.0, 'max_ms': 2500.0,
                                     'le_50': 98, 'le_100': 0, 'le_250': 0, 'le_500': 0, 'le_1000': 0, 'le_1500': 0, 'le_2000': 1, 'gt_2000': 1}
    assert by_operation['inspect']['errors'] == 1 and by_operation['other']['calls'] == 1
    empty, _, none = timings.drain_with_operations()
    assert empty is None and none == {}
    timings.observe(5)
    assert timings.drain()[0]['calls'] == 1  # drain() keeps its old shape


class Worker:
    def __init__(self):
        self.calls = 0

    def authorize_runtime_activation(self, **_):
        self.calls += 1
        raise AuthorizationUnavailable('synthetic')


@pytest.mark.parametrize('path,body,label', [
    (PATH, BODY, 'model'),
    (PATH, {**BODY, 'model_result': {}}, 'model_result'),
    (PATH, {**BODY, 'operation': 'inspect'}, 'inspect'),
    (PATH, {**BODY, 'operation': 'bogus'}, 'other'),
    ('/internal/v1/runtime/effects/find', {'activation_ref': 'activation_x', 'command': {'run_id': 'r'}, 'intent_id': 'i'}, 'effect_find'),
])
def test_the_guard_labels_each_call_with_its_operation_and_answers_as_before(tmp_path, monkeypatch, path, body, label):
    from nexloop_eios import observability
    from nexloop_eios.runtime_control import create_runtime_guard_server
    timings = GuardTimings()
    monkeypatch.setattr(observability, 'GUARD', timings)
    _, key = files(tmp_path)
    server = create_runtime_guard_server(Worker(), port=0, key_file=key, certificate_file=tmp_path / 'host-cert.pem', tls_key_file=tmp_path / 'host-key.pem')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with client(key) as http:
            response = http.post(f'https://127.0.0.1:{server.server_port}{path}', headers={'Authorization': 'Bearer ' + key.read_text()}, json=body)
            unauthenticated = http.post(f'https://127.0.0.1:{server.server_port}{PATH}', headers={'Authorization': 'Bearer wrong'}, json=BODY)
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
    assert response.status_code in (400, 403, 503) and unauthenticated.status_code == 401
    sample, _, by_operation = timings.drain_with_operations()
    assert sample['calls'] == 2
    expected = {label: 1} if label != 'other' else {'other': 2}
    if label != 'other':
        expected['other'] = 1  # the unauthenticated call is never labelled by its body
    assert {k: v['calls'] for k, v in by_operation.items()} == expected


def test_sql_accepts_guard_op_numbers_for_known_operations_only(admin, pg):
    bootstrap(admin)
    sample = {'calls': 3, 'timeouts': 0, 'errors': 0, 'p50_ms': 1.0, 'p95_ms': 2.0, 'max_ms': 2.0, 'le_50': 3, 'le_100': 0, 'le_250': 0,
              'le_500': 0, 'le_1000': 0, 'le_1500': 0, 'le_2000': 0, 'gt_2000': 0}
    with psycopg.connect(make_conninfo(pg, user='nexloop_domain_worker'), autocommit=True) as db:
        record = lambda kind, service, value: db.execute('select authz.nexloop_record_process_sample(%s,%s,%s,now(),60,%s)', (kind, service, 'synthetic:1', Jsonb(value)))
        for operation in GUARD_OPERATIONS:
            record('guard_op', operation, sample)
        for kind, service, value in (('guard_op', 'drop_table', sample), ('guard_op', 'model', {**sample, 'operation': 1}),
                                     ('guard_op', 'model', {'calls': 'many'}), ('guard', 'runtime-guard', {'le_50': 1})):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                record(kind, service, value)
    assert admin.execute("select count(*) from runtime.nexloop_process_samples where kind='guard_op'").fetchone() == (len(GUARD_OPERATIONS),)
