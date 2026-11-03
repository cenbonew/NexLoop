"""Contact refusal history backfill (plan docs/implementation/claim-to-consumer-property-plan.md §9.6, §11.1).

Re-checks the tenant's earlier inbound consumer messages with the current refusal rules through the signed port
``authz.nexloop_contact_refusal_backfill`` (eios:action:nexloop.contact.refusal_backfill:1) as the reply guarantor service, on its
restricted nexloop_domain_worker connection; never an owner or migration credential. SQL restricts through the inbound trigger's own
control writer and skips erasures, simulated or synthetic personas, owner releases, already checked messages and marketing-only hits.
Both modes print counts only.

    contact_refusal_backfill.py --database-url-file F --signing-key-file F --service-credential-file F --artifact-root D \\
        --rule-version 3 [--apply]
"""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from nexloop_eios.contact_restrictions import ContactRefusalBackfillPort


def main(argv=None):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text

    class _Parser(argparse.ArgumentParser):
        def error(self,message):self.exit(2,'contact refusal backfill configuration unavailable\n')

    p=_Parser(description='Contact refusal history backfill (signed port; counts only)')
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--signing-key-id',default='active')
    p.add_argument('--rule-version',type=int,required=True)
    p.add_argument('--apply',action='store_true',help='restrict; without it a dry run that writes nothing')
    a=p.parse_args(argv)
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=read_private_text(a.database_url_file,maximum=16384),artifact_root=a.artifact_root,
            signing_key_file=a.signing_key_file,signing_key_id=a.signing_key_id,pool_max_size=1))
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!='nexloop_domain_worker':raise SystemExit('restricted service role required')
        current=backend.authenticate(read_private_text(a.service_credential_file,maximum=16384),world='real')
        result=ContactRefusalBackfillPort(current._backend._pool,current._session,current._backend._signer).run(rule_version=a.rule_version,apply=a.apply)
        print(json.dumps(result,sort_keys=True,separators=(',',':')),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
