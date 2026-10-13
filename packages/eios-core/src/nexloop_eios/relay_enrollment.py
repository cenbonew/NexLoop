"""Relay multi-consumer (0160): enroll a consumer for a relay principal, withdraw it, or read the enrollment.

The caller is the owner principal of the consumer's EffectControl (the business setup owner); SQL re-checks that, that the
EffectControl, offering and binding (and optional Role link and plan step) all belong to the consumer, and that the relay
principal is an active service principal other than the caller. A relay can never enroll or withdraw itself. Enrolling the
same profile again (same or new intent) is a replay; the in-process functions are what the NX-031 loader calls while it builds
its template; the CLI is the deployment step after business and catalog setup.

    nexloop-relay-enroll --database-url-file F --signing-key-file F --owner-credential-file F --artifact-root D --enrollment-file F
"""
import argparse
import json
import logging
import re
from pathlib import Path

from nexloop_eios.plan_reevaluation import PlanUnavailable, _SignedPort
from nexloop_eios.private_configuration import read_private_text

ENROLL = 'eios:action:nexloop.relay.enroll:1'
WITHDRAW = 'eios:action:nexloop.relay.withdraw:1'
_ID = re.compile('[a-f0-9]{64}')
_INTENT = re.compile('[A-Za-z0-9._:-]{8,160}')


class RelayEnrollmentUnavailable(RuntimeError):
    pass


class _Port(_SignedPort):
    PROTOCOL = 'nexloop-relay-enrollment-v1'
    FUNCTION = 'nexloop_relay_enrollment_command'

    def __init__(self, service, action):
        self.ACTION = action
        super().__init__(service._backend._pool, service._session, service._backend._signer)


def _call(service, action, payload):
    try:
        with service._backend._lock:
            service._backend._assert_open()
            return _Port(service, action)._signed(payload)
    except (PlanUnavailable, RelayEnrollmentUnavailable):
        raise RelayEnrollmentUnavailable('relay enrollment unavailable') from None
    except Exception as error:
        # 22023 (invalid / conflict) stays distinguishable from authority refusal; no SQL text leaves.
        code = getattr(error, 'sqlstate', None)
        raise RelayEnrollmentUnavailable('relay enrollment invalid' if code == '22023' else 'relay enrollment unavailable') from None


def _ids(**values):
    for name, value in values.items():
        if value is not None and (type(value) is not str or not _ID.fullmatch(value)):
            raise RelayEnrollmentUnavailable(f'{name} invalid')


def enroll(service, *, consumer_id, control_id, offering_id, binding_id, relay_principal, intent_id, role_id=None, link_id=None, step_id=None,
           enterprise=None):
    """Enroll (or replay the enrollment of) one consumer for ``relay_principal``; returns state and enrollment_revision."""
    _ids(consumer_id=consumer_id, control_id=control_id, offering_id=offering_id, binding_id=binding_id, role_id=role_id, link_id=link_id, step_id=step_id)
    if type(intent_id) is not str or not _INTENT.fullmatch(intent_id) or type(relay_principal) is not str or not 1 <= len(relay_principal) <= 512:
        raise RelayEnrollmentUnavailable('enrollment invalid')
    if (role_id is None) != (link_id is None) or (role_id is None) != (step_id is None):
        raise RelayEnrollmentUnavailable('role, link and step go together')
    payload = {'verb': 'enroll', 'consumer_id': consumer_id, 'control_id': control_id, 'offering_id': offering_id, 'binding_id': binding_id,
               'relay_principal': relay_principal, 'intent_id': intent_id}
    if role_id is not None:
        payload.update(role_id=role_id, link_id=link_id, step_id=step_id)
    if enterprise is not None:
        # ADR-030: the consumer's enterprise slot for Message Runs: its own EffectControl (budget), the enterprise offering
        # and the consumer's binding to it. All three or none; enrolling again without it clears the slot.
        if type(enterprise) is not dict or set(enterprise) != {'control_id', 'offering_id', 'binding_id'}:
            raise RelayEnrollmentUnavailable('enterprise slot invalid')
        _ids(**{'enterprise_' + key: value for key, value in enterprise.items()})
        payload['enterprise'] = dict(enterprise)
    return _call(service, ENROLL, payload)


def withdraw(service, *, consumer_id, intent_id):
    _ids(consumer_id=consumer_id)
    if type(intent_id) is not str or not _INTENT.fullmatch(intent_id):
        raise RelayEnrollmentUnavailable('intent invalid')
    return _call(service, WITHDRAW, {'verb': 'withdraw', 'consumer_id': consumer_id, 'intent_id': intent_id})


def read(service, *, consumer_id):
    """The current enrollment (ids and state only) for the control owner; None when the consumer was never enrolled."""
    _ids(consumer_id=consumer_id)
    return _call(service, ENROLL, {'verb': 'read', 'consumer_id': consumer_id})


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'Relay enrollment configuration unavailable\n')


def main(argv=None):
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('relay-enrollment')
    parser = _Parser(description=__doc__.split('\n\n')[0])
    for name in ('database-url-file', 'signing-key-file', 'owner-credential-file', 'artifact-root', 'enrollment-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    args = parser.parse_args(argv)
    logger = logging.getLogger('psycopg.pool')
    disabled = logger.disabled
    logger.disabled = True
    try:
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.backend import open_backend
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}', args.signing_key_id):
            raise ValueError()
        spec = json.loads(read_private_text(args.enrollment_file, maximum=16384))
        allowed = {'consumer_id', 'control_id', 'offering_id', 'binding_id', 'relay_principal', 'intent_id', 'role_id', 'link_id', 'step_id'}
        if type(spec) is not dict or not set(spec) <= allowed:
            raise ValueError()
        with open_backend(database_url=read_private_text(args.database_url_file, maximum=16384), artifact_root=args.artifact_root,
                          signing_key_file=args.signing_key_file, signing_key_id=args.signing_key_id) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db) != 'nexloop_api':
                    raise ValueError()
            owner = backend.authenticate(read_private_text(args.owner_credential_file, maximum=16384), world='real')
            result = enroll(owner, **spec)
        print(json.dumps({'enrolled': True, **result}, separators=(',', ':')))
        return 0
    except Exception:
        print('Relay enrollment unavailable', flush=True)
        return 1
    finally:
        logger.disabled = disabled


if __name__ == '__main__':
    raise SystemExit(main())
