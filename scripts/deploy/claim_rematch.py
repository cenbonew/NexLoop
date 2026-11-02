#!/usr/bin/env python3
"""Release Claims for rematching by the reason of their latest match (plan docs/implementation/claim-to-consumer-property-plan.md §9.3;
migration claim_rematch; docs/release/run-claim-consumer-phase1.md). Today: type_not_writable, after edit_actions was configured.

Runs as the claim matcher's own service (its DSN, service credential and signing key, as the claim-matcher container mounts them):
tenant and world come from that credential, and SQL refuses any other caller. Dry run by default; --release writes.

    claim_rematch.py --database-url-file F --service-credential-file F --signing-key-file F --signing-key-id ID --artifact-root DIR \\
                     --reason type_not_writable [--matcher-version nx020-matcher/1] [--world real] [--release]

SQL releases only Claims in needs_resolution whose latest match has that reason, never one of an erasing or erased Consumer, a
Consumer whose persona is not real, a superseded or a corrected Claim. Output: one JSON line of counts {verb, eligible, released,
conversations} - no Claim id, text, DSN or credential. Exit 0 done, 2 refused.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))


def main(argv=None, out=sys.stdout):
    from nexloop_eios.claim_matching import MATCHER_VERSION, REMATCH_REASONS
    p = argparse.ArgumentParser(description='Release Claims for rematching by the reason of their latest match (counts only)')
    for name in ('--database-url-file', '--service-credential-file', '--signing-key-file', '--signing-key-id', '--artifact-root'):
        p.add_argument(name, required=True)
    p.add_argument('--reason', required=True, choices=REMATCH_REASONS)
    p.add_argument('--matcher-version', default=MATCHER_VERSION)
    p.add_argument('--world', default='real')
    p.add_argument('--release', action='store_true', help='write (default: dry run)')
    a = p.parse_args(argv)
    verb = 'release' if a.release else 'dry_run'
    try:
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.backend import open_backend
        from nexloop_eios.claim_matching import rematch
        from nexloop_eios.private_configuration import read_private_text
        with open_backend(database_url=read_private_text(a.database_url_file, maximum=16384), artifact_root=a.artifact_root,
                          signing_key_file=a.signing_key_file, signing_key_id=a.signing_key_id, pool_max_size=2) as backend:
            with backend._pool.connection() as connection:
                if verify_application_role(connection) != 'nexloop_domain_worker':
                    raise PermissionError('the claim matcher role required')
            current = backend.authenticate(read_private_text(a.service_credential_file, maximum=16384), world=a.world)
            result = rematch(current._backend._pool, current._session, current._backend._signer, verb=verb, reason=a.reason,
                             matcher_version=a.matcher_version)
    except Exception as error:  # noqa: BLE001 - never a DSN or credential: the type only
        print(json.dumps({'verb': verb, 'done': False, 'reason': type(error).__name__}), file=out)
        return 2
    print(json.dumps({'verb': verb, 'done': True, **{k: result[k] for k in ('eligible', 'released', 'conversations')}}, sort_keys=True), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
