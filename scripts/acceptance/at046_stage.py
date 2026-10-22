#!/usr/bin/env python3
"""AT-046 on stage (NX-041 D10 / NX-042 T7 end to end; owner approval: enable the simulation process group after release 27): a
simulation world, its synthetic data, the consumer twin and one simulation Run, and nothing real changes. Same configuration and result
formats as the S6 stage scripts (scripts/acceptance/s6_common.py; result schema nexloop-s6-stage-result/1, check `at046-<phase>`).

Two phases, because the world's credentials, DSN files and the compose project (NEXLOOP_SIM_WORLD) come in between:

    at046_stage.py prepare --config PRIVATE.json [--dry-run]     (owner password from stdin)
    at046_stage.py publish-twin --config PRIVATE.json             only the synthetic twin (inputs.publish_twin), no world created
    at046_stage.py cleanup --config PRIVATE.json                  after the acceptance: retire the synthetic twin (inputs.twin)
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
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
from s6_common import NONCE, Blocked, Redactor, Refused, Result, connect_from, load_chain, new_nonce, read_private  # noqa: E402


def s6_nonce_ok(nonce):
    return isinstance(nonce, str) and bool(NONCE.fullmatch(nonce))

SCHEMAS = ('authz', 'control', 'runtime', 'ontology')
TWIN_EXAMPLE = Path(__file__).resolve().parents[2] / 'packages/contracts/examples/consumer-twin.valid.json'
SYNTHETIC_TWIN = 'at046-synthetic-'


class Ports:
    """owner (workbench actions), probe (read only), twin (TwinPort or None), intake (a callable running the intake's three steps),
    negatives (a callable running AT-046's two refusal probes with the world's own api credential, or None)."""

    def __init__(self, *, owner=None, probe=None, twin=None, intake=None, negatives=None):
        self.owner, self.probe, self.twin, self.intake, self.negatives = owner, probe, twin, intake, negatives

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


# Periodic system bookkeeping of the real side, written on a timer whether AT-046 runs or not (stage finding 2026-10-22: the first
# stage run reported these two as changed while nothing of the Run touched them). They are left out of the comparison, by name and
# with the reason, and the result lists them; anything else that changes still fails the step.
EXCLUDED = {
    'control.nexloop_alert_state': 'the alert evaluator rewrites each rule\'s state every evaluation tick (NX-030), whether AT-046 runs or not',
    'runtime.nexloop_orphan_sweeps': 'the artifact orphan sweeper records each periodic sweep (0018), whether AT-046 runs or not',
    # X7 third round S8 (release 38): the two timers below wrote these in the window while nothing of the experiment touched them.
    'control.nexloop_alert_events': 'the alert evaluator appends a fired or resolved rule event each tick (NX-030), whether an experiment runs or not',
    'runtime.nexloop_retention_sweeps': 'the retention keeper records each class sweep it ran (0129/0140), whether an experiment runs or not',
}
# The owner's own governance Actions of AT-046 and X7 (strategies, experiments, reports, the shadow copy switch, simulation worlds and
# twins) leave a governed Action claim in world real under the owner's human principal. Those claims are the acceptance's own steps,
# not an effect of the simulation on the real side, so they are left out of the claim table's digest - only them: every other claim
# (any service or Run principal, any other Action, the same Actions by a non-member principal) is still compared.
OWNER_GOVERNANCE_ACTIONS = ('nexloop.strategy.publish', 'nexloop.strategy.retire', 'nexloop.experiment.register', 'nexloop.experiment.start',
                            'nexloop.experiment.stop', 'nexloop.experiment.report', 'nexloop.shadow_copy.set', 'nexloop.sim_world.create',
                            'nexloop.sim_world.delete', 'nexloop.sim_world.publish_catalog', 'nexloop.sim_world.generate', 'nexloop.sim_clock.control',
                            'nexloop.consumer_twin.publish',
                            'nexloop.consumer_twin.retire', 'nexloop.consumer_twin.calibrate', 'nexloop.consumer_twin.erase_text')
FILTERED = {
    'runtime.nexloop_action_claims': (
        "not (x.action_name = any(%s) and exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=x.tenant_id "
        "and m.principal_id=x.principal_id))", (list(OWNER_GOVERNANCE_ACTIONS),),
        'claims of the owner\'s own governance Actions (OWNER_GOVERNANCE_ACTIONS) by a workbench member are the acceptance\'s steps; every other claim is compared'),
}


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
                     + (' and tenant_id=%s' if tenanted else '') + (f' and {FILTERED[table][0]}' if table in FILTERED else ''))
        params += ([tenant] if tenanted else []) + (list(FILTERED[table][1]) if table in FILTERED else [])
    # L6 review B1: two tables without a world column - the real service credentials and the tenant's authority facts. The tenant row
    # (authority_revision) is left out: a credential revocation is expected to advance it.
    for table, where in (('authz.nexloop_service_credentials', "'real' = any(x.worlds) and x.tenant_id=%s"), ('authz.nexloop_authority_facts', 'x.tenant_id=%s')):
        parts.append(f"select '{table}', md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {table} x where {where}")
        params.append(tenant)
    out = dict(ports.read(' union all '.join(parts), tuple(params)))
    return {t: d for t, d in out.items() if t not in EXCLUDED}


def prepare(ports, config, r):
    inputs = config.get('inputs') or {}
    if 'publish_twin' in inputs:
        twin_definition(inputs)  # a bad publish_twin is refused before anything is created (no empty world)
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
    # Optional (stage finding 2026-10-22: the twin step blocked - no published twin): inputs.publish_twin publishes the synthetic consumer
    # twin of the contract example (generator twin-template/1: no model, D6) through the owner's governed Action publish_consumer_twin;
    # its twin_id and version go into the run phase's inputs.twin. A twin is the tenant's (not the world's); publishing is idempotent
    # per request id, so a rerun of prepare publishes no second version.
    if 'publish_twin' in inputs:  # the key enables it ({} and true included: dispatcher 2026-10-22)
        publish_twin(ports, config, r, key)


def twin_definition(inputs):
    """The synthetic twin's definition: the contract example, its twin_id with the prefix (inputs.publish_twin: true, {} or {"twin_id"})."""
    twin = inputs.get('publish_twin')
    if twin is not True and not isinstance(twin, dict):
        raise Refused('inputs.publish_twin: true, {} or {"twin_id": "at046-synthetic-<name>"}')
    definition = {k: v for k, v in json.loads(TWIN_EXAMPLE.read_text()).items() if k != 'frozen'}
    # L6 batch-12 review 4.1: the twin is the tenant's, so it carries an unmistakable prefix and is retired after the acceptance
    # (cleanup). Real experiments never pick a twin themselves: a registration names one, and a non-online stage needs it calibrated.
    definition['twin_id'] = twin.get('twin_id') if isinstance(twin, dict) and twin.get('twin_id') else SYNTHETIC_TWIN + 'trial'
    if not isinstance(definition['twin_id'], str) or not definition['twin_id'].startswith(SYNTHETIC_TWIN):
        raise Refused(f'inputs.publish_twin.twin_id: {SYNTHETIC_TWIN}<name> (the synthetic twin of the acceptance)')
    return definition


def publish_twin(ports, config, r, prior=None):
    """The owner publishes the synthetic consumer twin (also alone: the publish-twin phase - no world is created; a twin is the
    tenant's). Stage finding 2026-10-22: the workbench entry refused the body with 422 - its form fields are exactly definition and
    calibration_report_ref (the key set must match: workbench_actions_http.OPERATIONS), and the body named definition only."""
    definition = twin_definition(config.get('inputs') or {})
    # The Idempotency-Key is the governed request id (workbench_actions_http): one per definition, so a rerun replays.
    request = 'at046-twin-' + hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:32]
    status, out = ports.act('publish_consumer_twin', twin_publish_body(definition), request)
    out = out if isinstance(out, dict) else {}
    if status == 200 and out.get('replayed') is True:
        # A rerun with the same definition replays the first publish (the workbench answers outcome_id and replayed only): the same
        # twin and version as then - its version is in the first publish's result (the workbench has no twin read).
        r.step('twin_published', 'a published consumer twin without a model (replayed: twin_id and version as in the first publish result)',
               {'status': status, 'twin_id': definition['twin_id'], 'version': None, 'twin_status': 'published', 'replayed': True}, True)
        return
    r.step('twin_published', 'a published consumer twin without a model (inputs.twin of the run phase: twin_id, version)',
           {'status': status, 'twin_id': out.get('twin_id'), 'version': out.get('version'), 'twin_status': out.get('status')},
           status == 200 and out.get('status') == 'published')


def twin_publish_body(definition):
    return {'definition': definition, 'calibration_report_ref': None}  # every field of the workbench form, none extra

def cleanup(ports, config, r, prior=None):
    """After the acceptance (L6 batch-12 review 4.1): retire the synthetic twin prepare published (inputs.twin: twin_id, version), through
    the owner's governed Action retire_consumer_twin. Only a twin with the synthetic prefix is ever retired here; a retired twin binds no
    more and no experiment can use it at a non-online stage. Idempotent per twin version."""
    twin = (config.get('inputs') or {}).get('twin') or {}
    twin_id, version = twin.get('twin_id'), twin.get('version')
    if not isinstance(twin_id, str) or not twin_id.startswith(SYNTHETIC_TWIN) or type(version) is not int:
        raise Refused(f'inputs.twin: the synthetic twin of the acceptance ({SYNTHETIC_TWIN}<name>, its version)')
    status, out = ports.act('retire_consumer_twin', {'twin_id': twin_id, 'version': version, 'reason': 'AT-046 stage acceptance finished: synthetic twin retired'},
                            'at046-twin-retire-' + hashlib.sha256(f'{twin_id}/{version}'.encode()).hexdigest()[:32])
    r.step('synthetic_twin_retired', 'the acceptance\'s synthetic twin retired (no further binding)',
           {'status': status, 'twin_id': twin_id, 'version': version, 'twin_status': out.get('status') if isinstance(out, dict) else None},
           status == 200 and isinstance(out, dict) and out.get('status') == 'retired')


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
    r.nonce = r.nonce or new_nonce()  # the run nonce (s6_common): probe-before draws it, the later parts carry it
    chain(r, config)


def intake_phase(ports, config, r, prior):
    """APP_HOST, the simulation one-off container (sim_api and twin_port over hostssl): the twin through its port, then the Run accepted."""
    world = _world(config)
    inputs = config.get('inputs') or {}
    chain(r, config)
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
    # AT-046's core (stage judgement 2026-10-22): the server refuses the simulation group a real message Action, even with a forged mode.
    negatives(ports, r, world, consumers)

def refusal(error):
    """(refused by the server, SQLSTATE, the server's fixed refusal text) of an exception chain - a database refusal only; the text is
    the server's own message (no value of the request), cut to 120 characters."""
    import psycopg
    seen = 0
    while error is not None and seen < 8:
        if isinstance(error, psycopg.Error):
            state = getattr(error, 'sqlstate', None)
            return state == '42501', state, (getattr(error.diag, 'message_primary', None) or '')[:120]
        error, seen = error.__cause__ or error.__context__, seen + 1
    return False, None, None


def negatives(ports, r, world, consumers):
    """N1 and N2 (AT-046: "Twin calls a real message Action -> the server refuses, even if the model forges mode").
    Safety (stage: the real dispatch switches are open): neither probe can reach an outbound path even if it were let through. N1 only
    asks the server for a real-world session for the world's api credential (the step every real Action, message Actions included,
    starts with) and, when the twin port is configured, binds the twin in world real; neither submits an effect. N2 presents a Run
    command naming mode 'real' in the simulation world (the client's own check bypassed for this one call, as a forging model would):
    were it accepted, the task stays in the simulation world, whose effects only reach stand-ins. probe-after then shows the real side
    unchanged, so refusal and absence of effect are proved together."""
    if ports.negatives is None:
        r.block('real_action_refused', 'roles.sim_api not configured')
        r.block('forged_mode_refused', 'roles.sim_api not configured')
        return
    from nexloop_eios.effect_contexts import EFFECT
    out = ports.negatives(world, consumers[0] if consumers else None, 'eios:action:' + EFFECT + ':1', ports.twin)
    n1, n2 = out['n1'], out['n2']
    r.step('real_action_refused', 'the world\'s own credential asks for a real session (any real message Action starts there; twin: bind in real): '
           'refused by the server (42501)', n1, all(p['refused'] for p in n1.values()) and bool(n1))
    r.step('forged_mode_refused', 'a Run command forging mode real in the simulation world, the client check bypassed: refused by the server (42501); '
           'the identical command with mode simulation accepted (the control: the mode is the one difference)',
           n2, n2.get('refused') is True and n2.get('sqlstate') == '42501' and (n2.get('control') or {}).get('accepted') is True)


def probe_after(ports, config, r, prior):
    """DATA_HOST again: the task until it ends, the Run credential's world, the world's credentials, the real digest unchanged."""
    world, tenant = _world(config), config['tenant_id']
    inputs = config.get('inputs') or {}
    chain(r, config)
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
    r.step('real_side_unchanged', 'every world-real row of the tenant unchanged (digests of all tables with a world column, the periodic system '
           'bookkeeping excluded by name)', {'tables': len(before), 'changed': changed, 'excluded': EXCLUDED,
                                              'filtered': {t: f[2] for t, f in FILTERED.items()}}, not changed and bool(before))


def run(ports, config, r, prior=None):
    """The three parts in one process: only where one host reaches both the probe and the intake (local tests). On stage they run apart
    (probe-before and probe-after on DATA_HOST, intake on APP_HOST) and merge combines them (L6 sim-issue-install review 1.1)."""
    for part in (probe_before, intake_phase, probe_after):
        part(ports, config, r, r.steps)


def _tenant_ref(config):
    import hashlib
    return hashlib.sha256(('at046:' + config['tenant_id']).encode()).hexdigest()[:16]  # the tenant, never named in a result


def chain(r, config):
    """L6 at046-split review 1.1: one run of the three parts. The run nonce is the result's run_nonce (s6_common Result/load_chain);
    a part's own chain step records the world and the tenant reference, which load_chain does not check."""
    if not r.nonce:
        raise Refused('--from: no run nonce (a probe-before result of this script)')
    if not any(st.get('name') == 'run_chain' for st in r.steps):
        r.step('run_chain', 'one run of the three parts', {'part': r.check.removeprefix('at046-')}, True,
               world=_world(config), tenant_ref=_tenant_ref(config))


def evidence(prior, name):
    found = [s for s in (prior or []) if s.get('name') == name]
    return (found[-1].get('evidence') or {}) if found else {}


def passed(prior, name):
    return any(s.get('name') == name and s.get('passed') is True for s in (prior or []))


def load_prior(paths, config, parts):
    """The earlier parts' result documents (--from), checked by s6_common.load_chain (L3 fca51f2d: this script's results, never a dry
    run, exactly these parts, one run_nonce, executed in chain order) and here for what load_chain leaves to its caller (L2 note
    l2-at046-nonce-vs-load_chain.md): each --from at its own position (load_chain takes them in any order), strictly increasing times
    (load_chain allows equal ones), this world and tenant (run_chain). Returns (steps, blocked, problems, nonce): the merge records the
    problems as a failed step, the other parts refuse them."""
    problems, docs = [], []
    order = ['at046-' + p for p in parts]
    try:
        steps, blocked, nonce = load_chain(paths, prefix='at046-', order=order)
    except Refused as error:
        steps, blocked, nonce = [], [], None
        problems.append(str(error))
    for path in paths or []:
        try:
            docs.append(json.loads(Path(path).read_text().strip().splitlines()[-1]))
        except (OSError, ValueError, IndexError):
            raise Refused('--from: a result document of this script') from None
    if [d.get('check') for d in docs] != order:
        problems.append('--from: the results in the order ' + ', '.join(order))
    times = [str(d.get('executed_at') or '') for d in docs]
    if any(not t for t in times) or times != sorted(times) or len(set(times)) != len(times):
        problems.append('the results are not in the order probe-before < intake < probe-after (strictly)')
    world, ref = _world(config), _tenant_ref(config)
    for i, doc in enumerate(docs):
        link = next((st.get('evidence') or {} for st in doc.get('steps') or [] if st.get('name') == 'run_chain'), {})
        if link.get('world') != world or link.get('tenant_ref') != ref:
            problems.append(f'--from #{i + 1}: another world or tenant')
    if problems:  # the steps of a broken chain are still shown in the merge, in the order given
        steps, blocked = [], []
        for doc in docs:
            steps += doc.get('steps') or []
            blocked += doc.get('blocked') or []
    return steps, blocked, problems, nonce


REQUIRED = ('synthetic_consumers_present', 'run_action_published_for_the_world', 'real_digest_before', 'twin_observe_act',
            'real_action_refused', 'forged_mode_refused',  # AT-046's core (stage judgement 2026-10-22)
            'world_credentials_only_of_the_world', 'real_side_unchanged')


def negatives_from(roles):
    """AT-046's refusal probes with the world's api credential (sim_api): N1 a real session, N2 a forged mode (see negatives())."""
    s = roles.get('sim_api') or {}
    if not all(s.get(k) for k in ('dsn_file', 'token_file', 'signing_key_file', 'signing_key_id')):
        return None

    def run(world, consumer, effect, twin):
        import tempfile
        from contextlib import contextmanager
        from nexloop_eios import runtime_activation
        from nexloop_eios.backend import open_backend
        from nexloop_eios.sim_context import sim_run_input
        token = read_private(s['token_file'], 4096).strip()

        @contextmanager
        def client_check_bypassed():
            # The forging model's view: the Python client's own mode check is skipped for this one call; the server must refuse.
            original = runtime_activation.world_kind
            runtime_activation.world_kind = lambda w: 'real' if str(w).startswith(('sim:', 'shadow:')) else original(w)
            try:
                yield
            finally:
                runtime_activation.world_kind = original
        n1, n2 = {}, {}
        with tempfile.TemporaryDirectory(prefix='at046-neg-') as artifacts, open_backend(
                database_url=read_private(s['dsn_file'], 8192).strip(), artifact_root=artifacts, signing_key_file=s['signing_key_file'],
                signing_key_id=s['signing_key_id']) as api:
            try:
                api.authenticate(token, world='real')
                n1['real_session'] = {'refused': False, 'sqlstate': None, 'reason': 'allowed'}
            except Exception as error:  # noqa: BLE001 - classified below
                refused, state, reason = refusal(error)
                n1['real_session'] = {'refused': refused, 'sqlstate': state, 'reason': reason}
            if twin is not None:
                try:  # the port's server entry with world real (the client refuses real before any call: called directly here)
                    twin._one('select authz.nexloop_twin_bind(%s,%s,%s,%s,%s)', (twin._digest, 'real', 'at046-synthetic-probe', 1, 'probe'))
                    n1['twin_bind_real'] = {'refused': False, 'sqlstate': None, 'reason': 'allowed'}
                except Exception as error:  # noqa: BLE001
                    refused, state, reason = refusal(error)
                    n1['twin_bind_real'] = {'refused': refused, 'sqlstate': state, 'reason': reason}
            source = api.authenticate(token, world=world)
            run = source.issue_run_credential(action_resources=[effect])
            pack = sim_run_input(source, run.run_id, consumer, 8)
            command = {'schema_version': '1.0', 'run_id': run.run_id, 'tenant_id': json.loads(pack)['tenant_id'], 'world_id': world, 'mode': 'real',
                       'request_id': 'at046-forged-' + uuid.uuid4().hex, 'trigger_event_id': str(uuid.uuid4()), 'role_ref': 'role:sim-consumer-reply',
                       'consumer_ref': 'consumer:' + consumer, 'goal_version_ref': 'goal:sim-at046', 'context_manifest_ref': 'artifact:sim-context',
                       'runtime_profile': 'deterministic-test', 'credential_ref': 'run_credential:' + run.run_id,
                       'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0', 'currency': 'CNY'},
                       'not_after': run.expires_at.isoformat(), 'runtime_owner_epoch': 1}
            try:
                with client_check_bypassed():
                    source.accept_runtime_event(queue='operations', source_id='at046-forged', event_id='at046-forged-' + run.run_id, run_token=run.token,
                                                command=command, input=pack)
                n2 = {'refused': False, 'sqlstate': None, 'reason': 'allowed'}
            except Exception as error:  # noqa: BLE001
                refused, state, reason = refusal(error)
                n2 = {'refused': refused, 'sqlstate': state, 'reason': reason}
            # Control (L6 review 3.1): the server's command check folds many conditions into one refusal, so the same command with only
            # its mode set right must be accepted - the mode is then the one difference. The forged one went first and stored nothing,
            # so this is the Run's first registration. The accepted control is an ordinary simulation Run of this world: the simulation
            # worker runs it like the main one (deterministic profile, stand-ins only) and it ends by itself; its task id is recorded.
            try:
                accepted = source.accept_runtime_event(queue='operations', source_id='at046-forged', event_id='at046-control-' + run.run_id,
                                                       run_token=run.token, command={**command, 'mode': 'simulation'}, input=pack)
                n2['control'] = {'accepted': bool(accepted.get('accepted')), 'task_id': accepted.get('task_id')}
            except Exception as error:  # noqa: BLE001
                refused, state, reason = refusal(error)
                n2['control'] = {'accepted': False, 'sqlstate': state, 'reason': reason}
        return {'n1': n1, 'n2': n2}
    return run


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
    return Ports(owner=owner, probe=connect_from(probe) if probe else None, twin=twin, intake=intake_from(roles), negatives=negatives_from(roles))


PHASES = {'probe-before': probe_before, 'intake': intake_phase, 'probe-after': probe_after, 'run': run}
NEEDS = {'intake': ('probe-before',), 'probe-after': ('probe-before', 'intake'), 'merge': ('probe-before', 'intake', 'probe-after')}
PLAN = {'probe-before': ['probe (read only, DATA_HOST socket): world-real row digests, synthetic Consumers, the Run Action in the world catalog'],
        'intake': ['twin port (APP_HOST hostssl): bind, observe, act (when configured)', 'sim_api (APP_HOST hostssl): issue_run_credential, sim context register, accept'],
        'probe-after': ['probe (read only, DATA_HOST socket): task status until it ends, the Run credential world, the world credentials, digests again'],
        'merge': ['no connection: the three result documents (--from) combined into at046-run'],
        'cleanup': ['owner workbench: retire_consumer_twin (the synthetic twin of inputs.twin)'],
        'publish-twin': ['owner workbench: publish_consumer_twin (the synthetic twin only; no world is created)'],
        'prepare': ['owner workbench: create_sim_world', 'owner workbench: generate_sim_world_data', 'owner workbench: publish_sim_catalog',
                    'owner workbench: publish_consumer_twin (only with inputs.publish_twin)'],
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
            prior, prior_blocked, problems, result.nonce = load_prior(args.prior, config, NEEDS[args.phase])
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
            password = (stdin or sys.stdin).readline().rstrip('\n') if args.phase in ('prepare', 'cleanup', 'publish-twin') and (config.get('owner') or {}).get('origin') else None
            ports = ports_from(config, password)
            owner = ports.owner
        try:
            if args.phase == 'prepare':
                prepare(ports, config, result)
            elif args.phase == 'cleanup':
                cleanup(ports, config, result)
            elif args.phase == 'publish-twin':
                publish_twin(ports, config, result)
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
