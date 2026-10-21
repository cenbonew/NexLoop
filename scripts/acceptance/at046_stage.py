#!/usr/bin/env python3
"""AT-046 on stage (NX-041 D10 / NX-042 T7 end to end; owner approval: enable the simulation process group after release 27): a
simulation world, its synthetic data, the consumer twin and one simulation Run, and nothing real changes. Same configuration and result
formats as the S6 stage scripts (scripts/acceptance/s6_common.py; result schema nexloop-s6-stage-result/1, check `at046-<phase>`).

Two phases, because the world's credentials, DSN files and the compose project (NEXLOOP_SIM_WORLD) come in between:

    at046_stage.py prepare --config PRIVATE.json [--dry-run]     (owner password from stdin)
        owner workbench: create_sim_world, generate_sim_world_data, publish_sim_catalog (the world's Action catalog: the real Actions
        with a stand-in, 0294) -> the world id (into the result's evidence)
    # dispatcher: sim_world_credentials.py (api, worker, agent[, twin]), sim_dsn_files.py, compose --profile simulation up -d
    On stage the run is three parts on two hosts (L6 sim-issue-install review 1.1): the read-only probe reaches PostgreSQL only over
    DATA_HOST's socket (bootstrap: local pg_hba rules) and the intake (nexloop_sim_api, nexloop_twin_port) only over APP_HOST's hostssl,
    so no single process has both. Each part writes its result; the next takes the earlier ones with --from:
    at046_stage.py probe-before --config DATA.json                                   DATA_HOST: digests, Consumers, the Run Action
    at046_stage.py intake --config APP.json --from BEFORE                             APP_HOST: twin, Run credential, accept
    at046_stage.py probe-after --config DATA.json --from BEFORE --from INTAKE         DATA_HOST: task end, Run world, digests again
    at046_stage.py merge --config ANY.json --from BEFORE --from INTAKE --from AFTER   no connection: one at046-run result
    at046_stage.py run --config PRIVATE.json [--dry-run]   (the three parts in one process: only where one host reaches both - local)
        probe (read only): a digest of every world-real row of the tenant (all tables with a world column) before
        twin: bind, observe, act through the twin port (nexloop_twin_port + the world's twin credential)       blocked before the eighth batch
        Run: the Run Action published for the world (prepare), the simulation intake (nexloop_sim_api + the world's api credential)
             issues the Run credential, fixes the input by reference and accepts the Run; the running simulation group (runtime worker,
             guard, simulation Host) claims and runs it; the probe waits for the task to end
        probe: the Run credential's world is the simulation world, every credential of the tenant's Run path is of that world, and the
             digest of every world-real row is unchanged (AT-046: no real credential, channel or effect)

inputs (configuration):
    world_purpose, generate: {kind, seed, consumers, events_per_consumer, segments}   (prepare)
    world: "sim:<uuid>" (run; from prepare), twin: {twin_id, version, seed, steps}, run_timeout_seconds (default 300)
roles: sim_api {dsn_file, token_file, signing_key_file, signing_key_id}, twin_port {dsn_file, token_file}, probe {dsn_file} (read only:
    the bootstrap DSN with default_transaction_read_only=on, the dispatcher's); owner {origin, ca_file, username_file}.
Exit: 0 passed, 1 failed or blocked, 2 input refused. Never a token, password or DSN in the output.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
from s6_common import RESULT_SCHEMA, Blocked, Redactor, Refused, Result, connect_from, read_private  # noqa: E402

SCHEMAS = ('authz', 'control', 'runtime', 'ontology')


class Ports:
    """owner (workbench actions), probe (read only), twin (TwinPort or None), intake (a callable running the intake's three steps)."""

    def __init__(self, *, owner=None, probe=None, twin=None, intake=None):
        self.owner, self.probe, self.twin, self.intake = owner, probe, twin, intake

    def act(self, operation, body, key):
        if self.owner is None:
            raise Blocked('owner workbench login not configured')
        return self.owner._call('POST', f'/api/v1/workbench/actions/{operation}', body, {'X-CSRF-Token': self.owner.csrf, 'Idempotency-Key': key})

    def read(self, sql, params=()):
        if self.probe is None:
            raise Blocked('read-only probe DSN not configured')
        with self.probe() as db, db.transaction():
            db.execute('set transaction read only')
            return db.execute(sql, params).fetchall()


def real_digest(ports, tenant):
    """Review 5.2 (as T7's real digest): every base table with a world column, its world='real' rows of this tenant (or of the table
    when it has no tenant column) - a digest of the rows, so an update shows as well as an insert or a delete; plus the real service
    credentials and the tenant's authority facts (review B1)."""
    tables = ports.read("""select c.table_schema||'.'||c.table_name, exists(select 1 from information_schema.columns t where t.table_schema=c.table_schema
          and t.table_name=c.table_name and t.column_name='tenant_id')
        from information_schema.columns c join information_schema.tables b on b.table_schema=c.table_schema and b.table_name=c.table_name
        where c.column_name='world' and b.table_type='BASE TABLE' and c.table_schema = any(%s) order by 1""", (list(SCHEMAS),))
    # One probe query for all digests (one connection, one snapshot), not one per table.
    parts, params = [], []
    for table, tenanted in tables:
        parts.append(f"select '{table}', md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {table} x where world='real'"
                     + (' and tenant_id=%s' if tenanted else ''))
        params += [tenant] if tenanted else []
    # L6 review B1: two tables without a world column - the real service credentials and the tenant's authority facts. The tenant row
    # (authority_revision) is left out: a credential revocation is expected to advance it.
    for table, where in (('authz.nexloop_service_credentials', "'real' = any(x.worlds) and x.tenant_id=%s"), ('authz.nexloop_authority_facts', 'x.tenant_id=%s')):
        parts.append(f"select '{table}', md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {table} x where {where}")
        params.append(tenant)
    out = dict(ports.read(' union all '.join(parts), tuple(params)))
    return out


def prepare(ports, config, r):
    inputs = config.get('inputs') or {}
    key = 'at046-' + uuid.uuid4().hex[:24]
    status, out = ports.act('create_sim_world', {'kind': 'simulation', 'purpose': inputs.get('world_purpose') or 'AT-046 stage acceptance (synthetic)',
                                                 'expires_in_days': 7}, key + '-w')
    world = out.get('world') if isinstance(out, dict) else None
    r.step('world_created', 'a sim:<uuid> world', {'status': status, 'world': world, 'created': out.get('created') if isinstance(out, dict) else None},
           status == 200 and isinstance(world, str) and world.startswith('sim:'), world=world)
    if not world:
        return
    g = {'kind': 'synthetic', 'seed': 'at046-stage', 'consumers': 3, 'events_per_consumer': {'min': 2, 'max': 2}, 'segments': {'trial': 1},
         **(inputs.get('generate') or {})}
    status, out = ports.act('generate_sim_world_data', {'world': world, **g, 'max_objects': None}, key + '-g')
    r.step('data_generated', 'a generation of synthetic Consumers', {'status': status, 'generation_id': out.get('generation_id') if isinstance(out, dict) else None,
           'counts': out.get('counts') if isinstance(out, dict) else None}, status == 200 and isinstance(out, dict) and bool(out.get('generation_id')))
    status, out = ports.act('publish_sim_catalog', {'world': world, 'reason': 'AT-046: the world catalog (stand-in Actions)'}, key + '-c')
    actions = out.get('actions') if isinstance(out, dict) else None
    r.step('catalog_published', 'the stand-in Actions published for the world (definitions only)',
           {'status': status, 'actions': actions, 'copied': out.get('copied') if isinstance(out, dict) else None},
           status == 200 and bool(actions))


def _world(config):
    world = (config.get('inputs') or {}).get('world')
    if not isinstance(world, str) or not world.startswith('sim:'):
        raise Refused('inputs.world: the sim:<uuid> world of the prepare phase')
    return world


def _effect():
    from nexloop_eios.effect_contexts import EFFECT
    return 'eios:action:' + EFFECT + ':1'


def probe_before(ports, config, r, prior=None):
    """DATA_HOST, the read-only probe over the socket: the world's synthetic Consumers, the Run Action in its catalog, the real digest."""
    world, tenant = _world(config), config['tenant_id']
    before = real_digest(ports, tenant)
    consumers = [row[0] for row in ports.read("select object_id from runtime.nexloop_sim_objects where tenant_id=%s and world=%s and type_name='Consumer' order by 1",
                                              (tenant, world))]
    r.step('synthetic_consumers_present', 'at least one synthetic Consumer in the world', {'consumers': len(consumers)}, bool(consumers),
           consumers=consumers[:3])
    published = ports.read('select count(*) from control.nexloop_action_definitions where tenant_id=%s and world=%s and resource_id=%s and active',
                           (tenant, world, _effect()))[0][0]
    r.step('run_action_published_for_the_world', 'the Run Action is in the world catalog (prepare: publish_sim_catalog)',
           {'published': bool(published)}, bool(published))
    r.step('real_digest_before', 'the world-real digests taken', {'tables': len(before)}, bool(before), digests=before, world=world)
    chain(r, config, prior, uuid.uuid4().hex)


def intake_phase(ports, config, r, prior):
    """APP_HOST, the simulation one-off container (sim_api and twin_port over hostssl): the twin through its port, then the Run accepted."""
    world = _world(config)
    inputs = config.get('inputs') or {}
    chain(r, config, prior)
    consumers = evidence(prior, 'synthetic_consumers_present').get('consumers') or []
    twin = inputs.get('twin') or {}
    if ports.twin is None or not twin.get('twin_id'):
        r.block('twin_observe_act', 'roles.twin_port or inputs.twin not configured')
    elif not consumers:
        r.block('twin_observe_act', 'no synthetic Consumer (probe-before)')
    else:
        from nexloop_eios.twin_port import ConsumerTwin
        acts = ports.twin.run(ConsumerTwin(twin['twin_id'], int(twin.get('version') or 1), twin.get('seed') or 'at046-stage'), consumers[:3],
                              steps=int(twin.get('steps') or 2))
        provenance = all(a['provenance']['origin'] == 'twin' and a['provenance']['data_mode'] == 'simulation' for a in acts)
        r.step('twin_observe_act', 'every step through the port, simulated, with the twin provenance',
               {'steps': len(acts), 'events': sum(1 for a in acts if a.get('event_id')), 'provenance': provenance}, bool(acts) and provenance)
    if not passed(prior, 'run_action_published_for_the_world') or not consumers:
        r.block('simulation_run', 'probe-before did not find the Run Action published and a synthetic Consumer')
    elif ports.intake is None:
        r.block('simulation_run', 'roles.sim_api not configured')
    else:
        task_id, run_id = ports.intake(world, consumers[0], _effect())
        r.step('run_accepted', 'the simulation intake issued the Run credential and accepted the Run', {'accepted': bool(task_id)}, bool(task_id),
               task_id=task_id, run_id=run_id)


def probe_after(ports, config, r, prior):
    """DATA_HOST again: the task until it ends, the Run credential's world, the world's credentials, the real digest unchanged."""
    world, tenant = _world(config), config['tenant_id']
    inputs = config.get('inputs') or {}
    chain(r, config, prior)
    accepted = evidence(prior, 'run_accepted')
    if accepted.get('task_id'):
        deadline = time.monotonic() + int(inputs.get('run_timeout_seconds') or 300)
        status = None
        while time.monotonic() < deadline:
            status = ports.read('select status from runtime.jobs where job_id=%s', (accepted['task_id'],))[0][0]
            if status in ('succeeded', 'failed', 'dead_letter', 'cancelled'):
                break
            time.sleep(5)
        run_world = ports.read('select world from authz.nexloop_run_credentials where run_id=%s', (accepted['run_id'],))[0][0]
        r.step('simulation_run_completes', 'the task succeeded, its Run credential of the simulation world',
               {'task_status': status, 'run_world_is_simulation': run_world == world}, status == 'succeeded' and run_world == world, run_id=accepted['run_id'])
    used = ports.read("""select array_agg(distinct w) from authz.nexloop_service_credentials c, unnest(c.worlds) w
        where c.tenant_id=%s and c.sim_use is not null and c.status='active' and %s = any(c.worlds)""", (tenant, world))[0][0] or []
    # Review 5.3: none found is not a pass - the Run path needs the world's own credentials.
    r.step('world_credentials_only_of_the_world', 'credentials with a simulation use exist for this world, every one bound to it alone',
           {'worlds': used}, used == [world])
    before = evidence(prior, 'real_digest_before').get('digests') or {}
    after = real_digest(ports, tenant)
    changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
    r.step('real_side_unchanged', 'every world-real row of the tenant unchanged (digests of all tables with a world column)',
           {'tables': len(before), 'changed': changed}, not changed and bool(before))


def run(ports, config, r, prior=None):
    """The three parts in one process: only where one host reaches both the probe and the intake (local tests). On stage they run apart
    (probe-before and probe-after on DATA_HOST, intake on APP_HOST) and merge combines them (L6 sim-issue-install review 1.1)."""
    for part in (probe_before, intake_phase, probe_after):
        part(ports, config, r, r.steps)


def _tenant_ref(config):
    import hashlib
    return hashlib.sha256(('at046:' + config['tenant_id']).encode()).hexdigest()[:16]  # the tenant, never named in a result


def chain(r, config, prior, nonce=None):
    """L6 at046-split review 1.1: one run of the three parts. probe-before draws the run nonce; intake and probe-after carry the one of
    their --from results. A part's own chain step records nonce, world and tenant reference (load_prior checks them)."""
    if nonce is None:
        nonce = evidence(prior, 'run_chain').get('nonce')
        if not nonce:
            raise Refused('--from: no run nonce (a probe-before result of this script)')
    if not any(st.get('name') == 'run_chain' for st in r.steps):
        r.step('run_chain', 'one run of the three parts', {'part': r.check.removeprefix('at046-')}, True,
               nonce=nonce, world=_world(config), tenant_ref=_tenant_ref(config))


def evidence(prior, name):
    found = [s for s in (prior or []) if s.get('name') == name]
    return (found[-1].get('evidence') or {}) if found else {}


def passed(prior, name):
    return any(s.get('name') == name and s.get('passed') is True for s in (prior or []))


def load_prior(paths, config, parts):
    """The earlier parts' result documents (--from), in order (L6 at046-split review 1.1): each the part expected at its position
    (probe-before, intake, probe-after), never a dry run, all of one run (the same nonce), this world and tenant, executed in that
    order. Returns their steps and blocks, or the reasons the chain is broken."""
    steps, blocked, problems, docs = [], [], [], []
    for path in paths or []:
        try:
            docs.append(json.loads(Path(path).read_text().strip().splitlines()[-1]))
        except (OSError, ValueError, IndexError):
            raise Refused('--from: a result document of this script') from None
    if len(docs) != len(parts):
        problems.append(f'expected {len(parts)} result(s): ' + ', '.join(parts))
    world, ref = _world(config), _tenant_ref(config)
    nonces, times = set(), []
    for i, doc in enumerate(docs):
        want = 'at046-' + parts[i] if i < len(parts) else None
        if doc.get('schema') != RESULT_SCHEMA or doc.get('dry_run') or doc.get('check') != want:
            problems.append(f'--from #{i + 1}: not the {want} result (or a dry run)')
            continue
        link = next((st.get('evidence') or {} for st in doc.get('steps') or [] if st.get('name') == 'run_chain'), {})
        nonces.add(link.get('nonce'))
        if link.get('world') != world or link.get('tenant_ref') != ref:
            problems.append(f'--from #{i + 1}: another world or tenant')
        times.append(str(doc.get('executed_at') or ''))
        steps += doc.get('steps') or []
        blocked += doc.get('blocked') or []
    if len(nonces) > 1 or None in nonces:
        problems.append('the results are not of one run (run nonce)')
    if any(not t for t in times) or times != sorted(times) or len(set(times)) != len(times):
        problems.append('the results are not in the order probe-before < intake < probe-after')
    return steps, blocked, problems


REQUIRED = ('synthetic_consumers_present', 'run_action_published_for_the_world', 'real_digest_before', 'twin_observe_act',
            'world_credentials_only_of_the_world', 'real_side_unchanged')


def intake_from(roles):
    """The simulation intake's three steps (the D10 order): Run credential, input by reference, accept."""
    s = roles.get('sim_api') or {}
    if not all(s.get(k) for k in ('dsn_file', 'token_file', 'signing_key_file', 'signing_key_id')):
        return None

    def intake(world, consumer, effect):
        import tempfile
        from nexloop_eios.backend import open_backend
        from nexloop_eios.sim_context import sim_run_input
        with tempfile.TemporaryDirectory(prefix='at046-') as artifacts, open_backend(
                database_url=read_private(s['dsn_file'], 8192).strip(), artifact_root=artifacts, signing_key_file=s['signing_key_file'],
                signing_key_id=s['signing_key_id']) as api:
            source = api.authenticate(read_private(s['token_file'], 4096).strip(), world=world)
            run = source.issue_run_credential(action_resources=[effect])
            pack = sim_run_input(source, run.run_id, consumer, 8)
            command = {'schema_version': '1.0', 'run_id': run.run_id, 'tenant_id': json.loads(pack)['tenant_id'], 'world_id': world, 'mode': 'simulation',
                       'request_id': 'at046-' + uuid.uuid4().hex, 'trigger_event_id': str(uuid.uuid4()), 'role_ref': 'role:sim-consumer-reply',
                       'consumer_ref': 'consumer:' + consumer, 'goal_version_ref': 'goal:sim-at046', 'context_manifest_ref': 'artifact:sim-context',
                       'runtime_profile': 'deterministic-test', 'credential_ref': 'run_credential:' + run.run_id,
                       'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0', 'currency': 'CNY'},
                       'not_after': run.expires_at.isoformat(), 'runtime_owner_epoch': 1}
            accepted = source.accept_runtime_event(queue='operations', source_id='at046-stage', event_id='at046-' + run.run_id, run_token=run.token,
                                                   command=command, input=pack)
            return accepted['task_id'], run.run_id
    return intake


def ports_from(config, password):
    roles = config.get('roles') or {}
    owner = None
    o = config.get('owner') or {}
    if o.get('origin') and o.get('username_file') and password:
        from real_dispatch_switch import HttpsTransport, Workbench
        owner = Workbench(HttpsTransport(o['origin'], o.get('ca_file')), o['origin'])
        owner.login(read_private(o['username_file'], 1024).strip(), password)
    twin = None
    t = roles.get('twin_port') or {}
    if t.get('dsn_file') and t.get('token_file'):
        from nexloop_eios.twin_port import TwinPort
        twin = TwinPort(connect_from(t['dsn_file']), read_private(t['token_file'], 4096).strip(), (config.get('inputs') or {}).get('world', 'sim:'))
    probe = (roles.get('probe') or {}).get('dsn_file')
    return Ports(owner=owner, probe=connect_from(probe) if probe else None, twin=twin, intake=intake_from(roles))


PHASES = {'probe-before': probe_before, 'intake': intake_phase, 'probe-after': probe_after, 'run': run}
NEEDS = {'intake': ('probe-before',), 'probe-after': ('probe-before', 'intake'), 'merge': ('probe-before', 'intake', 'probe-after')}
PLAN = {'probe-before': ['probe (read only, DATA_HOST socket): world-real row digests, synthetic Consumers, the Run Action in the world catalog'],
        'intake': ['twin port (APP_HOST hostssl): bind, observe, act (when configured)', 'sim_api (APP_HOST hostssl): issue_run_credential, sim context register, accept'],
        'probe-after': ['probe (read only, DATA_HOST socket): task status until it ends, the Run credential world, the world credentials, digests again'],
        'merge': ['no connection: the three result documents (--from) combined into at046-run'],
        'prepare': ['owner workbench: create_sim_world', 'owner workbench: generate_sim_world_data', 'owner workbench: publish_sim_catalog'],
        'run': ['probe (read only): world-real row digests', 'twin port: bind, observe, act (when present)',
                'probe: the Run Action in the world catalog', 'sim_api: issue_run_credential, sim context register, accept',
                'probe: task status until it ends, the Run credential world', 'probe: world-real row digests again']}


def main(argv=None, *, ports=None, stdin=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('phase', choices=sorted(PLAN))  # run: the three parts in one process (local only)
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--from', dest='prior', action='append', default=[])
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    config = {}
    try:
        from s6_common import load_config
        config = load_config(args.config)
        check = 'at046-run' if args.phase == 'merge' else 'at046-' + args.phase
        result = Result(check, ['AT-046'], config, dry_run=args.dry_run, world='simulation')
        prior, prior_blocked, problems = [], [], []
        if args.phase in NEEDS and not args.dry_run:
            prior, prior_blocked, problems = load_prior(args.prior, config, NEEDS[args.phase])
            if problems and args.phase != 'merge':
                raise Refused(f'{args.phase}: ' + '; '.join(problems))
        if args.phase == 'merge':
            # The three parts as one result: every step and every block, the verdict recomputed (a failure is never masked). A broken
            # chain or a required step missing fails it (a step blocked, e.g. the twin, stays null, never true).
            if args.dry_run:
                result.step('plan', 'the entries this phase calls', {'calls': PLAN[args.phase]}, True)
                return result.write(out)
            _world(config)
            result.steps, result.blocked = [st for st in prior if st.get('name') != 'run_chain'], list(prior_blocked)
            result.step('parts_of_one_run', 'probe-before, intake, probe-after: one run nonce, this world and tenant, in order', {'problems': problems}, not problems)
            names = {st.get('name') for st in prior}
            missing = [n for n in REQUIRED if n not in names]
            if 'simulation_run' not in names:  # the Run ran (not blocked): its two steps must be there
                missing += [n for n in ('run_accepted', 'simulation_run_completes') if n not in names]
            result.step('required_steps_present', 'every step of AT-046 present (or blocked with its reason)', {'missing': missing}, not missing)
            return result.write(out)
        if args.dry_run:
            result.step('plan', 'the entries this phase calls', {'calls': PLAN[args.phase]}, True)
            return result.write(out)
        owner = None
        if ports is None:
            password = (stdin or sys.stdin).readline().rstrip('\n') if args.phase == 'prepare' and (config.get('owner') or {}).get('origin') else None
            ports = ports_from(config, password)
            owner = ports.owner
        try:
            if args.phase == 'prepare':
                prepare(ports, config, result)
            else:
                PHASES[args.phase](ports, config, result, prior)
        except Blocked as error:
            result.block(args.phase, str(error))
        except Refused:
            raise
        except Exception as error:  # noqa: BLE001 - by type only
            result.step('unexpected_error', 'no error', {'error': type(error).__name__}, False)
        finally:
            if owner is not None:
                owner.logout()
        return result.write(out)
    except Refused as error:
        print(json.dumps({'ok': False, 'refused': Redactor(config)(str(error))}), file=out)
        return 2


if __name__ == '__main__':
    sys.exit(main())
