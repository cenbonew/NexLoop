"""AT-049 unit level: whatever reaches Python logging leaves as one allowlisted line; raw text and keys never do."""
import io
import json
import logging

from nexloop_eios.structured_log import REDACTED, configure

RAW = '客户原话：请把订单改到北京市海淀区某某路'
KEY = 'sk-live-' + 'a' * 32


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_arguments_exceptions_and_unknown_fields_never_leave():
    stream = io.StringIO()
    configure('test-service', stream=stream)
    log = logging.getLogger('nexloop_eios.synthetic')
    log.warning('message_rejected %s', RAW)
    log.warning('effect_intent_unavailable %s', json.dumps({'sqlstate': 'NXC01', 'elapsed_ms': 12.5, 'reason': RAW, 'body': RAW, 'stage': 'admit'}))
    try:
        raise ValueError(f'provider said {RAW} with {KEY}')
    except ValueError:
        log.exception('provider_failed')
    logging.getLogger('psycopg').error('connection failed: password=%s host=%s', KEY, 'db.internal')
    logging.getLogger('uvicorn.error').error(f'Exception in ASGI application: {RAW}')
    text = stream.getvalue()
    assert RAW not in text and KEY not in text and 'db.internal' not in text
    out = lines(stream)
    assert out[0]['event'] == 'message_rejected' and 'detail' not in out[0]
    assert out[1]['detail'] == {'sqlstate': 'NXC01', 'elapsed_ms': 12.5, 'reason': REDACTED, 'stage': 'admit'}
    assert out[2]['event'] == 'provider_failed' and out[2]['exception'] == 'ValueError'
    assert out[3]['event'] == 'log' and 'detail' not in out[3]  # a template that is not a plain event name is not echoed either
    assert out[4]['event'] == 'log' and all(set(line) <= {'ts', 'service', 'level', 'logger', 'event', 'detail', 'exception'} for line in out)


def test_a_record_after_stderr_was_swapped_goes_to_the_current_stderr(monkeypatch):
    """Load-rules flake (batch 23 CI seq 99): the handler used to keep the stderr of configure time; a pool thread logging after
    a test's capture closed failed on that stream and logging printed '--- Logging error ---' with a traceback into the next
    capture. Without a stream the handler now writes to sys.stderr as it is when the record is emitted."""
    import io
    import logging
    import sys
    from nexloop_eios.structured_log import configure
    first, second = io.StringIO(), io.StringIO()
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        monkeypatch.setattr(sys, 'stderr', first)
        configure('claim-extraction-worker')
        first.close()  # the finished test's capture
        monkeypatch.setattr(sys, 'stderr', second)
        logging.getLogger('psycopg.pool').warning('error connecting in %r: %s', 'pool-1', 'timeout')
        out = second.getvalue()
        assert 'Logging error' not in out and '"logger":"psycopg.pool"' in out and 'timeout' not in out
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


def test_service_stderr_drops_only_the_pool_records():
    from support.service_stderr import service_stderr
    err = ('{"event":"error connecting in","level":"warning","logger":"psycopg.pool","service":"x","ts":"t"}\n'
           'Claim Extraction Worker unavailable\n{"logger":"nexloop","event":"tick"}\nnot json {\n')
    assert service_stderr(err) == 'Claim Extraction Worker unavailable\n{"logger":"nexloop","event":"tick"}\nnot json {\n'
