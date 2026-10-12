"""NX-037: the run entry of the controlled interactions (NX-037 design §3 step 4, ROADMAP D-i).

    python scripts/nx037/run_interactions.py --driver synthetic --trial-id UUID --out DIR [--plan-file F] [--inject SEQ=FAULT]
    python scripts/nx037/run_interactions.py --driver real --trial-id UUID --out DIR --plan-file F \\
        --tenant UUID --owner-principal REF --value-catalog F --channel-ref REF --period-start ISO --period-end ISO \\
        --metrics-dsn-file F --owner-session-file F
    ... --resume OWNER-CONFIRMATION-REF        # continue a paused trial (recorded in DIR/resumes.jsonl)

Reads the trial configuration (deploy/configuration/nx037-trial.v1.json: D-g caps, mix, rules) and the owner-editable
interaction plan (deploy/configuration/nx037-interaction-plan.v1.json: the order of the 30 interactions, which must hold the
mix, and for a real trial the real dispatch switch state the owner opened). Each interaction is driven through the driver;
after each, the executor collects its evidence and applies the stop and pause rules (nexloop_eios.controlled_interactions).

- synthetic: the deterministic provider and loopback channel (nexloop_eios.interaction_drivers), synthetic tenants.
- real: refused unless, before anything runs, (1) the real tenant check of scripts/nx037/real_tenant_init.py passes on every
  item except the switch items (active tenant, D-g budget over the trial period, alert rules in effect), and (2) the three
  switch layers are exactly the plan's real_dispatch (this process's REAL_DISPATCH_ENABLED; the tenant switch and its data
  mode, the open Action switches via authz.nexloop_nx037_open_switches of the metrics role). With them met, the real driver
  (nexloop_eios.interaction_drivers.RealDriver) prompts the operator, a person of the owner's staff playing the customer in
  the WebChat, for each interaction (stderr and prompts.jsonl in DIR), and watches through the metrics role for the Run and
  its effects (0380); it sends nothing itself. An interaction not completed within --interaction-timeout (default 900 s) is
  a pause. A real trial pauses after the calibration interactions (3) for the owner to confirm the cost per interaction.

A stopped trial never resumes (start a new trial id). A paused trial continues only with --resume and the owner's confirmation
reference. Output: one JSON line of ids, codes, numbers and times. Exit: 0 completed; 1 paused; 2 stopped; 3 refused (input or
real mode preconditions).
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
TRIAL = ROOT / 'deploy/configuration/nx037-trial.v1.json'
PLAN = ROOT / 'deploy/configuration/nx037-interaction-plan.v1.json'
PLAN_SCHEMA = 'nexloop.nx037-interaction-plan.v1'
SWITCH_CHECKS = ('deployment_switch_closed', 'tenant_switch_closed', 'action_switches_closed')
# Synthetic tenants (tests/support/tenants.TENANT_A and TENANT_OTHER): the synthetic driver never runs against a real tenant.
SYNTHETIC_TENANT, SYNTHETIC_OTHER = '00000000-0000-4000-8000-00000000000a', '00000000-0000-4000-8000-00000000000c'


class Refused(ValueError):
    pass


def load_interaction_plan(path, trial_plan):
    from nexloop_eios.controlled_interactions import validate_order
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if type(value) is not dict or value.get('schema') != PLAN_SCHEMA:
        raise Refused('interaction plan schema')
    real = value.get('real_dispatch')
    if real is not None and (type(real) is not dict or set(real) != {'deployment', 'tenant', 'data_mode', 'actions'}
                             or type(real['deployment']) is not bool or type(real['tenant']) is not bool
                             or real['data_mode'] not in ('real', 'test', None) or type(real['actions']) is not list):
        raise Refused('interaction plan real_dispatch')
    return {'order': validate_order(trial_plan, value.get('order')), 'real_dispatch': real}


def switch_state(connection, tenant, environ):
    from nexloop_eios.real_dispatch import deployment_open
    switches = connection.execute("select authz.nexloop_nx037_open_switches(%s,'real')", (tenant,)).fetchone()[0]
    return {'deployment': deployment_open(environ), 'tenant': switches['tenant']['enabled'],
            'data_mode': switches['tenant']['data_mode'] if switches['tenant']['enabled'] else None, 'actions': sorted(switches['actions'])}


def real_preconditions(params, connection, expected, environ):
    """(ok, refusal codes). Every check of the real tenant verify except its switch items, then the switches against the plan."""
    import real_tenant_init as init
    if expected is None:
        return False, ['plan_has_no_real_dispatch']
    report = init.verify(params, connection, environ=environ)
    failed = [name for name, ok in report['checks'].items() if not ok and name not in SWITCH_CHECKS]
    actual = switch_state(connection, params['tenant'], environ)
    want = {**expected, 'actions': sorted(expected['actions']), 'data_mode': expected['data_mode'] if expected['tenant'] else None}
    failed += [f'switch_{layer}_differs' for layer in ('deployment', 'tenant', 'data_mode', 'actions') if actual[layer] != want[layer]]
    return not failed, failed


def _connect(dsn_file):
    import psycopg
    return psycopg.connect(Path(dsn_file).read_text(encoding='utf-8').strip(), autocommit=True)


def main(argv=None, *, environ=None, connect=_connect, operator=None):
    from nexloop_eios import controlled_interactions as X
    from nexloop_eios import interaction_drivers as D
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description='NX-037 controlled interactions run entry')
    parser.add_argument('--driver', choices=('synthetic', 'real'), required=True)
    parser.add_argument('--trial-id', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--trial-config', default=str(TRIAL))
    parser.add_argument('--plan-file', default=str(PLAN))
    parser.add_argument('--resume')
    parser.add_argument('--inject', action='append', default=[], metavar='SEQ=FAULT')
    parser.add_argument('--calibration-pause', action='store_true', help='synthetic: pause after the calibration interactions')
    parser.add_argument('--interaction-timeout', type=int, default=900, help='real: seconds the operator has per interaction')
    parser.add_argument('--poll-seconds', type=float, default=2.0, help='real: observation interval')
    for name in ('--tenant', '--owner-principal', '--value-catalog', '--channel-ref', '--period-start', '--period-end',
                 '--metrics-dsn-file', '--owner-session-file'):
        parser.add_argument(name)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 3

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, default=str))
        return code

    try:
        trial = X.load_plan(args.trial_config)
        plan = load_interaction_plan(args.plan_file, trial)
        if args.driver == 'synthetic':
            if any(getattr(args, n) for n in ('owner_session_file', 'metrics_dsn_file', 'channel_ref')):
                raise Refused('synthetic mode takes no real configuration')
            inject = {}
            for item in args.inject:
                seq, _, fault = item.partition('=')
                inject[int(seq)] = fault
            driver = D.SyntheticDriver(tenant=SYNTHETIC_TENANT, other_tenant=SYNTHETIC_OTHER, inject=inject)
            collector, calibration = D.SyntheticCollector(driver), args.calibration_pause
        else:
            import real_tenant_init as init
            if args.inject:
                raise Refused('no injection in real mode')
            needed = ('tenant', 'owner_principal', 'value_catalog', 'channel_ref', 'period_start', 'period_end', 'metrics_dsn_file', 'owner_session_file')
            if any(getattr(args, n) is None for n in needed):
                raise Refused('real mode needs the tenant, trial period, metrics connection, channel reference and owner session')
            params = init.parameters(tenant=args.tenant, owner_principal=args.owner_principal, value_catalog=args.value_catalog,
                                     channel_ref=args.channel_ref, period_start=args.period_start, period_end=args.period_end,
                                     trial=Path(args.trial_config))
            config = D.RealDriverConfig(channel_ref=args.channel_ref, owner_session_file=args.owner_session_file)
            with connect(args.metrics_dsn_file) as connection:
                ok, failed = real_preconditions(params, connection, plan['real_dispatch'], environ)
            if not ok:
                return emit({'ok': False, 'refused': failed, 'trial_id': args.trial_id}, 3)
            observer = D.PgObserver(connect, args.metrics_dsn_file, args.tenant)
            driver = D.RealDriver(config, observer=observer, operator=operator or D.ConsoleOperator(args.out),
                                  timeout_seconds=args.interaction_timeout, poll_seconds=args.poll_seconds)
            collector, calibration = None, True
    except (Refused, X.PlanInvalid, ValueError, OSError) as error:
        return emit({'ok': False, 'detail': str(error) if isinstance(error, (Refused, X.PlanInvalid)) else type(error).__name__}, 3)
    except Exception as error:  # noqa: BLE001 - fixed text: a database error may carry connection details
        return emit({'ok': False, 'detail': type(error).__name__}, 3)
    try:
        if args.driver == 'real':
            collector = _RealCollector(connect, args.metrics_dsn_file, args.tenant)
        state = X.run(trial, driver, collector, args.out, trial_id=args.trial_id, order=plan['order'], resume=args.resume,
                      calibration_pause=calibration)
    except X.TrialPaused as paused:
        return emit({'ok': False, 'detail': 'paused_needs_resume', 'rules': str(paused).split(','), 'trial_id': args.trial_id}, 3)
    except RuntimeError as error:
        return emit({'ok': False, 'detail': 'trial_stopped' if str(error) == 'trial stopped' else type(error).__name__, 'trial_id': args.trial_id}, 3)
    except (X.PlanInvalid, ValueError) as error:
        return emit({'ok': False, 'detail': str(error) if isinstance(error, X.PlanInvalid) else 'invalid_resume_or_trial_id'}, 3)
    except Exception as error:  # noqa: BLE001 - fixed text: a database error may carry connection details
        return emit({'ok': False, 'detail': type(error).__name__, 'trial_id': args.trial_id}, 3)
    return emit({'ok': state['status'] == 'completed', **state}, {'completed': 0, 'paused': 1, 'stopped': 2}[state['status']])


class _RealCollector:
    """PgCollector over a connection opened for each collection (metrics role)."""

    def __init__(self, connect, dsn_file, tenant):
        self.connect, self.dsn_file, self.tenant = connect, dsn_file, tenant

    def collect(self, run_id):
        from nexloop_eios.controlled_interactions import PgCollector
        with self.connect(self.dsn_file) as connection:
            return PgCollector(connection, self.tenant).collect(run_id)


if __name__ == '__main__':
    sys.exit(main())
