#!/usr/bin/env python3
"""Publish the Claim auto-write whitelist manifest (trusted configuration) through the configurator login window, one command
(plan docs/implementation/claim-to-consumer-property-plan.md §9.1; deploy/configuration/auto-write.v<N>.json). The same path as
alert_rules_apply.py: configurator_window.configurator_window -> nexloop_eios.auto_write.apply_manifest ->
control.nexloop_configure_auto_write. The owner edits the file; nobody types into a console.

    python3 scripts/deploy/auto_write_apply.py --bootstrap-dsn-file F --tenant <TENANT_ID> --manifest deploy/configuration/auto-write.v1.json [--check]

--check validates the manifest's shape only (no connection; a sensitive or unpublished property is refused by the database on
publish) and prints each entry with its reason and the decision reference. Publishing first prints, as one JSON line, what each listed
property is in the ontology now (type version, value type, display name, description), then one JSON line with the result (version,
world, property count, decision reference, or what the database answered); never the DSN. Exit 0 applied or valid, 2 refused.
Adding a property needs the owner's decision, cited in approved_by_decision (docs/release/run-claim-consumer-phase1.md).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from configurator_window import configurator_window  # noqa: E402
from nexloop_eios.auto_write import AutoWriteRejected, apply_manifest, definitions, load_manifest  # noqa: E402


def main(argv=None, out=sys.stdout, window=configurator_window, apply=apply_manifest, describe=definitions):
    p = argparse.ArgumentParser(description='Publish the Claim auto-write whitelist through the configurator window')
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--tenant')
    p.add_argument('--bootstrap-dsn-file')
    p.add_argument('--check', action='store_true')
    a = p.parse_args(argv)
    try:
        manifest = load_manifest(a.manifest)
        summary = {'version': manifest['version'], 'world': manifest['world'], 'properties': len(manifest['properties']),
                   'approved_by_decision': manifest['approved_by_decision']}
        if a.check:
            print(json.dumps({'valid': True, **summary, 'entries': manifest['properties']}, ensure_ascii=False), file=out)
            return 0
        if not a.tenant or not a.bootstrap_dsn_file:
            raise AutoWriteRejected('--tenant and --bootstrap-dsn-file required')
        with window(a.bootstrap_dsn_file, refused=AutoWriteRejected) as dsn:
            print(json.dumps({'definitions': describe(manifest, a.tenant, database_url_file=dsn)}, ensure_ascii=False), file=out, flush=True)
            result = apply(manifest, a.tenant, database_url_file=dsn)
    except AutoWriteRejected as error:
        print(json.dumps({'applied': False, 'reason': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a DSN: the type and, for a database refusal, its message
        import psycopg
        reason = (error.diag.message_primary or type(error).__name__) if isinstance(error, psycopg.Error) else type(error).__name__
        print(json.dumps({'applied': False, 'reason': reason}), file=out)
        return 2
    print(json.dumps({'applied': True, **summary, 'result': result}, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
