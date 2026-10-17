#!/usr/bin/env python3
"""NX-037 evidence item effect_reconciliation (docs/release/at-015-evidence-pack.md: trial/reconciliation.json). Read only.

The controlled interaction records carry each intent's reconciliation as it stood when the interaction ended. This item is the
state after the trial, read from PostgreSQL in one READ ONLY transaction: for every intent of the trial, its current state, the
outbox state, the outbound reply's delivery state, its provider observations by state, the number of attempts and an owner
determination (0210) when there is one. Ids, codes, counts and times only, checked against the evidence pack's vocabulary.

    python scripts/nx037/effect_reconciliation.py --database-url-file F --trial DIR --out FILE

--trial is the run_interactions directory (its records name the intent ids); --database-url-file a private (0600) DSN of a role
that may read the effect tables (on stage: the bootstrap connection in a read-only transaction, as the other read-only checks).
Exit: 0 written (unresolved intents are reported, not an error), 2 input refused. Nothing is written to the database.
"""
import argparse
import json
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / 'packages/eios-core/src'))
import evidence_pack as E  # noqa: E402

SETTLED = ('fulfilled', 'confirmed', 'observed_fulfilled', 'failed')
QUERY = """
select i.intent_id::text,i.state,o.state,m.delivery_state,
  (select count(*) from runtime.nexloop_effect_attempts a where a.intent_id=i.intent_id),
  (select coalesce(jsonb_object_agg(x.provider_state,x.n),'{}'::jsonb) from (select provider_state,count(*) n from runtime.nexloop_effect_observations
     where intent_id=i.intent_id group by provider_state) x),
  (select d.kind from runtime.nexloop_effect_determinations d where d.intent_id=i.intent_id),
  to_char(i.created_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')
 from runtime.nexloop_effect_intents i left join runtime.nexloop_effect_outbox o using(intent_id)
 left join runtime.nexloop_outbound_messages m on m.intent_id=i.intent_id and m.tenant_id=i.tenant_id and m.world=i.world
 where i.intent_id=any(%s::uuid[])
"""


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError('private file must be a regular 0600 file of the caller')
        return stream.read().strip()


def trial_intents(directory):
    from nexloop_eios.controlled_interactions import read_records
    seen = {}
    for record in read_records(Path(directory)):
        for intent in record['intent_ids']:
            seen.setdefault(intent, {'seq': record['seq'], 'kind': record['kind'], 'at_end': record['reconciliation']})
    return seen


def reconcile(rows, intents):
    """rows from QUERY → the item (intents keyed by id, totals); an intent of the trial not found is reported as missing."""
    found = {r[0]: r for r in rows}
    items, totals = {}, {'intents': len(intents), 'settled': 0, 'unresolved': 0, 'missing': 0, 'determined': 0}
    for intent, origin in sorted(intents.items()):
        row = found.get(intent)
        if row is None:
            items[intent] = {**origin, 'status': 'missing'};totals['missing'] += 1;continue
        _, state, outbox, delivery, attempts, observations, determination, created = row
        settled = state in SETTLED
        totals['settled' if settled else 'unresolved'] += 1
        totals['determined'] += determination is not None
        items[intent] = {**origin, 'status': 'settled' if settled else 'unresolved', 'state': state, 'outbox_state': outbox, 'delivery_state': delivery,
                         'attempts': attempts, 'observations': {k: observations[k] for k in sorted(observations)}, 'determination': determination,
                         'created_at': created}
    return {'schema': 'nexloop.nx037-effect-reconciliation.v1', 'totals': totals, 'intents': items}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--database-url-file', required=True)
    parser.add_argument('--trial', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        dsn = read_private(args.database_url_file)
        intents = trial_intents(args.trial)
        if os.path.exists(args.out):
            raise ValueError('output exists')
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'detail': type(error).__name__}));return 2
    import psycopg
    with psycopg.connect(dsn) as db:
        db.execute('set transaction read only')
        rows = db.execute(QUERY, (sorted(intents),)).fetchall() if intents else []
        db.rollback()
    item = reconcile(rows, intents)
    E._write(Path(args.out), item)
    print(json.dumps({'ok': True, **item['totals']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
