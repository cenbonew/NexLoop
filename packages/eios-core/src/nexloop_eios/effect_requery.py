"""Owner-ordered re-query of stale unknown effects (0205): one order file, one command.

    python -m nexloop_eios.effect_requery --database-url-file F --signing-key-file F --service-credential-file F \
        --artifact-root DIR --world real --order-file ORDER.json

ORDER.json (private, owner-written): {"schema": "nexloop-effect-requery-order/1", "confirmation_ref": "owner-...",
"intents": ["<intent uuid>", ...]} (at most 16). It runs as the effect worker (its own database role and service credential):
for each intent the executor arms one more query of its own stopped unknown intent whose origin Run expired (0205). The
running effect worker then queries the provider on its existing query-first path: fulfilled is reconciled, not_found or an
unavailable query stays unknown and stops again. Nothing is sent here. Output: one JSON line per intent (ids and codes only).
Exit 0 when every intent was armed, 1 when any was not (its reason is printed), 2 on unusable configuration.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import uuid

from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.private_configuration import read_private_text

SCHEMA = 'nexloop-effect-requery-order/1'
REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Effect requery configuration unavailable\n')


def _arguments(argv):
    parser = _Parser(description=__doc__.splitlines()[0])
    for name in ('database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root', 'order-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    parser.add_argument('--world', required=True, choices=['real'])
    return parser.parse_args(argv)


def load_order(path):
    """The owner's order: schema, an opaque confirmation reference and 1-16 distinct intent uuids; anything else refused."""
    order = json.loads(read_private_text(path, maximum=16384))
    if (type(order) is not dict or set(order) != {'schema', 'confirmation_ref', 'intents'} or order['schema'] != SCHEMA
            or type(order['confirmation_ref']) is not str or not REFERENCE.fullmatch(order['confirmation_ref'])
            or type(order['intents']) is not list or not 1 <= len(order['intents']) <= 16 or len(set(order['intents'])) != len(order['intents'])):
        raise ValueError('effect_requery_order_invalid')
    for intent in order['intents']:
        if type(intent) is not str or str(uuid.UUID(intent)) != intent:
            raise ValueError('effect_requery_order_invalid')
    return order


def run(arguments, out=sys.stdout):
    order = load_order(arguments.order_file)
    dsn = read_private_text(arguments.database_url_file, maximum=16384)
    with open_backend(database_url=dsn, artifact_root=arguments.artifact_root, signing_key_file=arguments.signing_key_file,
                      signing_key_id=arguments.signing_key_id, pool_max_size=1) as backend:
        with backend._pool.connection() as connection:
            if verify_application_role(connection) != 'nexloop_action_worker':
                raise ValueError('effect_requery_role')
        service = backend.authenticate(read_private_text(arguments.service_credential_file, maximum=16384), world=arguments.world)
        armed = True
        for intent in order['intents']:
            try:
                result = service.requery_stale_unknown(intent_id=intent, confirmation_ref=order['confirmation_ref'])
                line = {k: result[k] for k in ('intent_id', 'armed', 'reason', 'orders', 'state') if k in result}
            except Exception:
                line = {'intent_id': intent, 'armed': False, 'reason': 'unavailable'}  # not this executor's intent, or no database
            line['intent_id'] = str(line['intent_id'])
            armed = armed and line['armed'] is True
            print(json.dumps(line, separators=(',', ':')), file=out, flush=True)
        return 0 if armed else 1


def main(argv=None):
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('effect-requery')
    arguments = _arguments(argv)
    try:
        return run(arguments)
    except Exception:
        print('Effect requery unavailable', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
