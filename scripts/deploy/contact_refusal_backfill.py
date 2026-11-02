"""Contact refusal history backfill (plan claim-to-consumer-property §11.1): dry run, then --apply, as the reply guarantor service
(nexloop_domain_worker) through the signed port; counts only. See nexloop_eios.contact_refusal_backfill.main for the arguments."""
from nexloop_eios.contact_refusal_backfill import main

if __name__ == '__main__':
    raise SystemExit(main())
