"""NX-054 P3 relation backfill (docs/design/relation-backfill-p3.md): preflight, start, run, status, verify, abort, revert through
the signed port as the backfill's own service principal (nexloop_domain_worker). Private configuration files only; see
nexloop_eios.relation_backfill.main for the arguments."""
from nexloop_eios.relation_backfill import main

if __name__ == '__main__':
    raise SystemExit(main())
