"""In-memory diagnostics only: no HTTP, PG, files or subprocesses."""
import builtins
import socket
import subprocess

import pytest

from fixture_failure_diagnostics import conversation_response_id, role_activation_diagnostic


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('stub diagnostics must not perform IO')
    for owner, name in ((builtins, 'open'), (socket, 'socket'),
                        (socket, 'create_connection'), (subprocess, 'Popen')):
        monkeypatch.setattr(owner, name, forbidden)


class Response:
    def __init__(self, value, status=200, error=None):
        self.value, self.status_code, self.error, self.calls = value, status, error, 0

    def json(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def test_success_is_unmodified_even_if_status_is_not_200():
    # A status assertion would strengthen the original test; this helper must not.
    value = object()
    response = Response({'id': value}, 503)
    assert conversation_response_id(response) is value
    assert response.calls == 1
    with role_activation_diagnostic():
        pass


@pytest.mark.parametrize('code', ['conversation_payload_conflict', 'unauthenticated', 'forbidden',
                                 'dependency_unavailable', 'request_timeout', 'invalid_request'])
def test_known_codes_are_bounded_notes_on_original_key_error(code):
    response = Response({'code': code, 'body': 'PRIVATE_SENTINEL'}, 503)
    with pytest.raises(KeyError) as caught:
        conversation_response_id(response)
    assert caught.value.args == ('id',)
    assert caught.value.__notes__ == ['fixture_diagnostic stage=conversation_create http_status=503 code='
                                      + code + ' error_type=KeyError']
    assert response.calls == 1


@pytest.mark.parametrize('code', ['PRIVATE_SENTINEL', {'private': 'PRIVATE_SENTINEL'}, None, ['forbidden']])
def test_unrecognized_code_and_status_are_unknown(code):
    with pytest.raises(KeyError) as caught:
        conversation_response_id(Response({'code': code}, 'PRIVATE_SENTINEL'))
    assert caught.value.__notes__ == ['fixture_diagnostic stage=conversation_create http_status=unknown'
                                      ' code=unknown error_type=KeyError']


@pytest.mark.parametrize('error', [ValueError('PRIVATE_SENTINEL'), RuntimeError('PRIVATE_SENTINEL')])
def test_json_exception_identity_and_cause_are_preserved(error):
    cause = LookupError('PRIVATE_SENTINEL')
    error.__cause__ = cause
    response = Response(None, 422, error)
    with pytest.raises(type(error)) as caught:
        conversation_response_id(response)
    assert caught.value is error and caught.value.__cause__ is cause
    assert 'PRIVATE_SENTINEL' not in str(error.__notes__)
    assert response.calls == 1


def test_non_object_json_keeps_original_type_error():
    with pytest.raises(TypeError) as caught:
        conversation_response_id(Response([]))
    assert caught.value.__notes__ == ['fixture_diagnostic stage=conversation_create http_status=200'
                                      ' code=unknown error_type=TypeError']


@pytest.mark.parametrize('known', [True, False])
def test_role_note_preserves_exception_identity_and_never_claims_inner_cause(known):
    kind = type('ContextArtifactUnavailable', (RuntimeError,), {}) if known else RuntimeError
    error = kind('PRIVATE_SENTINEL')
    error.add_note('existing note')
    with pytest.raises(kind) as caught:
        with role_activation_diagnostic():
            raise error from None
    assert caught.value is error and error.__suppress_context__
    expected = 'ContextArtifactUnavailable' if known else 'unknown'
    assert error.__notes__ == ['existing note', 'fixture_diagnostic stage=role_activation error_type='
                               + expected + ' inner_stage=unknown']
