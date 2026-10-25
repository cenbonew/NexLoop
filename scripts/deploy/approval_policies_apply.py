#!/usr/bin/env python3
"""Publish an approval policies manifest (NX-056 trusted configuration) through the configurator login window, one command
(deploy/configuration/approval-policies.v<N>.json). The same path as alert_rules_apply.py: configurator_window.configurator_window
(nexloop_bootstrap over the local socket opens the configurator for this call only) -> nexloop_eios.approval_policies.apply_policies
-> control.nexloop_configure_approval_policies, given the file's exact text; the database records its sha256 and expected revision.

    python3 scripts/deploy/approval_policies_apply.py --bootstrap-dsn-file F --tenant <TENANT_ID> --manifest deploy/configuration/approval-policies.v1.json [--check]

--check validates the manifest only (no connection). --tenant names the tenant the operator publishes for: the policies themselves are
the deployment's (one set for every tenant), so it is only checked to exist before anything is written. Output: one JSON line (the
version, policy count and sha256, or what the database answered); never the DSN. Exit 0 applied or valid, 2 refused.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from configurator_window import configurator_window  # noqa: E402
from nexloop_eios.approval_policies import ApprovalPoliciesRejected, apply_policies, validate_policies  # noqa: E402


def tenant_exists(dsn_file, tenant):
    from nexloop_eios.trusted_configuration import configurator_connection
    with configurator_connection(dsn_file) as db:
        return db.execute('select control.nexloop_configurator_tenant_exists(%s)', (tenant,)).fetchone()[0]


def main(argv=None, out=sys.stdout, window=configurator_window, apply=apply_policies, exists=tenant_exists):
    p = argparse.ArgumentParser(description='Publish an approval policies manifest through the configurator window')
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--tenant')
    p.add_argument('--bootstrap-dsn-file')
    p.add_argument('--check', action='store_true')
    a = p.parse_args(argv)
    try:
        text = a.manifest.read_text(encoding='utf-8')
        manifest = validate_policies(json.loads(text))
        summary = {'version': manifest['version'], 'expected_revision': manifest['expected_revision'], 'policies': len(manifest['policies']),
                   'manifest_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        if a.check:
            print(json.dumps({'valid': True, **summary}), file=out)
            return 0
        if not a.tenant or not a.bootstrap_dsn_file:
            raise ApprovalPoliciesRejected('--tenant and --bootstrap-dsn-file required')
        with window(a.bootstrap_dsn_file, refused=ApprovalPoliciesRejected) as dsn:
            if not exists(dsn, a.tenant):
                raise ApprovalPoliciesRejected('tenant unknown')
            result = apply(text, database_url_file=dsn)
    except (ApprovalPoliciesRejected, ValueError) as error:
        print(json.dumps({'applied': False, 'reason': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a DSN: the type only
        print(json.dumps({'applied': False, 'reason': type(error).__name__}), file=out)
        return 2
    print(json.dumps({'applied': True, **summary, 'result': result}, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
