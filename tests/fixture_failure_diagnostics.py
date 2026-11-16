"""Bounded notes for fixture failures; no exception text or response body is reported."""
from contextlib import contextmanager


@contextmanager
def role_activation_diagnostic():
    try:
        yield
    except Exception as error:
        # prepare() deliberately hides its inner cause. Do not guess a SQL/IO cause.
        kind = ('ContextArtifactUnavailable' if type(error).__name__ == 'ContextArtifactUnavailable'
                else 'unknown')
        error.add_note('fixture_diagnostic stage=role_activation error_type=' + kind + ' inner_stage=unknown')
        raise


def conversation_response_id(response):
    """Keep the original JSON/id lookup and exception; annotate failures only."""
    value = None
    try:
        value = response.json()
        return value['id']
    except Exception as error:
        status = response.status_code
        status = str(status) if type(status) is int and 100 <= status <= 599 else 'unknown'
        code = value.get('code') if type(value) is dict else None
        allowed = ('conversation_payload_conflict', 'unauthenticated', 'forbidden',
                   'dependency_unavailable', 'request_timeout', 'invalid_request')
        code = code if type(code) is str and code in allowed else 'unknown'
        kind = {KeyError: 'KeyError', TypeError: 'TypeError', ValueError: 'ValueError'}.get(type(error), 'unknown')
        error.add_note('fixture_diagnostic stage=conversation_create http_status=' + status
                       + ' code=' + code + ' error_type=' + kind)
        raise
