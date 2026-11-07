"""S6 stage preflight: one read-only report of the prerequisites P1-P5 of run-nx038-stage.md §0 (shared by run-nx040-stage.md).

    uv run --frozen python scripts/acceptance/s6_preflight.py --config PRIVATE.json [--owner-principal ID] [--days 14]

It connects only as roles.inspector (on stage the bootstrap DSN of the private S6 configuration), sets the session read only before
the first query (s6_common.connect_read_only) and reads the role token files of the same configuration to compare their digests. Each
item is ok | missing | expired | no_data with counts only (no token, DSN, host, path, user name or content):

  P1  the migrations the S6 checks need are in control.schema_migrations
  P2  the evolution principals as evolution_enroll.py --check judges them: writer and evaluator registered with one purpose each and
      different tokens, a test-only evaluation credential, a distinct real-only reader, the three LOGIN roles
  P3  the tenant's real evolution budget limit: present, its period running now (expired once period_end has passed)
  P4  the synthetic owner's grants (Budget.set, nexloop.budget.loosen, nexloop.consumer.erase_synthetic), the two budget Actions
      published and active, approval policy manifest >= 5
  P5  synthetic Consumers with a retrieval miss in the lookback (>= 4 for the holdouts) and near-duplicate Run pairs across Consumers
      (AT-048 route b; 0 pairs is no_data: that route is not planned)

No data is not a pass: only ok counts, and the exit is 0 only when every item is ok (1 otherwise, 2 for a refused configuration).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import itertools
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s6_common as C  # noqa: E402
import s6_stage_inputs as I  # noqa: E402

SCHEMA = 'nexloop-s6-preflight/1'
# 0235 diagnosis, 0237 candidates, 0241 evaluation, 0248 L1, 0252 Schema proposals (NX-040), 0286 AT-048 Consumer leakage.
MIGRATIONS = ('0235', '0237', '0241', '0248', '0252', '0286')
OWNER_GRANTS = ('eios:action:Budget.set:1', 'eios:action:nexloop.budget.loosen:1', 'eios:action:nexloop.consumer.erase_synthetic:1')
BUDGET_ACTIONS = ('eios:action:Budget.set:1', 'eios:action:nexloop.budget.loosen:1')
POLICY_MIN = 5
LOGIN_ROLES = ('nexloop_evo_writer', 'nexloop_evo_evaluator', 'nexloop_evo_reader')
DEFAULT_OWNER = 'stage-principal-approver3'
NO_DATA_IS_NOT_A_PASS = 'no data is not a pass'


def item(status, **counts):
    return {'status': status, **counts}


def p1(db):
    present = {r[0] for r in db.execute('select version from control.schema_migrations where version = any(%s)', (list(MIGRATIONS),))}
    missing = [v for v in MIGRATIONS if v not in present]
    return item('missing' if missing else 'ok', required=len(MIGRATIONS), present=len(present), missing=missing)


def _digest(path):
    try:
        return hashlib.sha256(C.read_private(path, 4096).strip().encode()).hexdigest()
    except C.Refused:
        return None


def p2(db, tenant, roles):
    files = {'writer': (roles.get('writer') or {}).get('token_file'), 'evaluator': (roles.get('evaluator') or {}).get('token_file'),
             'reader': (roles.get('reader') or {}).get('token_file'), 'evaluation': (roles.get('evaluation') or {}).get('token_file')}
    digests = {k: (_digest(v) if isinstance(v, str) else None) for k, v in files.items()}
    if None in digests.values():
        return item('no_data', reason='a role token file of the S6 configuration is missing or unreadable', tokens_read=sum(v is not None for v in digests.values()))
    creds = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
        "select token_digest,worlds,status,binding->>'subject_principal_id' from authz.nexloop_service_credentials where token_digest = any(%s)",
        (list(digests.values()),))}
    purposes = {r[0]: (r[1], r[2]) for r in db.execute('select principal_id,tenant_id,purpose from authz.nexloop_evo_principals where principal_id = any(%s)',
                                                         ([c[2] for c in creds.values() if c[2]],))}
    evaluation = {r[0] for r in db.execute('select token_digest from authz.nexloop_evo_evaluation_credentials where tenant_id=%s', (tenant,))}
    logins = {r[0] for r in db.execute('select rolname from pg_roles where rolcanlogin and rolname = any(%s)', (list(LOGIN_ROLES),))}
    w, e, rd, t = digests['writer'], digests['evaluator'], digests['reader'], digests['evaluation']
    principal = lambda d: creds.get(d, (None, None, None))[2]  # noqa: E731
    checks = {
        'writer_evaluator_exclusive': (len({w, e, t}) == 3 and purposes.get(principal(w)) == (tenant, 'writer')
                                       and purposes.get(principal(e)) == (tenant, 'evaluator') and w not in evaluation and e not in evaluation),
        'evaluation_credential_test_only': (t in evaluation and creds.get(t, (None,))[0] == ['test'] and creds.get(w, (None,))[0] == ['real']
                                            and creds.get(e, (None,))[0] == ['real'] and all(creds.get(x, (None, None))[1] == 'active' for x in (w, e, t))),
        'reader_distinct_real_only': (rd not in (w, e, t) and rd not in evaluation and principal(rd) not in purposes
                                      and creds.get(rd, (None, None))[:2] == (['real'], 'active')),
    }
    found = len(creds) + len(purposes) + len(evaluation) + len(logins)
    if not found:
        return item('no_data', reason='nothing registered for these tokens', checks=checks, login_roles=0)
    ok = all(checks.values()) and len(logins) == len(LOGIN_ROLES)
    return item('ok' if ok else 'missing', checks=checks, login_roles=len(logins), login_roles_required=len(LOGIN_ROLES))


def p3(db, tenant, now):
    rows = db.execute("""select unit,limit_amount::text,period_start,period_end from control.nexloop_budget_limits
        where tenant_id=%s and world='real' and budget_kind='evolution'""", (tenant,)).fetchall()
    if not rows:
        return item('missing', rows=0, remedy='run-nx038 §2.0: loosen_budget (approver3 requests and executes, approver2 approves)')
    unit, amount, start, end = rows[0]
    period = {'unit': unit, 'limit_amount': amount, 'period_start': I._iso(start), 'period_end': I._iso(end)}
    if end <= now:
        return item('expired', rows=len(rows), **period, remedy='run-nx038 §2.0: same-amount set_budget for the next period (no approval); '
                    'refused -> loosen_budget with from_* and expected_revision of this row')
    if start > now:
        return item('missing', rows=len(rows), **period, reason='the period has not started')
    return item('ok', rows=len(rows), **period, days_left=(end - now).days)


def p4(db, tenant, owner):
    grants = db.execute("""select count(distinct entity_key[2]) from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants'
        and entity_key[1]=%s and entity_key[2] = any(%s)""", (tenant, owner, list(OWNER_GRANTS))).fetchone()[0]
    actions = db.execute("""select count(distinct resource_id) from control.nexloop_action_definitions where tenant_id=%s and world='real'
        and active and resource_id = any(%s)""", (tenant, list(BUDGET_ACTIONS))).fetchone()[0]
    policy = db.execute('select max(manifest_version) from control.nexloop_approval_policy_publications').fetchone()[0]
    counts = dict(grants=grants, grants_required=len(OWNER_GRANTS), active_actions=actions, active_actions_required=len(BUDGET_ACTIONS),
                  approval_policy=policy, approval_policy_min=POLICY_MIN)
    if not grants and not actions and policy is None:
        return item('no_data', **counts)
    ok = grants == len(OWNER_GRANTS) and actions == len(BUDGET_ACTIONS) and policy is not None and policy >= POLICY_MIN
    return item('ok' if ok else 'missing', **counts, note='the approver2 vote cooldown is seen only at the approval (401/403)')


def p5(db, tenant, now, days):
    src = I.probe(db, tenant, now=now, days=days, personas=I.SYNTHETIC)
    stats = src['window'].get('evidence') or {}
    since = now - datetime.timedelta(days=days)
    rows = db.execute("""with r as (select a.run_id::text run_id,array_agg(distinct t order by t) terms
            from runtime.nexloop_semantic_accesses a, unnest(a.term_hashes) t
            where a.tenant_id=%s and a.world='real' and a.kind='access' and a.run_id is not null and a.recorded_at>=%s group by a.run_id)
        select r.run_id,r.terms,p.consumer_id from r
          join control.nexloop_consumer_personas p on p.tenant_id=%s and p.world='real' and p.persona = any(%s)
           and p.consumer_id=runtime.nexloop_evo_run_consumer(%s,'real',r.run_id)""", (tenant, since, tenant, list(I.SYNTHETIC), tenant)).fetchall()
    # The script's own definition (s6_stage_evolution inputs): two Runs of different Consumers with the same term set. Here every
    # term of the Run's reads counts; the script groups by its own clusters, so this is a count to plan by, not the script's list.
    pairs = sum(1 for a, b in itertools.combinations(rows, 2) if a[1] == b[1] and a[2] != b[2])
    consumers = stats.get('consumers_with_miss', 0)
    counts = dict(synthetic_consumers_with_miss=consumers, consumers_required=4, runs_with_miss=stats.get('runs_with_miss', 0),
                  near_duplicate_pairs=pairs)
    if not stats.get('runs', 0):
        return item('no_data', **counts)
    if consumers < 4:
        return item('missing', **counts, remedy='run-s6-stage-prep §2.2a (customer-5) and §2.2 (synthetic interactions)')
    if not pairs:
        return item('no_data', **counts, reason='no near-duplicate pair: AT-048 route b is not planned (route a still runs)')
    return item('ok', **counts)


def preflight(db, config, *, now, owner, days):
    tenant = config['tenant_id']
    db.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    items = {'P1': p1(db), 'P2': p2(db, tenant, config.get('roles') or {}), 'P3': p3(db, tenant, now), 'P4': p4(db, tenant, owner),
             'P5': p5(db, tenant, now, days)}
    return {'schema': SCHEMA, 'generated_at': I._iso(now), 'release': config['release'], 'rule': NO_DATA_IS_NOT_A_PASS,
            'passed': all(v['status'] == 'ok' for v in items.values()), 'not_ok': sorted(k for k, v in items.items() if v['status'] != 'ok'),
            'items': items}


def main(argv=None, out=sys.stdout, now=None):
    p = argparse.ArgumentParser(description='S6 stage preflight: P1-P5, read only')
    p.add_argument('--config', required=True)
    p.add_argument('--owner-principal', default=DEFAULT_OWNER, help='the synthetic owner who sets the budget (never the owner\'s own account)')
    p.add_argument('--days', type=int, default=14)
    a = p.parse_args(argv)
    try:
        config = C.load_config(a.config)
        dsn_file = ((config.get('roles') or {}).get('inspector') or {}).get('dsn_file')
        if type(dsn_file) is not str or not os.path.isabs(dsn_file):
            raise C.Refused('roles.inspector.dsn_file: an absolute path')
        if not 1 <= a.days <= 90:
            raise C.Refused('--days 1..90')
        with C.connect_read_only(dsn_file) as db:
            doc = preflight(db, config, now=now or datetime.datetime.now(datetime.UTC), owner=a.owner_principal, days=a.days)
    except C.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    print(json.dumps(C.Redactor(config)(doc), ensure_ascii=False, indent=2, default=str), file=out)
    return 0 if doc['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
