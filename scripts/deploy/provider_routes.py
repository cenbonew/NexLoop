#!/usr/bin/env python3
"""G-ROUTES: generate the action-worker's provider routes and provider configuration files (NX-037-provider-routing.md §2).

The action-worker reads `--provider-routes-file` (effect_worker._routes): {"schema": "nexloop-effect-provider-routes/1",
"routes": {"<action stable name>": "<absolute provider config file>"}}, and each provider config (effect_worker._provider):
{"origin", "credential_file", "ca_file", "timeout"[, "connect_address"]}. A typo in any of them stops the worker, so this script
writes them from a few private parameters instead of by hand:

- the routed Actions come from the published Actions (deploy/configuration/system-actions.v1.json: an effect Action is one whose
  target_systems include "service" and that the effect worker dispatches, effect_contexts.EFFECT_ACTIONS);
- `nexloop.service.request` is always routed to the NexLoop WebChat delivery service on the action-worker's loopback
  (https://127.0.0.1:<--webchat-delivery-port>);
- `nexloop.enterprise.deliver` is routed only when the enterprise endpoint is given (--enterprise-origin together with
  --enterprise-connect-address); otherwise no enterprise route and no enterprise config file are written;
- every path written is the container path of the stage Compose secret (/run/secrets/<secret name>), never a host path.

Credentials and CA certificates are not read or written here: the files only name them. Output files are new (never
overwritten), mode 0600, named as the Compose secret sources under secrets/action-worker/. Prints one JSON line (file names and
routed Actions only). Exit: 0 written, 2 refused.

    scripts/deploy/provider_routes.py --out-dir <NEW_PRIVATE_DIR> --webchat-delivery-port <PORT>
        [--enterprise-origin https://<host>[:<port>] --enterprise-connect-address <IP> | --enterprise-placeholders] [--timeout 2]

--enterprise-placeholders (no enterprise endpoint): also writes enterprise-provider-config ({}) and enterprise-provider-credential
(random, 32 bytes URL-safe), only to satisfy the action-worker's Compose secret mounts; they are not in the routes file and the
worker never reads them. enterprise-provider-ca.crt is the caller's (for example a copy of the WebChat CA); not read here.
"""
import argparse
import ipaddress
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
from nexloop_eios.effect_contexts import EFFECT_ACTIONS  # noqa: E402
from nexloop_eios.effect_provider import EffectProviderConfiguration  # noqa: E402
from nexloop_eios.effect_worker import ROUTES_SCHEMA, _ACTION  # noqa: E402

SYSTEM_ACTIONS = ROOT / 'deploy/configuration/system-actions.v1.json'
SECRETS = '/run/secrets'
WEBCHAT_ACTION, ENTERPRISE_ACTION = 'nexloop.service.request', 'nexloop.enterprise.deliver'
# Compose secret name (container path /run/secrets/<name>) -> source file name under secrets/action-worker/ (compose.app.example.yaml).
FILES = {
    'action_worker_provider_routes': 'provider-routes',
    'action_worker_webchat_provider': 'webchat-provider-config',
    'action_worker_enterprise_provider': 'enterprise-provider-config',
}
CREDENTIALS = {
    WEBCHAT_ACTION: ('action_worker_webchat_provider', 'action_worker_webchat_provider_credential', 'action_worker_webchat_provider_ca'),
    ENTERPRISE_ACTION: ('action_worker_enterprise_provider', 'action_worker_enterprise_provider_credential', 'action_worker_enterprise_provider_ca'),
}


class Refused(ValueError):
    pass


def effect_actions(path=SYSTEM_ACTIONS):
    """Published Actions the effect worker dispatches to a service provider."""
    published = json.loads(Path(path).read_text())['actions']
    names = {a['stable_name'] for a in published if 'service' in a.get('target_systems', [])} & set(EFFECT_ACTIONS)
    if WEBCHAT_ACTION not in names:
        raise Refused('nexloop.service.request is not a published effect Action')
    return names


def _config(origin, secret, credential, ca, timeout, connect_address=None):
    body = {'origin': origin, 'credential_file': f'{SECRETS}/{credential}', 'ca_file': f'{SECRETS}/{ca}', 'timeout': timeout}
    if connect_address is not None:
        body['connect_address'] = connect_address
    try:
        EffectProviderConfiguration(**body)  # the same validation the worker applies (origin, https, numeric address, timeout)
    except ValueError:
        raise Refused(f'{secret}: provider configuration invalid') from None
    return body


def build(*, webchat_delivery_port, enterprise_origin=None, enterprise_connect_address=None, timeout=2.0, system_actions=SYSTEM_ACTIONS):
    """Returns {file name: JSON body}. Raises Refused on any invalid input."""
    if type(webchat_delivery_port) is not int or not 1024 <= webchat_delivery_port <= 65535:
        raise Refused('webchat delivery port invalid')
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise Refused('timeout invalid (0 < timeout <= 30 s, as the worker accepts; the deployed value is 2)')
    if (enterprise_origin is None) != (enterprise_connect_address is None):
        raise Refused('enterprise origin and connect address are given together or not at all')
    if enterprise_connect_address is not None:
        try:
            ipaddress.ip_address(enterprise_connect_address)
        except ValueError:
            raise Refused('enterprise connect address must be a numeric address') from None
    published = effect_actions(system_actions)
    wanted = {WEBCHAT_ACTION} | ({ENTERPRISE_ACTION} if enterprise_origin else set())
    if not wanted <= published:
        raise Refused('a routed Action is not a published effect Action: ' + ','.join(sorted(wanted - published)))
    files, routes = {}, {}
    secret, credential, ca = CREDENTIALS[WEBCHAT_ACTION]
    files[FILES[secret]] = _config(f'https://127.0.0.1:{webchat_delivery_port}', secret, credential, ca, timeout)
    routes[WEBCHAT_ACTION] = f'{SECRETS}/{secret}'
    if enterprise_origin:
        if not enterprise_origin.startswith('https://'):
            raise Refused('enterprise origin must be https')
        secret, credential, ca = CREDENTIALS[ENTERPRISE_ACTION]
        files[FILES[secret]] = _config(enterprise_origin, secret, credential, ca, timeout, enterprise_connect_address)
        routes[ENTERPRISE_ACTION] = f'{SECRETS}/{secret}'
    if not all(_ACTION.fullmatch(a) for a in routes) or not 1 <= len(routes) <= 16:
        raise Refused('routes invalid')
    files[FILES['action_worker_provider_routes']] = {'schema': ROUTES_SCHEMA, 'routes': routes}
    return files


def write(out_dir, files):
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise Refused('output directory exists')
    out_dir.mkdir(mode=0o700, parents=False)
    for name, body in files.items():
        fd = os.open(out_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(body if type(body) is str else json.dumps(body, indent=2, sort_keys=True) + '\n')


PLACEHOLDER_CONFIG, PLACEHOLDER_CREDENTIAL = 'enterprise-provider-config', 'enterprise-provider-credential'


def placeholders():
    """Without an enterprise endpoint the stage Compose still mounts the three enterprise secrets of the action-worker; these
    files only satisfy the mounts. They are not in the routes file, so the worker never reads them: an empty JSON object as the
    config and a fresh random URL-safe credential (32 bytes) that no endpoint knows. The CA file is not written here: the caller
    supplies it (or copies the WebChat CA); its content is never read."""
    return {PLACEHOLDER_CONFIG: {}, PLACEHOLDER_CREDENTIAL: secrets.token_urlsafe(32) + '\n'}


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, json.dumps({'ok': False, 'error': 'arguments'}) + '\n')


def main(argv=None):
    parser = _Parser(description='Generate the action-worker provider routes and configs (G-ROUTES)')
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--webchat-delivery-port', type=int, required=True)
    parser.add_argument('--enterprise-origin')
    parser.add_argument('--enterprise-connect-address')
    parser.add_argument('--timeout', type=float, default=2.0)
    parser.add_argument('--enterprise-placeholders', action='store_true',
                        help='no enterprise endpoint: also write placeholder enterprise config and credential for the Compose mounts')
    args = parser.parse_args(argv)
    try:
        files = build(webchat_delivery_port=args.webchat_delivery_port, enterprise_origin=args.enterprise_origin,
                      enterprise_connect_address=args.enterprise_connect_address, timeout=args.timeout)
        if args.enterprise_placeholders:
            if args.enterprise_origin:
                raise Refused('placeholders are only for a deployment without an enterprise endpoint')
            files.update(placeholders())
        write(args.out_dir, files)
    except Refused as refused:
        print(json.dumps({'ok': False, 'refused': str(refused)}))
        return 2
    routes = files[FILES['action_worker_provider_routes']]['routes']
    result = {'ok': True, 'files': sorted(files), 'routes': sorted(routes)}
    if args.enterprise_placeholders:
        result['placeholders'] = [PLACEHOLDER_CONFIG, PLACEHOLDER_CREDENTIAL]  # not routed; the CA mount source comes from the caller
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
