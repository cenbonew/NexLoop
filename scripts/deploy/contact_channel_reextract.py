"""Contact channel re-extraction (plan claim-to-consumer-property §8.4): dry run, then --apply, as the claim extraction scheduler service
(nexloop_api) through the signed port; the claim extraction worker then extracts the windows. Counts only. See
nexloop_eios.claim_extraction_jobs.reextract_main for the arguments."""
from nexloop_eios.claim_extraction_jobs import reextract_main

if __name__ == '__main__':
    raise SystemExit(reextract_main())
