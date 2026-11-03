"""S-DUP-2 (docs/implementation/S-DUP-extraction-dedupe-design.md §11): supersede the still-active duplicates of Claims that a correction
superseded before migration claim_duplicate_supersede, through the signed port authz.nexloop_claim_duplicate_supersede_backfill
(eios:action:nexloop.claim.supersede_duplicates:1) as the claim matcher service on its restricted nexloop_domain_worker connection.
SQL uses the 0412 statement key and the same supersede write as the trigger; it skips Consumers with an erasure request. Counts only.

    claim_duplicate_backfill.py --database-url-file F --signing-key-file F --service-credential-file F --artifact-root D [--apply]
"""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from nexloop_eios.plan_reevaluation import _SignedPort

ACTION='eios:action:nexloop.claim.supersede_duplicates:1'


class ClaimDuplicateSupersedePort(_SignedPort):
    PROTOCOL='nexloop-claim-duplicate-supersede-v1';ACTION=ACTION;FUNCTION='nexloop_claim_duplicate_supersede_backfill'

    def run(self,*,apply=False):return self._signed({'verb':'apply' if apply else 'dry_run'})


def main(argv=None):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text

    class _Parser(argparse.ArgumentParser):
        def error(self,message):self.exit(2,'claim duplicate backfill configuration unavailable\n')

    p=_Parser(description='Supersede duplicates of corrected Claims (signed port; counts only)')
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--signing-key-id',default='active')
    p.add_argument('--apply',action='store_true',help='supersede; without it a dry run that writes nothing')
    a=p.parse_args(argv)
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=read_private_text(a.database_url_file,maximum=16384),artifact_root=a.artifact_root,
            signing_key_file=a.signing_key_file,signing_key_id=a.signing_key_id,pool_max_size=1))
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!='nexloop_domain_worker':raise SystemExit('restricted service role required')
        current=backend.authenticate(read_private_text(a.service_credential_file,maximum=16384),world='real')
        result=ClaimDuplicateSupersedePort(current._backend._pool,current._session,current._backend._signer).run(apply=a.apply)
        print(json.dumps(result,sort_keys=True,separators=(',',':')),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
