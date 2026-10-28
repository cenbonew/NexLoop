#!/usr/bin/env python3
"""Release 53 P9 (L6 hard constraint of outreach slice 5 G-1): the outreach sender as Compose will run it uses the loopback provider
only and mounts no channel credential. Reads `docker compose ... config --format json` on stdin (APP_HOST, read-only):

    docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file ... config --format json | python3 outreach_sender_p9.py

Checks the `outreach-sender` service: entrypoint nexloop-outreach-sender; no --provider and no --real-channel-enabled in its command
(the provider is then the loopback outbox); its secrets exactly its own four files plus signing_key and pg_ca, and no secret of
any other service among them; its volumes only the Artifact directory and the outreach outbox; read-only root, cap_drop ALL,
no-new-privileges, network_mode service:app-net; no environment variable but REAL_DISPATCH_ENABLED. One JSON line:
{"ok": bool, "problems": [...]} (names and flags only, no value). Exit 0 ok, 1 a problem, 2 the input is not a Compose config.
Standard library only.
"""
import json
import sys

SERVICE = 'outreach-sender'
SECRETS = {'outreach_sender_dsn', 'outreach_sender_pg_client_cert', 'outreach_sender_pg_client_key', 'outreach_sender_credential',
           'signing_key', 'pg_ca'}
TARGETS = {'/var/lib/nexloop/artifacts', '/var/lib/nexloop/outreach-outbox'}


def problems(config):
    service = (config.get('services') or {}).get(SERVICE)
    if not isinstance(service, dict):
        return [f'{SERVICE} missing']
    found = []
    if service.get('entrypoint') != ['nexloop-outreach-sender']:
        found.append('entrypoint')
    command = [str(a) for a in service.get('command') or []]
    for flag in ('--provider', '--real-channel-enabled'):
        if any(a == flag or a.startswith(flag + '=') for a in command):
            found.append('command has ' + flag)
    if '--outbox-directory' not in command:
        found.append('command has no --outbox-directory')
    secrets = {s.get('source') if isinstance(s, dict) else s for s in service.get('secrets') or []}
    if secrets != SECRETS:
        found += [f'secret {s} not expected' for s in sorted(secrets - SECRETS, key=str)] + [f'secret {s} missing' for s in sorted(SECRETS - secrets)]
    for volume in service.get('volumes') or []:
        target = volume.get('target') if isinstance(volume, dict) else str(volume).split(':')[1] if ':' in str(volume) else str(volume)
        if target not in TARGETS:
            found.append(f'volume {target}')
        if isinstance(volume, dict) and '/secrets' in str(volume.get('source', '')):
            found.append('a secrets directory is mounted')
    if service.get('read_only') is not True:
        found.append('read_only')
    if service.get('cap_drop') != ['ALL']:
        found.append('cap_drop')
    if 'no-new-privileges:true' not in (service.get('security_opt') or []):
        found.append('no-new-privileges')
    if service.get('network_mode') != 'service:app-net':
        found.append('network_mode')
    if set(service.get('environment') or {}) - {'REAL_DISPATCH_ENABLED'}:
        found.append('environment: ' + ', '.join(sorted(set(service['environment']) - {'REAL_DISPATCH_ENABLED'})))
    return found


def main(stdin=sys.stdin, out=sys.stdout):
    try:
        config = json.load(stdin)
        if not isinstance(config, dict) or not isinstance(config.get('services'), dict):
            raise ValueError()
    except ValueError:
        print(json.dumps({'ok': False, 'problems': ['input is not a compose config']}), file=out)
        return 2
    found = problems(config)
    print(json.dumps({'ok': not found, 'problems': found}, sort_keys=True), file=out)
    return 0 if not found else 1


if __name__ == '__main__':
    sys.exit(main())
