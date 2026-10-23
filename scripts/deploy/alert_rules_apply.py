#!/usr/bin/env python3
"""Publish an alert rules manifest (trusted configuration) through the configurator login window, one command (host health rollout:
deploy/configuration/alert-rules.v12.json). The same path real_tenant_init.py configure uses for its rules step, without its
effect-category step: configurator_window.configurator_window (nexloop_bootstrap over the local socket opens the configurator for
this call only) -> nexloop_eios.observability.apply_rules -> control.nexloop_configure_alert_rules.

    python3 scripts/deploy/alert_rules_apply.py --bootstrap-dsn-file F --tenant <TENANT_ID> --manifest deploy/configuration/alert-rules.v12.json [--check]

--check validates the manifest only (no connection). Output: one JSON line (the version and rule count, or what the database
answered); never the DSN. Exit 0 applied or valid, 2 refused.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from configurator_window import configurator_window  # noqa: E402
from nexloop_eios.observability import AlertRulesRejected, apply_rules, load_rules  # noqa: E402


def main(argv=None, out=sys.stdout, window=configurator_window, apply=apply_rules):
    p = argparse.ArgumentParser(description='Publish an alert rules manifest through the configurator window')
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--tenant')
    p.add_argument('--bootstrap-dsn-file')
    p.add_argument('--check', action='store_true')
    a = p.parse_args(argv)
    try:
        manifest = load_rules(a.manifest)
        if a.check:
            print(json.dumps({'valid': True, 'version': manifest['version'], 'rules': len(manifest['rules'])}), file=out)
            return 0
        if not a.tenant or not a.bootstrap_dsn_file:
            raise AlertRulesRejected('--tenant and --bootstrap-dsn-file required')
        with window(a.bootstrap_dsn_file, refused=AlertRulesRejected) as dsn:
            result = apply(manifest, a.tenant, database_url_file=dsn)
    except AlertRulesRejected as error:
        print(json.dumps({'applied': False, 'reason': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a DSN: the type only
        print(json.dumps({'applied': False, 'reason': type(error).__name__}), file=out)
        return 2
    print(json.dumps({'applied': True, 'version': manifest['version'], 'rules': len(manifest['rules']), 'result': result}, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
