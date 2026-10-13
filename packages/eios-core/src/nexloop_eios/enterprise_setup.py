"""Private enterprise-slot setup for one consumer through existing governed CREATE Actions (ADR-030, NX-037 gap B).

Creates, each step committing on its own under a stable intent (an identical replay returns the same objects):
the enterprise ServiceOffering and the consumer's ConsumerServiceOffering (catalog maintainer; the binding names the relay
Source), and the consumer's enterprise EffectControl (owner), configured for nexloop.enterprise.deliver with the effect
executor. No schema, grant or enrollment is written here; the owner then enrolls the slot with nexloop-relay-enroll.
Credentials are read only from private files and never printed.

    nexloop-enterprise-setup --database-url-file F --signing-key-file F --artifact-root D --owner-credential-file F
        --executor-credential-file F --maintainer-credential-file F --source-credential-file F --recipe-file F
"""
import argparse
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eios.identity.models import SubjectKind
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text

ACTION = 'nexloop.enterprise.deliver'
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)')
_ID = re.compile('[a-f0-9]{64}')


class EnterpriseSetupUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('enterprise_setup_unavailable')


def _future(value):
    if type(value) is not str or not _STAMP.fullmatch(value) or datetime.fromisoformat(value.replace('Z', '+00:00')) <= datetime.now(UTC):
        raise ValueError()


def strict_recipe(text):
    """{schema_version '1.0', request_id (UUID), consumer_id, offering {service_code, title, price_amount, valid_until},
    control {budget_units, valid_until}}; unknown or duplicate keys refused."""
    def pairs(rows):
        out = {}
        for key, value in rows:
            if key in out:
                raise ValueError()
            out[key] = value
        return out

    def constant(unused):
        raise ValueError()
    try:
        if type(text) is not str or len(text.encode('utf8')) > 32768:
            raise ValueError()
        recipe = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if type(recipe) is not dict or set(recipe) != {'schema_version', 'request_id', 'consumer_id', 'offering', 'control'} or recipe['schema_version'] != '1.0':
            raise ValueError()
        if type(recipe['request_id']) is not str or str(uuid.UUID(recipe['request_id'])) != recipe['request_id']:
            raise ValueError()
        if type(recipe['consumer_id']) is not str or not _ID.fullmatch(recipe['consumer_id']):
            raise ValueError()
        offering, control = recipe['offering'], recipe['control']
        if type(offering) is not dict or set(offering) != {'service_code', 'title', 'price_amount', 'valid_until'}:
            raise ValueError()
        if type(offering['service_code']) is not str or not re.fullmatch(r'enterprise\.[a-z0-9][a-z0-9.-]{0,62}', offering['service_code']):
            raise ValueError()
        if type(offering['title']) is not str or not 1 <= len(offering['title']) <= 256:
            raise ValueError()
        price = offering['price_amount']
        if type(price) is not str or not re.fullmatch(r'(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?', price) or Decimal(price) < 0:
            raise ValueError()
        _future(offering['valid_until'])
        if type(control) is not dict or set(control) != {'budget_units', 'valid_until'}:
            raise ValueError()
        if type(control['budget_units']) is not int or not 1 <= control['budget_units'] <= 1000000:
            raise ValueError()
        _future(control['valid_until'])
        return recipe
    except Exception:
        raise EnterpriseSetupUnavailable() from None


def _service(backend, path):
    service = backend.authenticate(read_private_text(path, maximum=16384), world='real')
    session = service._session
    if session.world != 'real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:
        raise ValueError()
    return service


def setup_enterprise(backend, *, owner_credential_file, executor_credential_file, maintainer_credential_file, source_credential_file, recipe):
    """Returns the three ids under the enrollment-file key names (enterprise_control_id, _offering_id, _binding_id)."""
    try:
        from nexloop_eios.service_offerings import enterprise_delivery_example
        recipe = strict_recipe(canonical_payload(recipe))
        owner, executor = _service(backend, owner_credential_file), _service(backend, executor_credential_file)
        maintainer, source = _service(backend, maintainer_credential_file), _service(backend, source_credential_file)
        ids = {name: s._session.authentication for name, s in (('owner', owner), ('executor', executor), ('maintainer', maintainer), ('source', source))}
        if len({a.tenant_id for a in ids.values()}) != 1 or ids['maintainer'].subject_principal_id == ids['source'].subject_principal_id:
            raise ValueError()
        prefix = 'enterprise-setup-' + recipe['request_id']
        o = recipe['offering']
        offering = maintainer.create_object(action_name='ServiceOffering.create', action_version=1, intent_id=prefix + '-offering', type_name='ServiceOffering',
            properties=enterprise_delivery_example(service_code=o['service_code'], title=o['title'], price_amount=o['price_amount'], valid_until=o['valid_until']))
        binding = _service(backend, maintainer_credential_file).create_object(action_name='ConsumerServiceOffering.create', action_version=1,
            intent_id=prefix + '-binding', type_name='ConsumerServiceOffering', properties={'consumer_id': recipe['consumer_id'],
            'offering_id': offering['object_id'], 'offering_revision': 1, 'source_principal': ids['source'].subject_principal_id, 'active': True})
        control = _service(backend, owner_credential_file).create_object(action_name='EffectControl.create', action_version=1, intent_id=prefix + '-control',
            type_name='EffectControl', properties={'consumer_id': recipe['consumer_id'], 'owner_principal': ids['owner'].subject_principal_id,
            'executor_principal': ids['executor'].subject_principal_id, 'budget_units': recipe['control']['budget_units'], 'allow_effect': True,
            'valid_until': recipe['control']['valid_until']})
        _service(backend, owner_credential_file).configure_effect_control(control_id=control['object_id'], control_revision=1,
            executor_token=read_private_text(executor_credential_file, maximum=16384), action=ACTION)
        result = {'enterprise_control_id': control['object_id'], 'enterprise_offering_id': offering['object_id'], 'enterprise_binding_id': binding['object_id']}
        if any(type(v) is not str or not _ID.fullmatch(v) for v in result.values()):
            raise ValueError()
        return {'configured': True, 'tenant_id': ids['owner'].tenant_id, 'world_id': 'real', 'consumer_id': recipe['consumer_id'], **result,
                'delivery_action': ACTION + ':1', 'source_ref': 'principal:' + ids['source'].subject_principal_id,
                'executor_ref': 'principal:' + ids['executor'].subject_principal_id, 'enrolled': False}
    except Exception:
        raise EnterpriseSetupUnavailable() from None


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'Enterprise setup configuration unavailable\n')


def main(argv=None):
    parser = _Parser(description=__doc__.split('\n\n')[0])
    for name in ('database-url-file', 'signing-key-file', 'artifact-root', 'owner-credential-file', 'executor-credential-file',
                 'maintainer-credential-file', 'source-credential-file', 'recipe-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    args = parser.parse_args(argv)
    logger = logging.getLogger('psycopg.pool')
    disabled, logger.disabled = logger.disabled, True
    try:
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}', args.signing_key_id):
            raise ValueError()
        recipe = strict_recipe(read_private_text(args.recipe_file, maximum=32768))
        with open_backend(database_url=read_private_text(args.database_url_file, maximum=16384), artifact_root=args.artifact_root,
                          signing_key_file=args.signing_key_file, signing_key_id=args.signing_key_id) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db) != 'nexloop_api':
                    raise ValueError()
            result = setup_enterprise(backend, owner_credential_file=args.owner_credential_file, executor_credential_file=args.executor_credential_file,
                                      maintainer_credential_file=args.maintainer_credential_file, source_credential_file=args.source_credential_file, recipe=recipe)
        print(canonical_payload(result))
        return 0
    except Exception:
        print('Enterprise setup unavailable', file=sys.stderr)
        return 1
    finally:
        logger.disabled = disabled


if __name__ == '__main__':
    raise SystemExit(main())
