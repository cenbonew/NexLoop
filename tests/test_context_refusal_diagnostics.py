"""10/18 stage (trial e80e360c stopped by critical_alert): a customer's message failed five relay attempts and its fallback reply
with ContextArtifactUnavailable, and nothing said which Context check refused. The refusing check now reaches the relay's log
line and the owner's escalation evidence, as a code (never a value, a body or a statement)."""
import json
import logging

from nexloop_eios.context_artifacts import check_code
from nexloop_eios.contact_restrictions import ReplyGuaranteeWorker
from nexloop_eios.structured_log import StructuredFormatter


def test_the_refusing_check_becomes_a_code():
    assert check_code(('InsufficientPrivilege', 'context v6 pinned source missing')) == 'context_v6_pinned_source_missing'
    assert check_code(('ValueError', None)) == 'python:ValueError'
    assert check_code(('InsufficientPrivilege', 'Value "quoted" from a body')) == 'python:InsufficientPrivilege'  # never free text
    assert check_code(None) is None
    # The code survives the allowlisted log line unredacted.
    record = logging.LogRecord('nexloop.message_relay', logging.WARNING, __file__, 1, 'message relay attempt failed %s',
                               ({'message_id': 'a' * 64, 'stage': 'context_prepare', 'reason': 'ContextArtifactUnavailable',
                                 'code': 'context_v6_pinned_source_missing'},), None)
    line = json.loads(StructuredFormatter('message-relay').format(record))
    assert line['detail'] == {'message_id': 'a' * 64, 'stage': 'context_prepare', 'reason': 'ContextArtifactUnavailable', 'code': 'context_v6_pinned_source_missing'}


class _Feed:
    def __init__(self):
        self.retries = []

    def claim(self, limit, lease_seconds):
        return [{'item_key': 'reply:m', 'fence': 1, 'payload': {'message_id': 'm' * 64}}]

    def retry(self, **kw):
        self.retries.append(kw)
        return 'pending'

    def complete(self, **kw):
        return 'completed'


class _Port:
    def __init__(self):
        self.escalations = []

    def state(self, message_id):
        return {'settled': False, 'escalated': False, 'restricted': False, 'in_flight': False}

    def escalate(self, message_id, reason, detail):
        self.escalations.append((reason, detail))


class _Launcher:
    def __init__(self, check):
        self.check = check

    def start(self, *, message_id, state):
        self.last_check = self.check
        raise RuntimeError('context_artifact_unavailable')


def _worker(launcher):
    worker = ReplyGuaranteeWorker.__new__(ReplyGuaranteeWorker)
    worker.feed, worker.port, worker.launcher = _Feed(), _Port(), launcher
    worker.policy = {'fallback': {'batch': 1, 'lease_seconds': 30, 'run_ttl_seconds': 60, 'run_budget': {'active_timeout_seconds': 60}, 'max_attempts': 3}}
    return worker


def test_a_failed_fallback_names_the_refusing_check_in_the_escalation():
    worker = _worker(_Launcher('context_v6_pinned_source_missing'))
    worker.run_once()
    assert worker.port.escalations == [('fallback_failed', {'error': 'RuntimeError', 'restricted': False, 'check': 'context_v6_pinned_source_missing'})]
    # A failure before any Context check: the evidence is as before (no check key), never a stale one from an earlier message.
    launcher = _Launcher(None)
    launcher.last_check = 'stale_from_before'
    worker = _worker(launcher)
    worker.run_once()
    assert worker.port.escalations == [('fallback_failed', {'error': 'RuntimeError', 'restricted': False})]
