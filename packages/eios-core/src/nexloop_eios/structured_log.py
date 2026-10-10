"""NX-030 / AT-049: one structured, allowlisted log line per record for every NexLoop process.

Ordinary logs carry IDs, states, codes and durations only (runbook §63); full model requests and message text live in
authorized Artifacts and business tables, never in logs. This handler enforces that for anything that reaches Python logging,
including third-party loggers and uvicorn's error log:

* the event is the record's message template (``record.msg``), never its formatted arguments;
* a detail object is taken only from a single JSON-object argument, and only for allowlisted keys;
* every string value must look like a code or an ID (short, no spaces); anything else becomes ``[redacted]``;
* exceptions are reduced to their class name; no traceback, no message, no SQL.
"""
import json
import logging
import re
import sys
from datetime import UTC, datetime

FIELDS = frozenset({'status', 'code', 'sqlstate', 'reason', 'stage', 'elapsed_ms', 'attempt', 'attempts', 'deadline_exceeded', 'retryable', 'count',
                    'run_id', 'intent_id', 'task_id', 'message_id', 'request_id', 'conversation_id', 'tenant', 'world', 'queue', 'feed'})
_CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}')
_TEMPLATE = re.compile(r'[A-Za-z][A-Za-z0-9_.: -]{0,79}')
REDACTED = '[redacted]'


def clean(value):
    if value is None or type(value) is bool or (type(value) in (int, float)):
        return value
    if type(value) is str and _CODE.fullmatch(value):
        return value
    return REDACTED


class StructuredFormatter(logging.Formatter):
    def __init__(self, service='nexloop'):
        super().__init__()
        self.service = clean(service) if type(service) is str else 'nexloop'

    def format(self, record):
        template = record.msg if isinstance(record.msg, str) else ''
        event = template.split('%', 1)[0].strip()
        line = {'ts': datetime.fromtimestamp(record.created, UTC).isoformat(timespec='milliseconds'), 'service': self.service,
                'level': record.levelname.lower(), 'logger': clean(record.name), 'event': event if _TEMPLATE.fullmatch(event or '') else 'log'}
        detail = None
        if isinstance(record.args, tuple) and len(record.args) == 1:
            detail = record.args[0]
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except ValueError:
                    detail = None
        if isinstance(record.args, dict):
            detail = record.args
        if isinstance(detail, dict):
            kept = {k: clean(v) for k, v in detail.items() if k in FIELDS}
            if kept:
                line['detail'] = kept
        if record.exc_info and record.exc_info[0] is not None:
            line['exception'] = clean(record.exc_info[0].__name__)
        return json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def configure(service, *, stream=None, level=logging.WARNING):
    """Replace every root handler with the structured one (idempotent); uvicorn's loggers propagate to it."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(StructuredFormatter(service))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        named = logging.getLogger(name)
        for existing in list(named.handlers):
            named.removeHandler(existing)
        named.propagate = True
    return handler


def uvicorn_log_config(service):
    """For uvicorn.run(log_config=...): every uvicorn logger through the structured formatter."""
    return {'version': 1, 'disable_existing_loggers': False,
            'formatters': {'structured': {'()': 'nexloop_eios.structured_log.StructuredFormatter', 'service': service}},
            'handlers': {'default': {'class': 'logging.StreamHandler', 'formatter': 'structured', 'stream': 'ext://sys.stderr'}},
            'loggers': {'uvicorn': {'handlers': ['default'], 'level': 'WARNING', 'propagate': False},
                        'uvicorn.error': {'handlers': ['default'], 'level': 'WARNING', 'propagate': False},
                        'uvicorn.access': {'handlers': ['default'], 'level': 'WARNING', 'propagate': False}},
            'root': {'handlers': ['default'], 'level': 'WARNING'}}
