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
