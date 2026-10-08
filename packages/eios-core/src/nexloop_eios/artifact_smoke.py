"""Explicit test-world PG/local-Artifact smoke; no bootstrap or model calls."""
import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import secrets

from nexloop_eios.backend import open_backend
from nexloop_eios.private_configuration import read_private_text

SMOKE_BYTES = b'nexloop artifact smoke v1\n'


def run_smoke(*, database_url_file, service_credential_file, signing_key_file,
              signing_key_id, artifact_root):
    result = {'mode':'test', 'world':'test', 'scope':'postgres_local_artifact_write_read',
              'passed':False, 'product_ready':False, 'model_validation':'not_run',
              'checks':{'backend_startup':False,'service_authentication':False,
                        'artifact_write':False,'artifact_read':False}}
    try:
        dsn = read_private_text(database_url_file, maximum=16384)
        token = read_private_text(service_credential_file, maximum=512)
        with open_backend(database_url=dsn, artifact_root=artifact_root,
                          signing_key_file=signing_key_file, signing_key_id=signing_key_id) as backend:
            result['checks']['backend_startup'] = True
            service = backend.authenticate(token, world='test')
            token = ''
            result['checks']['service_authentication'] = True
            ref = service.put_artifact(request_id='nexloop-artifact-smoke-'+secrets.token_hex(16),
                payload=SMOKE_BYTES, media_type='text/plain',
                retention_until=datetime.now(UTC)+timedelta(minutes=5))
            result['checks']['artifact_write'] = True
            data = service.read_artifact(ref.artifact_id)
            result['checks']['artifact_read'] = data == SMOKE_BYTES
            result['artifact'] = {'artifact_id':ref.artifact_id,'sha256':ref.sha256,
                                  'size_bytes':ref.size_bytes,'world':ref.world}
            result['passed'] = (all(result['checks'].values())
                and ref.sha256==hashlib.sha256(SMOKE_BYTES).hexdigest() and ref.world=='test')
    except Exception:
        # Drivers/decoders can carry input in their exceptions. Never print it.
        result['error'] = 'artifact smoke failed; check private configuration and live test-world authority'
    return result


def main():
    parser = argparse.ArgumentParser(description='NexLoop explicit test-world Artifact smoke; performs a small authorized write')
    parser.add_argument('--mode', required=True, choices=['test'])
    parser.add_argument('--database-url-file', required=True, type=Path)
    parser.add_argument('--service-credential-file', required=True, type=Path)
    parser.add_argument('--signing-key-file', required=True, type=Path)
    parser.add_argument('--signing-key-id', required=True)
    parser.add_argument('--artifact-root', required=True, type=Path)
    args = parser.parse_args()
    result = run_smoke(database_url_file=args.database_url_file,
        service_credential_file=args.service_credential_file, signing_key_file=args.signing_key_file,
        signing_key_id=args.signing_key_id, artifact_root=args.artifact_root)
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
