"""S-DUP-2: supersede the active duplicates of Claims corrected before the fix; dry run, then --apply, as the claim matcher service
(nexloop_domain_worker) through the signed port; counts only. See nexloop_eios.claim_duplicates.main for the arguments."""
from nexloop_eios.claim_duplicates import main

if __name__ == '__main__':
    raise SystemExit(main())
