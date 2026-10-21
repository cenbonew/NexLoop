#!/usr/bin/env python3
"""S6 stage acceptance, NX-040 part (L2): AT-024 / S6-R9 structure permissions on the Schema path, S6-R8 Schema compatibility,
S6-R10 breaking changes closed, S6-R6 semantic release rollback and watch. The NX-038/039 parts are L3's (same configuration and
result formats: scripts/acceptance/s6_common.py).

    s6_stage_schema.py <at024|r8|r10|r6> --config PRIVATE.json [--dry-run] [--publish]      (owner password from stdin)

Everything runs through the governed entries a deployment has, never SQL on tables: the evolution writer (nexloop_evo_writer with the
writer's service credential: candidate proposal, Schema proposal freeze, bundle freeze), the evaluator (nexloop_evo_evaluator with
its credential: evaluation open, reflow reservation, conclude, release permit) and the owner's workbench session (the Schema
publication routes: basis, check, publish; the governed Actions publish_semantic_release / rollback_semantic_release; the semantic
release read). Synthetic changes only (names from the configuration); evaluation in the test world (D18); nothing is published to the
formal Schema unless --publish (r8 only, the owner's B-class step).

inputs (configuration):
    candidate_template   private JSON: a valid evolution candidate body of this tenant (diagnosis_id, candidate, category, patch,
                         case_groups, regroups, source_consumer_ids) - L3's NX-038 stage step produces it from an open diagnosis
    schema_type          an existing Object Type of the tenant (e.g. Consumer); new_property: a property name it does not have
    new_type             an Object Type name that does not exist; escalation_action: an Action ref (default eios:action:Grant.issue:1)
    restricted           optional {type_name, property_name}: a property name in an owner-restricted group (AT-024 re-exposure)
    semantic             r6: {candidate_id (accepted_for_release), items: [{kind, ref, text}], reason}
    seed                 the bundle seed, 32 hex (default: derived from the candidate id)
    reflow_amount        the reflow reservation in CNY, counted as evolution (default 0.01)

Checks:
  at024  AT-024, S6-R9: refused at the candidate write (an Action in the change scope), at the proposal freeze (an Action carried in the
         proposal; a type outside the candidate's scope), and at the publication gate (a new type carries no Action - definition and
         authority are published apart - and is not published without an accepted nx040-gates-1 report: permit_missing; with
         `restricted`, the re-exposure is refused: property_security_bypass). Nothing published.
  r8     S6-R8: an add_optional_property proposal through G9-G13 (bundle nx040-gates-1, evaluation open, tool regression set from the
         Parent's Actions, Context volume, reflow reserved as evolution) - accepted and a release permit; the migration catalog and
         bootstrap.verify unchanged. --publish: the owner publishes it; still no catalog change.
  r10    S6-R10: three breaking changes of the stage Parent (removed property, changed value type, required property), each refused by
         F1 with its own code.
  r6     S6-R6: the owner publishes a semantic release and rolls it back: a new, higher version row; the rollback report changes no
         Claim (claims_changed 0); the 7-day watch is active (the alert's firing is the local positive control).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import datetime
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from s6_common import Blocked, Redactor, Refused, Result, connect_from, digest, load_config, read_private, refusal, token_digest  # noqa: E402

ESCALATION = 'eios:action:Grant.issue:1'
CHECKS = {'at024': ['AT-024', 'S6-R9'], 'r8': ['S6-R8'], 'r10': ['S6-R10'], 'r6': ['S6-R6']}


class Ports:
    """The governed entries. Built from the private configuration (main) or from a test's fixtures (the local positive control)."""

    def __init__(self, *, writer_connect=None, writer_digest=None, writer_credential=None, evaluator_connect=None, evaluator_credential=None,
                 verify_connect=None, owner=None):
        self.writer_connect, self.writer_digest, self.writer_credential = writer_connect, writer_digest, writer_credential
        self.evaluator_connect, self.evaluator_credential = evaluator_connect, evaluator_credential
        self.verify_connect, self.owner = verify_connect, owner

    def _writer(self, sql, *args):
        if not self.writer_connect:
            raise Blocked('writer role and credential not configured')
        with self.writer_connect() as db:
            return db.execute(sql, (self.writer_digest, *args)).fetchone()[0]

    def propose(self, body):
        return self._writer('select authz.nexloop_evo_candidate_propose(%s,%s)', json.dumps(body))

    def freeze_proposal(self, body):
        return self._writer('select authz.nexloop_schema_proposal_freeze(%s,%s)', json.dumps(body))

    def freeze_bundle(self, candidate, seed):
        from nexloop_eios.evolution_store import CandidateStore
        if not self.writer_connect:
            raise Blocked('writer role and credential not configured')
        return CandidateStore(self.writer_connect, self.writer_credential).freeze_bundle(candidate, seed=seed)

    def evaluator(self):
        from nexloop_eios.evolution_store import EvaluatorStore
        if not self.evaluator_connect:
            raise Blocked('evaluator role and credential not configured')
        return EvaluatorStore(self.evaluator_connect, self.evaluator_credential)

    def verify(self):
        from nexloop_eios.bootstrap import verify
        if not self.verify_connect:
            raise Blocked('verify connection not configured')
        with self.verify_connect() as db, db.transaction():
            db.execute('set transaction read only')
            got = verify(db)
        return {'revision': got['revision'], 'migration_count': got['migration_count'], 'digest': digest(got)}

    def owner_get(self, path):
        if self.owner is None:
            raise Blocked('owner workbench login not configured')
        return self.owner._call('GET', path)

    def owner_post(self, path, body, key):
        if self.owner is None:
            raise Blocked('owner workbench login not configured')
        return self.owner._call('POST', path, body, {'X-CSRF-Token': self.owner.csrf, 'Idempotency-Key': key})


def _template(inputs):
    path = inputs.get('candidate_template')
    if not path:
        raise Blocked('inputs.candidate_template missing (L3 NX-038 stage step: a candidate body of an open diagnosis)')
    try:
        return json.loads(read_private(path))
    except ValueError:
        raise Refused('candidate_template: not JSON') from None


def candidate_body(inputs, scope, change_kind, targets):
    body = copy.deepcopy(_template(inputs))
    body['candidate'] = {**body['candidate'], 'candidate_id': str(uuid.uuid4()), 'dimension': 'ontology_schema', 'allowed_change_scope': scope}
    body['patch'] = {**body['patch'], 'change_kind': change_kind, 'target_refs': targets}
    return body


def freeze_body(candidate, operation, type_name, change, **extra):
    return {'proposal_id': str(uuid.uuid4()), 'candidate_id': candidate, 'operation': operation, 'type_name': type_name, 'change': change,
            'claim_ids': [], 'candidate_definition_ids': [], 'compatibility_report_digest': 'a' * 64, 'permission_proof_digest': 'b' * 64, **extra}


def refused_with(call, text):
    """(refused as expected, observed)."""
    try:
        out = call()
    except Blocked:
        raise
    except Exception as error:  # noqa: BLE001 - the governed function's refusal is the observation
        r = refusal(error)
        return text in (r['message'] or ''), r
    return False, {'accepted': True, 'result_digest': digest(out)}


def property_candidate(ports, inputs):
    t, p = inputs['schema_type'], inputs['new_property']
    body = candidate_body(inputs, [f'eios:object_type:{t}', f'eios:property:{t}.{p}'], 'add_optional_property', [f'eios:object_type:{t}'])
    out = ports.propose(body)
    return out['candidate']['candidate_id']


def check_at024(ports, inputs, r):
    t, p, n = inputs['schema_type'], inputs['new_property'], inputs['new_type']
    action = inputs.get('escalation_action') or ESCALATION
    # 1. The candidate write: an Action in the change scope.
    body = candidate_body(inputs, [f'eios:object_type:{t}', action], 'add_optional_property', [f'eios:object_type:{t}'])
    ok, obs = refused_with(lambda: ports.propose(body), 'change scope not allowed')
    r.step('candidate_write_refuses_an_action_in_scope', 'refused: change scope not allowed', obs, ok)
    # 2. The proposal freeze: an Action carried in the proposal; a type outside the candidate's scope.
    cid = property_candidate(ports, inputs)
    ok, obs = refused_with(lambda: ports.freeze_proposal(freeze_body(cid, 'add_optional_property', t, {'property_name': p, 'value_type': 'string'},
                                                                     actions=[{'stable_name': action}])), 'proposal invalid')
    r.step('proposal_freeze_refuses_a_carried_action', 'refused: proposal invalid', obs, ok)
    ok, obs = refused_with(lambda: ports.freeze_proposal(freeze_body(cid, 'add_object_type', n, {'properties': [{'property_name': 'amount', 'value_type': 'number'}]})),
                           'outside the candidate scope')
    r.step('proposal_freeze_refuses_a_type_outside_scope', 'refused: outside the candidate scope', obs, ok)
    # 3. The publication gate: a new type carries no Action and is not published without an accepted nx040-gates-1 report.
    body = candidate_body(inputs, [f'eios:object_type:{n}'], 'add_object_type', [f'eios:object_type:{n}'])
    new_cid = ports.propose(body)['candidate']['candidate_id']
    frozen = ports.freeze_proposal(freeze_body(new_cid, 'add_object_type', n, {'properties': [{'property_name': 'amount', 'value_type': 'number'}]}))
    pid = frozen['proposal']['proposal_id']
    status, check = ports.owner_get(f'/api/v1/workbench/schema-proposals/{pid}/check')
    actions = check.get('successor_actions') if status == 200 else None
    r.step('new_type_publishes_no_action', 'the next version carries 0 Actions (definition and authority published apart)',
           {'status': status, 'successor_actions': actions, 'allowed': (check.get('compatibility') or {}).get('allowed') if status == 200 else None},
           status == 200 and actions == 0)
    status, out = ports.owner_post(f'/api/v1/workbench/schema-proposals/{pid}/publish', {'rationale': 'S6 AT-024 gate check (synthetic)'},
                                   's6-at024-gate-' + pid.replace('-', '')[:24])
    r.step('publication_gate_refuses_without_accepted_gates', 'published false, codes [permit_missing]',
           {'status': status, 'published': out.get('published') if isinstance(out, dict) else None, 'codes': out.get('codes') if isinstance(out, dict) else None},
           status == 200 and out.get('published') is False and 'permit_missing' in (out.get('codes') or []))
    restricted = inputs.get('restricted')
    if restricted:
        rt, rp = restricted['type_name'], restricted['property_name']
        body = candidate_body(inputs, [f'eios:object_type:{rt}', f'eios:property:{rt}.{rp}'], 'add_optional_property', [f'eios:object_type:{rt}'])
        rcid = ports.propose(body)['candidate']['candidate_id']
        rpid = ports.freeze_proposal(freeze_body(rcid, 'add_optional_property', rt, {'property_name': rp, 'value_type': 'string'}))['proposal']['proposal_id']
        status, check = ports.owner_get(f'/api/v1/workbench/schema-proposals/{rpid}/check')
        codes = (check.get('compatibility') or {}).get('codes') if status == 200 else None
        r.step('publication_gate_refuses_a_restricted_re_exposure', 'codes include property_security_bypass', {'status': status, 'codes': codes},
               status == 200 and 'property_security_bypass' in (codes or []))


def _sample(schema):
    kind = (schema or {}).get('type')
    if kind == 'object':
        return {k: _sample((schema.get('properties') or {}).get(k, {})) for k in schema.get('required', [])}
    return {'string': 'x', 'integer': 1, 'number': 1, 'boolean': True, 'array': []}.get(kind, 'x')


def gates_inputs(basis, reserved, cost='0'):
    """The F4 inputs from the publication basis (as the evaluator's regression set): F1 change, Parent and per-Action cases."""
    from nexloop_eios.schema_publication import _f1_actions, build
    publication, _ = build(basis, 'publish')
    schemas = {row['definition']['stable_name']: {'parent': row['definition'].get('input_schema') or {}, 'candidate': succ['definition'].get('input_schema') or {}}
               for row, succ in zip(basis['actions'], publication['actions'])}
    cases = []
    for name, s in schemas.items():
        cases += [{'id': name + '-valid', 'action': name, 'params': _sample(s['parent']), 'expect': 'valid'},
                  {'id': name + '-invalid', 'action': name, 'params': [], 'expect': 'invalid'}]
    name = basis['type_name']
    prop = publication['schema']['properties'][-1]
    change = {'op': 'add_optional_property', 'type_name': name, 'property': {k: prop[k] for k in ('property_name', 'value_type', 'required', 'nullable')},
              'successor_actions': [{'stable_name': a['stable_name'], 'replaces': a['version'] - 1, **a} for a in _f1_actions(publication['actions'])]}
    parent = {'types': {name: basis['schema']}, 'object_counts': {name: basis.get('object_count') or 0}, 'actions': _f1_actions(basis['actions']),
              'restricted_names': basis.get('restricted_names') or []}
    inputs = {'tool_cases': cases, 'action_schemas': schemas, 'context_volume': {'parent': 1000, 'candidate': 1000}, 'reflow_reserved': reserved, 'reflow_cost': cost}
    return change, parent, inputs


def check_r8(ports, inputs, r, *, publish=False):
    from nexloop_eios.schema_gates import evaluate_schema
    t, p = inputs['schema_type'], inputs['new_property']
    before = ports.verify()
    cid = property_candidate(ports, inputs)
    pid = ports.freeze_proposal(freeze_body(cid, 'add_optional_property', t, {'property_name': p, 'value_type': 'string'}))['proposal']['proposal_id']
    frozen = ports.freeze_bundle(cid, inputs.get('seed') or hashlib.sha256(('s6-stage:' + cid).encode()).hexdigest()[:32])
    r.step('bundle_freezes_nx040_gates', 'hard_gates_version nx040-gates-1', {'hard_gates_version': frozen['bundle'].get('hard_gates_version')},
           frozen['bundle'].get('hard_gates_version') == 'nx040-gates-1', bundle_digest=frozen['bundle_digest'])
    ev = ports.evaluator()
    opened = ev.open(cid, frozen['bundle_digest'])
    r.step('evaluation_opens_in_the_test_world', 'opened', {'opened': opened.get('opened')}, opened.get('opened') is True)
    status, basis = ports.owner_get(f'/api/v1/workbench/schema-proposals/{pid}/basis')
    if status != 200:
        raise Blocked(f'owner basis read answered {status}')
    amount = str(inputs.get('reflow_amount') or '0.01')
    reserved = ev.reflow_reserve(cid, 'reflow:' + cid, amount, 'CNY')
    change, parent, gate_inputs = gates_inputs(basis, reserved.get('reserved') is True, amount)
    created = datetime.datetime.now(datetime.UTC).isoformat().replace('+00:00', 'Z')
    report = evaluate_schema(frozen['bundle'], frozen['bundle_digest'], change, parent, created_at=created, inputs=gate_inputs)
    gates = {g['gate']: g['result'] for g in report['hard_gates']}
    r.step('g9_g13_accept', 'conclusion accept, class minor, no failed gate',
           {'conclusion': report['conclusion'], 'change_class': report['change_class'], 'failed_gates': report['failed_gates'], 'gates': gates},
           report['conclusion'] == 'accept' and report['change_class'] == 'minor' and not report['failed_gates'], report_digest=digest(report))
    concluded = ev.conclude(report)
    permit = ev.permit_issue(cid) if concluded.get('status') == 'accepted_for_release' else {}
    r.step('accepted_for_release_with_a_permit', 'accepted_for_release and a release permit',
           {'status': concluded.get('status'), 'permit_issued': permit.get('issued')}, concluded.get('status') == 'accepted_for_release' and permit.get('issued') is True)
    after = ports.verify()
    r.step('catalog_and_verify_unchanged', 'the same revision, migration count and verify digest', {'before': before, 'after': after}, before == after)
    if publish:
        status, out = ports.owner_post(f'/api/v1/workbench/schema-proposals/{pid}/publish', {'rationale': 'S6-R8 add_optional_property (synthetic)'},
                                       's6-r8-publish-' + pid.replace('-', '')[:24])
        published = ports.verify()
        r.step('owner_publication_changes_no_catalog', 'published, catalog and verify still unchanged',
               {'status': status, 'published': out.get('published') if isinstance(out, dict) else None, 'version_after': out.get('version_after') if isinstance(out, dict) else None,
                'verify': published}, status == 200 and out.get('published') is True and published == before)
    return pid


BREAKING = (('removed_property', lambda d: d['properties'].pop(0), 'breaking_remove_property'),
            ('changed_value_type', lambda d: d['properties'][0].update(value_type='integer' if d['properties'][0].get('value_type') != 'integer' else 'string'),
             'breaking_change_value_type'),
            ('required_property', lambda d: d['properties'][0].update(required=True, nullable=False, default_value=None), 'breaking_required_property'))


def check_r10(ports, inputs, r):
    from nexloop_eios.evolution.schema_compat import judge
    pid = inputs.get('proposal_id')
    if not pid:
        t, p = inputs['schema_type'], inputs['new_property']
        cid = property_candidate(ports, inputs)
        pid = ports.freeze_proposal(freeze_body(cid, 'add_optional_property', t, {'property_name': p, 'value_type': 'string'}))['proposal']['proposal_id']
    status, basis = ports.owner_get(f'/api/v1/workbench/schema-proposals/{pid}/basis')
    if status != 200:
        raise Blocked(f'owner basis read answered {status}')
    name, parent = basis['type_name'], {'types': {basis['type_name']: basis['schema']}}
    if not (basis.get('schema') or {}).get('properties'):
        raise Blocked(f'the Parent of {name} has no property to break (choose inputs.schema_type with properties)')
    for label, mutate, code in BREAKING:
        definition = copy.deepcopy(basis['schema'])
        definition['version'] = definition['version'] + 1
        if label == 'required_property':
            # Required without a default on existing objects: refused (a required property with a default is not this case).
            definition['properties'][0].pop('default_value', None)
        mutate(definition)
        v = judge({'op': 'replace_type_version', 'type_name': name, 'definition': definition}, parent)
        r.step(f'breaking_{label}_refused', f'refused with {code}', {'allowed': v['allowed'], 'change_class': v['change_class'], 'codes': v['codes']},
               v['allowed'] is False and v['change_class'] == 'major' and code in v['codes'])


def check_r6(ports, inputs, r):
    sem = inputs.get('semantic') or {}
    if not sem.get('candidate_id') or not sem.get('items'):
        raise Blocked('inputs.semantic missing (an accepted_for_release semantic_content candidate from the NX-039 stage steps)')
    status, before = ports.owner_get('/api/v1/workbench/semantic-releases')
    if status != 200:
        raise Blocked(f'semantic release read answered {status}')
    v0 = before.get('current')
    key = 's6-r6-' + sem['candidate_id'].replace('-', '')[:24]
    status, pub = ports.owner_post('/api/v1/workbench/actions/publish_semantic_release',
                                   {'candidate_id': sem['candidate_id'], 'items': sem['items'], 'reason': sem.get('reason') or 'S6-R6 发布（合成）'}, key + '-p')
    r.step('publish_a_semantic_release', 'a new version', {'status': status, 'version': pub.get('version') if isinstance(pub, dict) else None, 'previous': v0},
           status == 200 and isinstance(pub, dict) and bool(pub.get('version')))
    target = v0 or (pub.get('previous_version') if isinstance(pub, dict) else None)
    if not target:
        raise Blocked('no earlier release version to roll back to (the first release of the tenant)')
    status, back = ports.owner_post('/api/v1/workbench/actions/rollback_semantic_release', {'target_version': target, 'reason': 'S6-R6 回退（合成）'}, key + '-r')
    report = (back or {}).get('report') or {} if isinstance(back, dict) else {}
    r.step('rollback_is_a_new_higher_version_and_changes_no_claim', 'a new version above the published one; claims_changed 0',
           {'status': status, 'version': back.get('version') if isinstance(back, dict) else None, 'rolled_back_to': back.get('rolled_back_to') if isinstance(back, dict) else None,
            'claims_changed': report.get('claims_changed'), 'claim_count': report.get('claim_count')},
           status == 200 and isinstance(back, dict) and back.get('version') not in (None, pub.get('version'), target) and report.get('claims_changed') == 0)
    status, after = ports.owner_get('/api/v1/workbench/semantic-review')
    watch = (after or {}).get('watch') or {} if isinstance(after, dict) else {}
    r.step('the_watch_is_active', 'release watch active >= 1 (firing: local positive control)', {'status': status, 'active': watch.get('active'),
           'regressing': watch.get('regressing')}, status == 200 and (watch.get('active') or 0) >= 1)


def plan(check, inputs):
    """What a dry run shows: the governed entries the check calls, in order."""
    writer, evaluator, owner = 'writer: authz.nexloop_evo_candidate_propose / nexloop_schema_proposal_freeze', 'evaluator', 'owner workbench'
    return {'at024': [writer + ' (refusals)', owner + ': GET schema-proposals/{id}/check, POST .../publish (refused)'],
            'r8': ['verify (read only)', writer + ', bundle freeze', evaluator + ': open, reflow_reserve, conclude, permit_issue',
                   owner + ': GET schema-proposals/{id}/basis', 'verify (read only)'],
            'r10': [writer + ' (when no proposal_id)', owner + ': GET schema-proposals/{id}/basis', 'F1 judge (local, pure)'],
            'r6': [owner + ': GET semantic-releases', owner + ': publish_semantic_release, rollback_semantic_release', owner + ': GET semantic-review (watch)']}[check]


def run(check, ports, inputs, result, *, publish=False):
    try:
        {'at024': lambda: check_at024(ports, inputs, result), 'r8': lambda: check_r8(ports, inputs, result, publish=publish),
         'r10': lambda: check_r10(ports, inputs, result), 'r6': lambda: check_r6(ports, inputs, result)}[check]()
    except Blocked as error:
        result.block(check, str(error))
    except Refused:
        raise
    except Exception as error:  # noqa: BLE001 - a failed step, by type only (its message may hold a value)
        result.step('unexpected_error', 'no error', {'error': type(error).__name__}, False)


def ports_from(config, password):
    roles = config.get('roles') or {}
    w, e, v = roles.get('writer') or {}, roles.get('evaluator') or {}, roles.get('verify') or {}
    owner = None
    o = config.get('owner') or {}
    if o.get('origin') and o.get('username_file') and password:
        from real_dispatch_switch import HttpsTransport, Workbench
        owner = Workbench(HttpsTransport(o['origin'], o.get('ca_file')), o['origin'])
        owner.login(read_private(o['username_file'], 1024).strip(), password)
    return Ports(writer_connect=connect_from(w['dsn_file']) if w.get('dsn_file') else None,
                 writer_digest=token_digest(w['token_file']) if w.get('token_file') else None, writer_credential=w.get('token_file'),
                 evaluator_connect=connect_from(e['dsn_file']) if e.get('dsn_file') else None, evaluator_credential=e.get('token_file'),
                 verify_connect=connect_from(v['dsn_file']) if v.get('dsn_file') else None, owner=owner)


def main(argv=None, *, ports=None, stdin=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('check', choices=sorted(CHECKS))
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--publish', action='store_true', help='r8 only: the owner publishes the accepted proposal (B class)')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    config = {}
    try:
        config = load_config(args.config)
        if args.publish and args.check != 'r8':
            raise Refused('--publish goes with r8')
        inputs = config.get('inputs') or {}
        result = Result('s6-schema-' + args.check, CHECKS[args.check], config, dry_run=args.dry_run)
        if args.dry_run:
            result.step('plan', 'the governed entries this check calls', {'calls': plan(args.check, inputs), 'publish': args.publish}, True)
            return result.write(out)
        for key in ('schema_type', 'new_property', 'new_type'):
            if args.check in ('at024', 'r8', 'r10') and not inputs.get(key):
                raise Refused(f'inputs.{key} required')
        owner = None
        if ports is None:
            password = (stdin or sys.stdin).readline().rstrip('\n') if (config.get('owner') or {}).get('origin') else None
            ports = ports_from(config, password)
            owner = ports.owner
        try:
            run(args.check, ports, inputs, result, publish=args.publish)
        finally:
            if owner is not None:
                owner.logout()
        return result.write(out)
    except Refused as error:
        print(json.dumps({'ok': False, 'refused': Redactor(config)(str(error))}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - the type alone, never a message or traceback
        print(json.dumps({'ok': False, 'error': type(error).__name__}), file=out)
        return 1


if __name__ == '__main__':
    sys.exit(main())
