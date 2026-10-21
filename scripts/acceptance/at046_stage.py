#!/usr/bin/env python3
"""AT-046 on stage (NX-041 D10 / NX-042 T7 end to end; owner approval: enable the simulation process group after release 27): a
simulation world, its synthetic data, the consumer twin and one simulation Run, and nothing real changes. Same configuration and result
formats as the S6 stage scripts (scripts/acceptance/s6_common.py; result schema nexloop-s6-stage-result/1, check `at046-<phase>`).

Two phases, because the world's credentials, DSN files and the compose project (NEXLOOP_SIM_WORLD) come in between:

    at046_stage.py prepare --config PRIVATE.json [--dry-run]     (owner password from stdin)
        owner workbench: create_sim_world, generate_sim_world_data -> the world id (into the result's evidence)
    # dispatcher: sim_world_credentials.py (api, worker, agent[, twin]), sim_dsn_files.py, compose --profile simulation up -d
    at046_stage.py run --config PRIVATE.json [--dry-run]
        probe (read only): the real tables' counts before
        twin: bind, observe, act through the twin port (nexloop_twin_port + the world's twin credential)       blocked before the eighth batch
        Run: the simulation intake (nexloop_sim_api + the world's api credential) issues the Run credential, fixes the input by reference
             and accepts the Run; the running simulation group (runtime worker, guard, simulation Host) claims and runs it; the probe
             waits for the task to end                                                   blocked while the Run Action is not published for the world
        probe: the Run credential's world is the simulation world, every credential of the tenant's Run path is of that world, and the
             real tables' counts are unchanged (AT-046: no real credential, channel or effect)

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
from s6_common import Blocked, Redactor, Refused, Result, connect_from, read_private  # noqa: E402

REAL_TABLES = ('runtime.nexloop_effect_intents', 'runtime.nexloop_effect_outbox', 'runtime.nexloop_outbound_messages', 'runtime.nexloop_message_outbox',
               'runtime.nexloop_incentive_issues', 'runtime.nexloop_outreach_sends', 'ontology.nexloop_claims', 'runtime.nexloop_conversation_messages')


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


def real_counts(ports):
    return {t: ports.read(f'select count(*) from {t}')[0][0] for t in REAL_TABLES}


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


def run(ports, config, r):
    inputs = config.get('inputs') or {}
    world = inputs.get('world')
    if not isinstance(world, str) or not world.startswith('sim:'):
        raise Refused('inputs.world: the sim:<uuid> world of the prepare phase')
    tenant = config['tenant_id']
    before = real_counts(ports)
    consumers = [row[0] for row in ports.read("select object_id from runtime.nexloop_sim_objects where tenant_id=%s and world=%s and type_name='Consumer' order by 1",
                                              (tenant, world))]
    r.step('synthetic_consumers_present', 'at least one synthetic Consumer in the world', {'consumers': len(consumers)}, bool(consumers))
    # The consumer twin (NX-042 T3/T7): through its port only.
    twin = inputs.get('twin') or {}
    if not ports.read("select to_regprocedure('authz.nexloop_twin_act(text,text,text,text,integer)') is not null")[0][0]:
        r.block('twin_observe_act', 'the twin port is not in this release (NX-042 T3, eighth batch)')
    elif ports.twin is None or not twin.get('twin_id'):
        r.block('twin_observe_act', 'roles.twin_port or inputs.twin not configured')
    else:
        from nexloop_eios.twin_port import ConsumerTwin
        acts = ports.twin.run(ConsumerTwin(twin['twin_id'], int(twin.get('version') or 1), twin.get('seed') or 'at046-stage'), consumers[:3],
                              steps=int(twin.get('steps') or 2))
        provenance = all(a['provenance']['origin'] == 'twin' and a['provenance']['data_mode'] == 'simulation' for a in acts)
        r.step('twin_observe_act', 'every step through the port, simulated, with the twin provenance',
               {'steps': len(acts), 'events': sum(1 for a in acts if a.get('event_id')), 'provenance': provenance}, bool(acts) and provenance)
    # The simulation Run: the Run Action must be published for the world (D10).
    from nexloop_eios.effect_contexts import EFFECT
    effect = 'eios:action:' + EFFECT + ':1'
    published = ports.read('select count(*) from control.nexloop_action_definitions where tenant_id=%s and world=%s and resource_id=%s and active',
                           (tenant, world, effect))[0][0]
    if not published:
        r.block('simulation_run', 'the Run Action is not published for the simulation world (no product path publishes the catalog per '
                'simulation world yet: D10 seeds it as trusted configuration in its tests)')
    elif ports.intake is None:
        r.block('simulation_run', 'roles.sim_api not configured')
    else:
        task_id, run_id = ports.intake(world, consumers[0], effect)
        deadline = time.monotonic() + int(inputs.get('run_timeout_seconds') or 300)
        status = None
        while time.monotonic() < deadline:
            status = ports.read('select status from runtime.jobs where job_id=%s', (task_id,))[0][0]
            if status in ('succeeded', 'failed', 'dead_letter', 'cancelled'):
                break
            time.sleep(5)
        run_world = ports.read('select world from authz.nexloop_run_credentials where run_id=%s', (run_id,))[0][0]
        r.step('simulation_run_completes', 'the task succeeded, its Run credential of the simulation world',
               {'task_status': status, 'run_world_is_simulation': run_world == world}, status == 'succeeded' and run_world == world, run_id=run_id)
    used = ports.read("""select array_agg(distinct w) from authz.nexloop_service_credentials c, unnest(c.worlds) w
        where c.tenant_id=%s and c.sim_use is not null and %s = any(c.worlds)""", (tenant, world))[0][0] or []
    r.step('world_credentials_only_of_the_world', 'every credential carrying a simulation use for this world is bound to it alone',
           {'worlds': used}, used in ([], [world]))
    after = real_counts(ports)
    r.step('real_side_unchanged', 'the real tables have the same counts', {'changed': sorted(t for t in REAL_TABLES if before[t] != after[t])}, before == after)


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


PLAN = {'prepare': ['owner workbench: create_sim_world', 'owner workbench: generate_sim_world_data'],
        'run': ['probe (read only): real table counts', 'twin port: bind, observe, act (when present)',
                'sim_api: issue_run_credential, sim context register, accept (when the Run Action is published for the world)',
                'probe: task status until it ends, the Run credential world', 'probe: real table counts again']}


def main(argv=None, *, ports=None, stdin=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('phase', choices=sorted(PLAN))
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    config = {}
    try:
        from s6_common import load_config
        config = load_config(args.config)
        result = Result('at046-' + args.phase, ['AT-046'], config, dry_run=args.dry_run)
        if args.dry_run:
            result.step('plan', 'the entries this phase calls', {'calls': PLAN[args.phase]}, True)
            return result.write(out)
        owner = None
        if ports is None:
            password = (stdin or sys.stdin).readline().rstrip('\n') if args.phase == 'prepare' and (config.get('owner') or {}).get('origin') else None
            ports = ports_from(config, password)
            owner = ports.owner
        try:
            (prepare if args.phase == 'prepare' else run)(ports, config, result)
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
