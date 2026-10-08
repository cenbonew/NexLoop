"""Bounded Artifact maintenance through live EIOS authority, with explicit recovery.

No bootstrap, raw business SQL, environment credentials, or automatic real-world
selection. A failed sweep is resumed by its durable ID before scheduling new work.
"""
import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from nexloop_eios.backend import open_backend
from nexloop_eios.private_configuration import read_private_text


def run_page(service, *, older_than, limit=25, after='', resume=None):
    """One durable page; identifiers are returned to the trusted scheduler only."""
    if resume is not None:
        return service.resume_final_artifact_orphan_sweep(resume)
    return service.collect_final_artifact_orphans(older_than=older_than, limit=limit, after=after)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url-file',type=Path,required=True)
    parser.add_argument('--signing-key-file',type=Path,required=True)
    parser.add_argument('--service-credential-file',type=Path,required=True)
    parser.add_argument('--artifact-root',type=Path,required=True)
    parser.add_argument('--signing-key-id',required=True)
    parser.add_argument('--world',choices=['test','real'],required=True)
    parser.add_argument('--minimum-age-seconds',type=int,default=3600)
    parser.add_argument('--limit',type=int,default=25)
    parser.add_argument('--after',default='')
    parser.add_argument('--resume')
    args=parser.parse_args(argv)
    if not 60<=args.minimum_age_seconds<=31536000 or not 1<=args.limit<=50:
        parser.error('invalid bounded maintenance policy')
    try:
        with open_backend(database_url=read_private_text(args.database_url_file,maximum=16384),
                artifact_root=args.artifact_root,signing_key_file=args.signing_key_file,
                signing_key_id=args.signing_key_id) as backend:
            service=backend.authenticate(read_private_text(args.service_credential_file,maximum=2048),world=args.world)
            report=run_page(service,older_than=datetime.now(UTC)-timedelta(seconds=args.minimum_age_seconds),
                            limit=args.limit,after=args.after,resume=args.resume)
        # No DSN, credential, paths or per-file metadata in process output.
        print(json.dumps({'passed':True,'sweep_id':report['sweep_id'],
            'processed':len(report['outcomes']),'next_after':report['next_after'],
            'exhausted':report['exhausted'],'product_ready':False}))
        return 0
    except Exception:
        print(json.dumps({'passed':False,'code':'maintenance_unavailable','product_ready':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
