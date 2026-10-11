"""NX-037 A-class prep: independent initialization of a real tenant (11_DEPLOYMENT §9 D4; NX-037 design §2–§3, ROADMAP D-g).

    python scripts/nx037/real_tenant_init.py plan    --tenant UUID --owner-principal ID --value-catalog FILE --channel-ref REF \\
        [--cumulative 100 --per-run 2 --monthly 300] --period-start ISO --period-end ISO          # default: dry-run, prints the steps
    python scripts/nx037/real_tenant_init.py execute ... --configurator-dsn-file F --signing-key-file F --signing-key-id ID \\
        --service-secrets-file F --credential-expires-at ISO                                       # trusted configuration steps only
    python scripts/nx037/real_tenant_init.py verify  --tenant UUID --metrics-dsn-file F ...        # the post-initialization check

The real tenant is a new tenant id, never the synthetic demo tenant, and is initialized only through:
- trusted configuration (the technical configurator role): service principals and grants (the 0050 path also creates the tenant),
  action effect categories v2 for the Actions the tenant has published (any other stays undeclared: customer contact and
  external, fail closed), alert rules;
- the owner's governed Action `Budget.set` (owner change set_budget) for the D-g cumulative cap as the model budget of the trial
  period: it needs the owner's own workbench session, so this script prints the request and never performs it;
- references only for the value catalog (file sha256) and the channel configuration (an opaque reference): the channel itself
  is connected in NX-037 B-class work, not here.
plan and verify never write; execute performs only the trusted configuration steps above (no direct table write). None of
the modes opens a real dispatch switch or reads a channel secret.

`verify` (read-only, metrics role, authz.nexloop_nx037_tenant_state of 0161) passes only when: the tenant is active; the three
real dispatch layers are closed (this process's REAL_DISPATCH_ENABLED is not 'true', no tenant switch open, no Action switch
open); the model budget equals the D-g cumulative cap in the trial currency and covers the trial period; the alert rules of the
configured version are in effect (the count of effect category declarations is reported: an undeclared Action is external
and closed, so it is not a readiness condition). The per-Run and monthly caps have no budget table in the
database (one model budget per tenant and world): the controlled interaction executor enforces them (stop / pause rules).
Output is JSON of ids, digests, numbers, codes and times; exit 0 ready / 1 not ready / 2 refused input.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
CONFIG = ROOT / 'deploy/configuration'
TRIAL = CONFIG / 'nx037-trial.v1.json'
GRANTS = ROOT / 'deploy/authorization/service-grants.v1.json'
OWNER_RESTRICTIONS = ROOT / 'deploy/authorization/owner-property-restrictions.json'
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class Refused(ValueError):
    pass


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise Refused('time zone required')
    return parsed.astimezone(UTC)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parameters(*, tenant, owner_principal, value_catalog, channel_ref, period_start, period_end, cumulative=None, per_run=None,
               monthly=None, trial=TRIAL):
    from nexloop_eios.controlled_interactions import load_plan
    if not _UUID.fullmatch(tenant or ''):
        raise Refused('tenant must be a lowercase uuid')
    from nexloop_eios.sandbox_profile import SANDBOX_TENANT
    if tenant == SANDBOX_TENANT:
        raise Refused('a real tenant is not the synthetic demo tenant')
    if not _REF.fullmatch(owner_principal or '') or not _REF.fullmatch(channel_ref or ''):
        raise Refused('owner principal and channel reference must be opaque references')
    caps = {k: v for k, v in (('cumulative', cumulative), ('per_run', per_run), ('tenant_monthly', monthly)) if v is not None}
    plan = load_plan(trial, cost_caps=caps)
    start, end = _time(period_start), _time(period_end)
    if not start < end:
        raise Refused('trial period')
    return {'tenant': tenant, 'owner_principal': owner_principal, 'value_catalog_sha256': _sha256(value_catalog),
            'channel_ref': channel_ref, 'period_start': start, 'period_end': end, 'plan': plan,
            'alert_rules': json.loads((CONFIG / json.loads(trial.read_text())['alert_rules']).read_text()),
            'effect_categories': CONFIG / json.loads(trial.read_text())['effect_categories']}


def budget_request(p):
    """The owner's governed Budget.set (GoalGovernedActions.set_budget) the owner submits from the workbench. The request id is
    a stable business-intent id: resubmitting it is the same change."""
    request_id = 'nx037-budget-' + str(uuid.uuid5(uuid.NAMESPACE_URL, f'nexloop:nx037:{p["tenant"]}:{p["period_start"].isoformat()}'))
    return {'action_name': 'Budget.set', 'action_version': 1, 'request_id': request_id, 'budget_kind': 'model',
            'unit': p['plan']['currency'], 'limit_amount': str(p['plan']['cost_caps']['cumulative']),
            'period_start': p['period_start'].isoformat(), 'period_end': p['period_end'].isoformat()}


def steps(p):
    return [
        {'step': 'service_grants', 'kind': 'trusted_configuration', 'manifest_sha256': _sha256(GRANTS),
         'note_code': 'creates_tenant_and_service_principals'},
        {'step': 'effect_categories', 'kind': 'trusted_configuration', 'manifest_sha256': _sha256(p['effect_categories'])},
        {'step': 'alert_rules', 'kind': 'trusted_configuration', 'version': p['alert_rules']['version']},
        {'step': 'model_budget', 'kind': 'owner_action', 'request': budget_request(p)},
        {'step': 'value_catalog', 'kind': 'reference', 'sha256': p['value_catalog_sha256']},
        {'step': 'channel_configuration', 'kind': 'reference', 'ref': p['channel_ref']},
        {'step': 'verify', 'kind': 'check'},
    ]


def plan(p):
    return {'mode': 'dry_run', 'executed': False, 'tenant': p['tenant'], 'owner_principal': p['owner_principal'],
            'cost_caps': {k: str(v) for k, v in p['plan']['cost_caps'].items()}, 'currency': p['plan']['currency'], 'steps': steps(p)}


def execute(p, *, configurator_dsn_file, signing_key_file, signing_key_id, service_secrets_file, credential_expires_at,
            owner_actions=None):
    """Runs the trusted configuration steps (configurator role through the configuration functions; idempotent). The budget
    step is performed only through `owner_actions` (the owner's own governed session, GoalGovernedActions); without it the
    step stays pending and is reported with its request."""
    from nexloop_eios import effect_categories, observability
    from nexloop_eios import service_grants as G
    done = {}
    report = G.apply(G.load(GRANTS), p['tenant'], database_url_file=configurator_dsn_file, signing_key_file=signing_key_file,
                     signing_key_id=signing_key_id, service_secrets_file=service_secrets_file,
                     credential_expires_at=_time(credential_expires_at), owner_restrictions=G.load_owner_restrictions(OWNER_RESTRICTIONS))
    done['service_grants'] = {'changed': bool(report.get('changed')), 'grants_added': len(report.get('grants_added', []))}
    # Only Actions the tenant has published can be declared (the declaration is frozen by the published definition's digest).
    # An Action published later stays undeclared until this step is run again: undeclared is customer contact and external
    # (fail closed), so deferring never opens anything.
    manifest = effect_categories.load(p['effect_categories'])
    published = {(a['stable_name'], a['version']) for a in G.action_lineage(configurator_dsn_file, p['tenant']) if a['world'] == 'real' and a['active']}
    declared = [a for a in manifest['actions'] if (a['stable_name'], a['version']) in published]
    done['effect_categories'] = {'declared': len(declared), 'deferred': len(manifest['actions']) - len(declared)}
    if declared:
        categories = effect_categories.apply({**manifest, 'actions': declared}, p['tenant'], database_url_file=configurator_dsn_file)
        done['effect_categories'].update({k: v for k, v in categories.items() if type(v) in (int, bool)})
    rules = observability.apply_rules(p['alert_rules'], p['tenant'], database_url_file=configurator_dsn_file)
    done['alert_rules'] = {k: v for k, v in rules.items() if type(v) in (int, bool)}
    request = budget_request(p)
    if owner_actions is None:
        done['model_budget'] = {'status': 'owner_pending', 'request_id': request['request_id']}
    else:
        owner_actions.set_budget(**{**request, 'period_start': p['period_start'], 'period_end': p['period_end']})
        done['model_budget'] = {'status': 'submitted', 'request_id': request['request_id']}
    return {'mode': 'execute', 'executed': True, 'tenant': p['tenant'], 'steps': done}


def verify(p, connection, *, environ=None):
    from nexloop_eios.real_dispatch import deployment_open
    state = connection.execute("select authz.nexloop_nx037_tenant_state(%s,'real')", (p['tenant'],)).fetchone()[0]
    model = state['budgets'].get('model')
    rules = state.get('alert_rules') or {}
    checks = {
        'tenant_active': state['tenant_status'] == 'active',
        'deployment_switch_closed': not deployment_open(environ),
        'tenant_switch_closed': state['tenant_switch_open'] is False,
        'action_switches_closed': state['open_action_switches'] == 0,
        'model_budget_is_cumulative_cap': bool(model) and Decimal(model['limit']) == p['plan']['cost_caps']['cumulative']
            and model['unit'] == p['plan']['currency'],
        'model_budget_covers_trial': bool(model) and _time(model['period_start']) <= p['period_start'] and _time(model['period_end']) >= p['period_end'],
        'alert_rules_in_effect': rules.get('version') == p['alert_rules']['version'],
    }
    return {'mode': 'verify', 'ready': all(checks.values()), 'tenant': p['tenant'], 'checks': checks,
            'enforced_by_executor': {'per_run': str(p['plan']['cost_caps']['per_run']), 'tenant_monthly': str(p['plan']['cost_caps']['tenant_monthly'])},
            'alert_rules_digest': rules.get('digest'), 'effect_category_declarations': state['effect_categories'], 'budget_revision': (model or {}).get('revision')}


def _connect(dsn_file):
    import psycopg
    return psycopg.connect(Path(dsn_file).read_text(encoding='utf-8').strip(), autocommit=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='NX-037 real tenant initialization (dry-run by default)')
    parser.add_argument('mode', nargs='?', choices=('plan', 'execute', 'verify'), default='plan')
    for name in ('--tenant', '--owner-principal', '--value-catalog', '--channel-ref', '--period-start', '--period-end'):
        parser.add_argument(name, required=True)
    for name in ('--cumulative', '--per-run', '--monthly', '--configurator-dsn-file', '--signing-key-file', '--signing-key-id',
                 '--service-secrets-file', '--credential-expires-at', '--metrics-dsn-file'):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    try:
        p = parameters(tenant=args.tenant, owner_principal=args.owner_principal, value_catalog=args.value_catalog, channel_ref=args.channel_ref,
                       period_start=args.period_start, period_end=args.period_end, cumulative=args.cumulative, per_run=args.per_run, monthly=args.monthly)
        if args.mode == 'plan':
            out, code = plan(p), 0
        elif args.mode == 'execute':
            needed = ('configurator_dsn_file', 'signing_key_file', 'signing_key_id', 'service_secrets_file', 'credential_expires_at')
            if any(getattr(args, n) is None for n in needed):
                raise Refused('execute needs the trusted configuration files')
            out = execute(p, **{n: getattr(args, n) for n in needed})
            code = 0
        else:
            if args.metrics_dsn_file is None:
                raise Refused('verify needs the metrics role connection file')
            with _connect(args.metrics_dsn_file) as connection:
                out = verify(p, connection, environ=os.environ)
            code = 0 if out['ready'] else 1
    except Exception as error:  # noqa: BLE001 - fixed text only: a database error may carry connection details
        print(json.dumps({'ok': False, 'detail': str(error) if isinstance(error, Refused) else type(error).__name__}))
        return 2
    print(json.dumps(out, default=str, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
