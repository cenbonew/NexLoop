#!/usr/bin/env python3
"""S6 stage acceptance: a read-only helper that finds the values of the private configuration's `inputs` on stage.

    uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config PRIVATE.json [--days 14] [--personas simulated_agent,synthetic]

It connects only as roles.inspector (on stage the bootstrap DSN from the private configuration), sets the session read only before the
first query and checks that it took, and reads:
  window             the span of the synthetic Consumers' Runs with semantic reads (persona from control.nexloop_consumer_personas,
                     Consumer derived per Run by runtime.nexloop_evo_run_consumer) and how many Runs / Consumers / misses fall in it
  parent_revision    the tenant's published semantic release (ontology.nexloop_semantic_release_pointers)
  protected_refs     a contact-limit or refusal definition ref the window's reads returned (R3 needs it in a read that fills the browse limit of 10)
  erasure_consumer_id  a synthetic Consumer with Runs in the window (R7)
  candidate_template whether the configured file exists (written by s6_stage_evolution.py candidate-template; not read here)
  semantic.candidate_id  an accepted_for_release semantic candidate not yet published (at023's L0 candidate)
  schema_type / new_property / new_type / escalation_action  an existing Object Type, a free property and type name, a published Action
Output (stdout, one JSON document, schema nexloop-s6-stage-inputs/1): `inputs`, a draft to paste into nexloop-s6-stage/1's inputs
(ids as they are; no token, DSN, tenant, path or personal content), and `sources`, per key {status: ok | blocked, value, source,
candidates, reason, remedy}. Exit 0 when nothing is blocked, 1 when something is, 2 when the configuration is refused.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s6_common as C  # noqa: E402

SCHEMA = 'nexloop-s6-stage-inputs/1'
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
# A contact-limit or refusal definition: by its ref's own words (the ontology has no fixed name for it).
CONTACT_LIMIT = re.compile(r'contact|refus|opt.?out|consent|do.?not|quiet|suppress|unsubscrib|limit|block', re.I)
DEFAULT_ACTION = 'eios:action:Grant.issue:1'
NEW_PROPERTY, NEW_TYPE = 's6_preferred_store', 'S6Voucher'
WIDE = 10  # R3: the protected ref is pushed past the browse limit (10) only in a read that fills it


def connect_read_only(path):
    import psycopg
    conn = psycopg.connect(C.read_private(path, 8192).strip(), autocommit=True, connect_timeout=10)
    conn.execute('set default_transaction_read_only=on')
    if conn.execute('show default_transaction_read_only').fetchone()[0] != 'on':
        conn.close()
        raise C.Refused('inspector is not read only')
    return conn


def ok(value, source, **more):
    return {'status': 'ok', 'value': value, 'source': source, **more}


def blocked(reason, remedy, **more):
    return {'status': 'blocked', 'value': None, 'reason': reason, 'remedy': remedy, **more}


def _iso(t):
    return t.astimezone(datetime.UTC).replace(microsecond=0).isoformat()


def probe(db, tenant, *, now, days=14, personas=('simulated_agent', 'synthetic'), template=None):
    """Every input's candidates and sources (db: a read-only connection)."""
    db.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))  # the tables' tenant_boundary policy (a non-superuser bootstrap)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    src = {}

    # window, erasure_consumer_id: the synthetic Consumers' Runs with semantic reads -----------------------------------------------
    since = now - datetime.timedelta(days=days)
    runs = q("""with r as (select a.run_id,min(a.recorded_at) first_at,max(a.recorded_at) last_at,
                   bool_or(a.kind='access' and (a.result_count=0 or a.unresolved_count>0)) miss,max(a.result_count) widest
                 from runtime.nexloop_semantic_accesses a where a.tenant_id=%s and a.world='real' and a.run_id is not null and a.recorded_at>=%s
                 group by a.run_id),
               c as (select r.*,runtime.nexloop_evo_run_consumer(%s,'real',r.run_id::text) consumer from r)
             select c.run_id::text,c.first_at,c.last_at,c.miss,c.widest,c.consumer from c
               join control.nexloop_consumer_personas p on p.tenant_id=%s and p.world='real' and p.consumer_id=c.consumer and p.persona=any(%s)
             order by c.first_at""", tenant, since, tenant, tenant, list(personas))
    consumers = {}
    for _, _, _, miss, _, consumer in runs:
        n = consumers.setdefault(consumer, [0, 0])
        n[0] += 1
        n[1] += bool(miss)
    stats = {'lookback_days': days, 'personas': list(personas), 'runs': len(runs), 'runs_with_miss': sum(bool(r[3]) for r in runs),
             'consumers': len(consumers), 'consumers_with_miss': sum(1 for n in consumers.values() if n[1]),
             'wide_reads': sum(1 for r in runs if (r[4] or 0) >= WIDE)}
    if not stats['runs_with_miss']:
        src['window'] = blocked('no Run of a synthetic Consumer with a retrieval miss in the lookback', 'run a stretch of synthetic interactions '
                                '(simulated_agent Consumers asking for what the semantic layer does not resolve), then probe again', evidence=stats)
    else:
        first, last = runs[0][1], runs[-1][2]
        window = {'since': _iso(first - datetime.timedelta(hours=1)), 'until': _iso(min(last + datetime.timedelta(hours=1), now))}
        if stats['consumers_with_miss'] < 4:
            src['window'] = blocked(f"{stats['consumers_with_miss']} synthetic Consumers with misses; the holdouts need at least 4 cluster units",
                                    'add synthetic interactions from more simulated_agent Consumers', candidates=[window], evidence=stats)
        else:
            src['window'] = ok(window, 'runtime.nexloop_semantic_accesses x control.nexloop_consumer_personas (1 h margin each side)', evidence=stats)
    if consumers:
        ranked = sorted(consumers, key=lambda k: (-consumers[k][1], -consumers[k][0], k))
        src['erasure_consumer_id'] = ok(ranked[0], 'a synthetic Consumer with Runs in the window (the most misses); R7 erases it, so it runs last',
                                        candidates=ranked[:5])
    else:
        src['erasure_consumer_id'] = blocked('no synthetic Consumer with Runs in the lookback', 'as for window')

    # parent_revision --------------------------------------------------------------------------------------------------------------
    pointer = q("select version from ontology.nexloop_semantic_release_pointers where tenant_id=%s and world='real'", tenant)
    if pointer and LABEL.fullmatch(pointer[0][0]):
        src['parent_revision'] = ok(pointer[0][0], 'ontology.nexloop_semantic_release_pointers (the current published semantic release)')
    else:
        src['parent_revision'] = blocked('no published semantic release for the tenant', 'the label only names the candidates\' parent: the '
                                         'dispatcher picks a baseline label (e.g. sem-0) and records it, or the owner publishes a release first')

    # protected_refs: contact-limit / refusal refs the window's reads returned -------------------------------------------------------
    seen = q("""select x->>'ref',count(*),count(*) filter (where a.result_count>=%s) from runtime.nexloop_semantic_accesses a,
                  jsonb_array_elements(case when jsonb_typeof(a.result_refs)='array' then a.result_refs else '[]'::jsonb end) x
                where a.tenant_id=%s and a.world='real' and a.kind='access' and a.run_id::text=any(%s) group by 1""",
             WIDE, tenant, [r[0] for r in runs])
    limits = sorted(((ref, n, wide) for ref, n, wide in seen if ref and CONTACT_LIMIT.search(ref)), key=lambda t: (-t[2], -t[1], t[0]))
    if limits and limits[0][2]:
        src['protected_refs'] = ok([limits[0][0]], 'result_refs of the window\'s semantic reads (in a read that fills the browse limit of 10)',
                                   candidates=[{'ref': r, 'reads': n, 'wide_reads': w} for r, n, w in limits[:10]])
    else:
        defined = [f'eios:property:{t}/{p}' for t, p in q("""select v.type_name,p->>'property_name' from ontology.object_type_versions v,
                       jsonb_array_elements(coalesce(v.definition->'properties','[]'::jsonb)) p
                     where v.tenant_id=%s and v.version=(select max(w.version) from ontology.object_type_versions w where w.tenant_id=v.tenant_id and w.type_name=v.type_name)""",
                                                          tenant) if p and CONTACT_LIMIT.search(t + '.' + p)]
        src['protected_refs'] = blocked('no contact-limit or refusal definition in a read that fills the browse limit (10) in the window' if limits else
                                        'no contact-limit or refusal definition among the window\'s read results',
                                        'let the synthetic interactions browse widely enough to return one of these definitions (R3), then probe again',
                                        candidates=[{'ref': r, 'reads': n, 'wide_reads': w} for r, n, w in limits[:10]] + [{'ref': r, 'reads': 0} for r in sorted(defined)[:10]])

    # candidate_template -------------------------------------------------------------------------------------------------------------
    if template and os.path.isfile(template):
        src['candidate_template'] = ok('<configured path>', 'the configured file exists (not read)')
    else:
        src['candidate_template'] = blocked('the configured template file does not exist' if template else 'inputs.candidate_template not configured',
                                            'configure a 0600 path, then run s6_stage_evolution.py candidate-template (after the window is ok)')

    # semantic.candidate_id ----------------------------------------------------------------------------------------------------------
    accepted = q("""select c.candidate_id::text,c.allowed_change_scope from runtime.nexloop_evo_candidates c
                    where c.tenant_id=%s and c.world='real' and c.dimension='semantic_content' and c.status='accepted_for_release'
                      and not exists (select 1 from ontology.nexloop_semantic_releases s where s.tenant_id=c.tenant_id and s.candidate_id=c.candidate_id)
                    order by c.created_at desc""", tenant)
    if accepted:
        cid, scope = accepted[0]
        src['semantic'] = ok({'candidate_id': cid, 'items': [{'kind': 'alias', 'ref': scope[0], 'text': '<SYNTHETIC_ALIAS>'}], 'reason': 'S6-R6 发布（合成）'},
                             'runtime.nexloop_evo_candidates (accepted_for_release, semantic_content, not yet published)', candidates=[a[0] for a in accepted[:5]])
    else:
        src['semantic'] = blocked('no accepted_for_release semantic candidate that is not yet published',
                                  'run s6_stage_evolution.py at023: its L0 candidate is the one (its id is in at023\'s result); then probe again')

    # schema_type, new_property, new_type, escalation_action --------------------------------------------------------------------------
    types = dict(q("""select v.type_name,v.definition from ontology.object_type_versions v where v.tenant_id=%s
                      and v.version=(select max(w.version) from ontology.object_type_versions w where w.tenant_id=v.tenant_id and w.type_name=v.type_name)""", tenant))
    if types:
        t = 'Consumer' if 'Consumer' in types else sorted(types)[0]
        names = {p.get('property_name') for p in (types[t] or {}).get('properties') or []}
        src['schema_type'] = ok(t, 'ontology.object_type_versions (latest version)', candidates=sorted(types)[:20])
        src['new_property'] = ok(NEW_PROPERTY, f'not a property of {t}') if NEW_PROPERTY not in names else \
            blocked(f'{t} already has {NEW_PROPERTY}', 'pick another property name with an s6_ prefix')
    else:
        src['schema_type'] = src['new_property'] = blocked('the tenant has no Object Type', 'bootstrap the tenant\'s ontology first')
    src['new_type'] = ok(NEW_TYPE, 'not an Object Type of the tenant') if NEW_TYPE not in types else \
        blocked(f'{NEW_TYPE} exists', 'pick another type name with an S6 prefix')
    actions = [a for (a,) in q("select resource_id from control.nexloop_action_definitions where tenant_id=%s and world='real' and active order by 1", tenant)]
    if actions:
        src['escalation_action'] = ok(DEFAULT_ACTION if DEFAULT_ACTION in actions else actions[0], 'control.nexloop_action_definitions (active)',
                                      candidates=actions[:20])
    else:
        src['escalation_action'] = blocked('the tenant has no published Action', 'publish the tenant\'s Actions first (stage_secrets publish)')
    return src


def draft(src, config_inputs=None):
    """The inputs draft: the values found, with the configuration's own values kept where nothing was found."""
    v = lambda k: src[k]['value']  # noqa: E731
    old = dict(config_inputs or {})
    evo = dict(old.get('evolution') or {})
    for k in ('window', 'parent_revision', 'protected_refs', 'erasure_consumer_id'):
        if v(k) is not None:
            evo[k] = v(k)
        else:
            evo.setdefault(k, None)
    out = {**old, 'evolution': evo}
    if out.get('candidate_template'):
        out['candidate_template'] = '<keep the configured path>'  # a private path never leaves the configuration
    for k in ('schema_type', 'new_property', 'new_type', 'escalation_action', 'semantic'):
        if v(k) is not None:
            out[k] = v(k)
    return out


def main(argv=None, out=sys.stdout, now=None):
    p = argparse.ArgumentParser(description='S6 stage: read-only probe of the configuration inputs')
    p.add_argument('--config', required=True)
    p.add_argument('--days', type=int, default=14)
    p.add_argument('--personas', default='simulated_agent,synthetic')
    a = p.parse_args(argv)
    try:
        config = C.load_config(a.config)
        dsn_file = ((config.get('roles') or {}).get('inspector') or {}).get('dsn_file')
        if type(dsn_file) is not str or not os.path.isabs(dsn_file):
            raise C.Refused('roles.inspector.dsn_file: an absolute path')
        personas = tuple(x for x in a.personas.split(',') if x)
        if not personas or not set(personas) <= {'simulated_agent', 'synthetic', 'real'} or not 1 <= a.days <= 90:
            raise C.Refused('--personas simulated_agent,synthetic[,real]; --days 1..90')
        inputs = config.get('inputs') if type(config.get('inputs')) is dict else {}
        with connect_read_only(dsn_file) as db:
            src = probe(db, config['tenant_id'], now=now or datetime.datetime.now(datetime.UTC), days=a.days, personas=personas,
                        template=inputs.get('candidate_template'))
    except C.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    redact = C.Redactor(config)
    doc = {'schema': SCHEMA, 'generated_at': _iso(now or datetime.datetime.now(datetime.UTC)), 'release': config['release'],
           'blocked': sorted(k for k, s in src.items() if s['status'] == 'blocked'),
           'inputs': draft(src, inputs), 'sources': src}
    print(json.dumps(redact(doc), ensure_ascii=False, indent=2, default=str), file=out)
    return 1 if doc['blocked'] else 0


if __name__ == '__main__':
    sys.exit(main())
