#!/usr/bin/env python3
"""NX-054 relation types on stage: publish the first batch, or stop the derivation, through the configurator login window (the
alert_rules_apply.py pattern: nexloop_bootstrap over the local socket opens the configurator for this call only;
control.nexloop_publish_relation_types accepts the configurator alone).

    relation_types_publish.py --dry-run                                         # the manifest summary; no connection
    relation_types_publish.py --tenant T --bootstrap-dsn-file F                 # publish catalog version 1 (the first batch)
    relation_types_publish.py --tenant T --bootstrap-dsn-file F --stop-derivation --from-version N [--dry-run]

--stop-derivation (the rollback, NX-054-P2.md §1, I-1): a new catalog version N+1 in which every first-batch type gets a new
version without source_property. The derivation map follows the latest version, so from its commit no new relation is derived
and, on an object's next change of properties, its derived relations of those types are dropped; relations already derived stay
until then (no reader uses them before P4). Published versions are immutable: derivation comes back only with a later version
(N+2) that names the properties again (--resume-derivation --from-version N+1). N must be the latest published catalog version
(control.nexloop_relation_catalog_head, read in the window before publishing; anything older is refused - L6 review R-1).
--defer NAME (repeatable; stage 2026-10-27): leave a first-batch relation type out of this catalog version, for a tenant on which
one of its endpoint types is not registered (ontology.object_type_versions; 0319 refuses the whole publication otherwise - on
stage RelationshipAssessment, so assessment_evidence). The name must be a first-batch type and what is left must still form
complete ownership chains (a deferred message_in would leave assessment_evidence without one: refused). The deferred names are
printed; the manifest (and so its digest) leaves them out. A later catalog version publishes them once the type is registered. Every mode
takes it (a rollback of a deferred catalog defers the same names).
Output: one JSON line (counts, versions, the manifest digest; never a DSN). Exit 0 done, 2 refused.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from configurator_window import configurator_window  # noqa: E402
from nexloop_eios import relation_catalog as R  # noqa: E402


class Refused(ValueError):
    pass


def specs(deferred=()):
    """The first batch without the deferred names; ownership chains rechecked by R.manifest."""
    names = {s.name for s in R.FIRST_BATCH}
    unknown = sorted(set(deferred) - names)
    if unknown:
        raise Refused(f'--defer: not a first-batch relation type: {unknown[0]}')
    return tuple(s for s in R.FIRST_BATCH if s.name not in set(deferred))


def built(kept, version):
    try:
        return R.manifest(kept, version=version)
    except R.RelationCatalogInvalid as error:
        raise Refused(f'--defer leaves an incomplete catalog: {error}') from None


def versioned(from_version, *, derive, deferred=()):
    """The first batch as catalog version from_version+1, every type at that version; source_property kept or cleared."""
    if type(from_version) is not int or from_version < 1:
        raise Refused('--from-version: the published catalog version (1 or more)')
    value = built(specs(deferred), from_version + 1)
    for r in value['relation_types']:
        r['definition']['version'] = from_version + 1
        if not derive:
            r['source_property'] = None
    return value


def check_head(head, version):
    """L6 review R-1: a rollback or resume is built on the latest published catalog version, never on an older one (an older
    --from-version would rebuild a version already published and come back as a replay - a rollback that did nothing). Accepted:
    the next version of the head, or the head itself again (the same command rerun; the database refuses other content, 40001)."""
    if version == 1:
        if head not in (None, 1):
            raise Refused(f'the catalog is at version {head}: the first batch is published already - stop or resume from {head}')
    elif head is None or version not in (head, head + 1):
        raise Refused(f'--from-version must be the latest published catalog version ({head}), not {version - 1}')


def summary(value, deferred=()):
    return {'deferred': sorted(set(deferred)), 'catalog_version': value['version'], 'relation_types': len(value['relation_types']),
            'deriving': sum(1 for r in value['relation_types'] if r['source_property']),
            'longest_ownership_chain': value['longest_ownership_chain'], 'manifest_digest': R.manifest_digest(value)}


def main(argv=None, out=sys.stdout, window=configurator_window, connect=None):
    p = argparse.ArgumentParser(description='NX-054: publish relation types / stop or resume derivation (configurator window)')
    p.add_argument('--tenant')
    p.add_argument('--bootstrap-dsn-file')
    p.add_argument('--dry-run', action='store_true')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--stop-derivation', action='store_true')
    mode.add_argument('--resume-derivation', action='store_true')
    p.add_argument('--from-version', type=int)
    p.add_argument('--defer', action='append', default=[], metavar='NAME')
    a = p.parse_args(argv)
    try:
        if a.stop_derivation or a.resume_derivation:
            value = versioned(a.from_version, derive=a.resume_derivation, deferred=a.defer)
        else:
            value = copy.deepcopy(built(specs(a.defer), R.CATALOG_VERSION))
        if a.dry_run:
            print(json.dumps({'dry_run': True, **summary(value, a.defer)}, sort_keys=True), file=out)
            return 0
        if not a.tenant or not a.bootstrap_dsn_file:
            raise Refused('--tenant and --bootstrap-dsn-file required')
        if connect is None:
            from nexloop_eios.trusted_configuration import configurator_connection as connect
        with window(a.bootstrap_dsn_file, refused=Refused) as dsn:
            db = connect(dsn)
            with db, db.transaction():
                db.execute("set local statement_timeout='30000ms'; set local lock_timeout='3000ms'")
                head = db.execute('select control.nexloop_relation_catalog_head(%s)', (a.tenant,)).fetchone()[0]
                check_head(head, value['version'])
                result = R.publish(db, a.tenant, value)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a DSN: the type and the database's own message
        message = getattr(getattr(error, 'diag', None), 'message_primary', None)
        print(json.dumps({'refused': type(error).__name__, 'message': message}), file=out)
        return 2
    print(json.dumps({**summary(value, a.defer), 'result': result}, sort_keys=True, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
