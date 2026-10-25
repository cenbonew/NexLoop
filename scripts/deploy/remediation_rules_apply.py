#!/usr/bin/env python3
"""Publish a remediation rules manifest (NX-057 trusted configuration) through the configurator login window, one command
(deploy/configuration/remediation-rules.v<N>.json; run sheet docs/release/run-nx057-remediation-stage.md §3, gap G1). The same path as
alert_rules_apply.py: configurator_window.configurator_window (nexloop_bootstrap over the local socket opens the configurator for this
call only) -> nexloop_eios.remediation.publish_rules -> control.nexloop_publish_remediation_rules, given the file's exact text; the
database records its sha256 and the expected revision.

    python3 scripts/deploy/remediation_rules_apply.py --bootstrap-dsn-file F --tenant <TENANT_ID> --expected-revision 0 \
        --manifest deploy/configuration/remediation-rules.v1.json [--check]

--expected-revision is the tenant's current rules version (0 before the first publication); another current version is refused
(40001) and nothing is written. --check validates the manifest only (no connection). Output: one JSON line (the version, rule count
and sha256, or what the database answered); never the DSN. Exit 0 applied or valid, 2 refused.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from configurator_window import configurator_window  # noqa: E402
from nexloop_eios.remediation import RemediationRulesRejected, publish_rules, validate_rules  # noqa: E402


def main(argv=None, out=sys.stdout, window=configurator_window, publish=publish_rules):
    p = argparse.ArgumentParser(description='Publish a remediation rules manifest through the configurator window')
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--tenant')
    p.add_argument('--world', default='real')
    p.add_argument('--expected-revision', type=int)
    p.add_argument('--bootstrap-dsn-file')
    p.add_argument('--check', action='store_true')
    a = p.parse_args(argv)
    try:
        text = a.manifest.read_text(encoding='utf-8')
        manifest = validate_rules(json.loads(text))
        summary = {'manifest_version': manifest['manifest_version'], 'rules': len(manifest['rules']),
                   'manifest_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        if a.check:
            print(json.dumps({'valid': True, **summary}), file=out)
            return 0
        if not a.tenant or not a.bootstrap_dsn_file or a.expected_revision is None or a.expected_revision < 0:
            raise RemediationRulesRejected('--tenant, --expected-revision and --bootstrap-dsn-file required')
        with window(a.bootstrap_dsn_file, refused=RemediationRulesRejected) as dsn:
            result = publish(text, a.tenant, world=a.world, expected_revision=a.expected_revision, database_url_file=dsn)
    except (RemediationRulesRejected, ValueError) as error:
        print(json.dumps({'applied': False, 'reason': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a DSN: the type, and SQL's own refusal text for a database refusal
        reason = type(error).__name__
        diag = getattr(error, 'diag', None)
        if getattr(error, 'sqlstate', None) in ('22023', '40001', '42501') and diag is not None and diag.message_primary:
            reason += ': ' + diag.message_primary
        print(json.dumps({'applied': False, 'reason': reason}), file=out)
        return 2
    print(json.dumps({'applied': True, **summary, 'expected_revision': a.expected_revision, 'result': result}, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
