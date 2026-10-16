"""NX-032 G-GOAL: the owner approves a metric and publishes an NX-022 goal version through the governed owner Actions
(Metric.approve, Goal.publish), as itself, on the owner's own workbench session.

    python -m nexloop_eios.goal_setup --recipe F --database-url-file F --identity-database-url-file F --signing-key-file F
        --signing-key-id ID --artifact-root D --username-file F --password-file F --browser-rate-key-file F --browser-origin ORIGIN

A plan names the goal version it serves (goal:<goal_id>@<version>, nexloop_eios.plan_setup); only the owner may publish one (the
owner-only workbench grants, workbench roles). The login is the workbench's own (context_strategy_setup.owner_session: username and
password from private files, never arguments, never printed; the browser login rate limit), the Actions run on the owner's browser
business authority (0045), and the session is revoked afterwards. The tenant's Action for each capability is looked up in SQL, as
the workbench write entry does (authz.nexloop_workbench_action_for); a principal without the grant is refused.

Recipe (schema_version nexloop-goal-setup/1, deploy/examples/nx037/goal-setup.example.json):
  {"schema_version", "tenant_id", "workbench_application_id",
   "metric": {"request_id", "metric_id", "version", "name", "aggregation", "unit", "currency", "maturity_seconds", "refund_rule",
              "cohort_rule"} | null,
   "goal": {"request_id", "goal_id", "expected_current_version", "goal_kind", "objective", "period_start", "period_end", "priority",
            "change_summary", "key_results": [{"kr_key", "metric_id", "metric_version", "target", "direction", "window_start",
            "window_end"}]}}
Each request id is a stable business-intent id: running the tool again replays (`replayed: true`), it never publishes twice.
Output: one JSON object of ids and codes, with goal_version_ref = goal:<goal_id>@<expected_current_version + 1> for plan_setup.
Exit 0 published or replayed, 2 refused.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

RECIPE_SCHEMA = 'nexloop-goal-setup/1'
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
_METRIC = {'request_id', 'metric_id', 'version', 'name', 'aggregation', 'unit', 'currency', 'maturity_seconds', 'refund_rule', 'cohort_rule'}
_GOAL = {'request_id', 'goal_id', 'expected_current_version', 'goal_kind', 'objective', 'period_start', 'period_end', 'priority', 'change_summary',
         'key_results'}
_TIMES = ('period_start', 'period_end')


class GoalSetupRejected(ValueError):
    pass


def _time(value):
    if type(value) is not str:
        raise GoalSetupRejected('time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise GoalSetupRejected('UTC time required')
    return parsed


def validate_recipe(value):
    """Shape only; goal_controls validates every field again and SQL decides (expected version, approved metric, owner)."""
    if (type(value) is not dict or set(value) != {'schema_version', 'tenant_id', 'workbench_application_id', 'metric', 'goal'}
            or value['schema_version'] != RECIPE_SCHEMA or type(value['tenant_id']) is not str or not _UUID.fullmatch(value['tenant_id'])
            or type(value['workbench_application_id']) is not str or not _NAME.fullmatch(value['workbench_application_id'])):
        raise GoalSetupRejected('recipe')
    metric, goal = value['metric'], value['goal']
    if metric is not None and (type(metric) is not dict or set(metric) != _METRIC or type(metric['request_id']) is not str
                               or not _NAME.fullmatch(metric['request_id'])):
        raise GoalSetupRejected('metric')
    if (type(goal) is not dict or set(goal) != _GOAL or type(goal['request_id']) is not str or not _NAME.fullmatch(goal['request_id'])
            or type(goal['expected_current_version']) is not int or goal['expected_current_version'] < 0
            or type(goal['key_results']) is not list or any(type(k) is not dict for k in goal['key_results'])):
        raise GoalSetupRejected('goal')
    for name in _TIMES:
        _time(goal[name])
    for kr in goal['key_results']:
        for name in ('window_start', 'window_end'):
            _time(kr.get(name))
    return value


def goal_fields(goal):
    """The publish_goal fields with aware datetimes (goal_controls.goal_payload takes datetimes)."""
    fields = {k: v for k, v in goal.items() if k not in _TIMES and k != 'key_results'}
    fields.update({k: _time(goal[k]) for k in _TIMES})
    fields['key_results'] = [{**kr, 'window_start': _time(kr['window_start']), 'window_end': _time(kr['window_end'])} for kr in goal['key_results']]
    return fields


def publish(recipe, *, database_url_file, identity_database_url_file, signing_key_file, signing_key_id, artifact_root, username_file, password_file,
            browser_rate_key_file, origin):
    from nexloop_eios.backend import open_backend
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.context_strategy_setup import owner_session
    from nexloop_eios.goal_controls import CAPABILITIES, GoalGovernedActions
    from nexloop_eios.trusted_configuration import read_private_text
    username = read_private_text(username_file, maximum=1024).strip()
    password = read_private_text(password_file, maximum=1024).strip()
    rate_key = bytes.fromhex(read_private_text(browser_rate_key_file, maximum=256).strip())
    out = {'tenant_id': recipe['tenant_id']}
    with open_backend(database_url=read_private_text(database_url_file, maximum=16384).strip(), artifact_root=artifact_root,
                      signing_key_file=signing_key_file, signing_key_id=signing_key_id) as api, \
            owner_session(read_private_text(identity_database_url_file, maximum=16384).strip(), rate_key, recipe, username, password, origin) as session:
        human = authenticate_browser_business(api._pool, session, world='real')
        owner = GoalGovernedActions(api._pool, human, api._signer)

        def action(operation):
            with api._pool.connection() as db:
                found = db.execute('select authz.nexloop_workbench_action_for(%s,%s,%s)', (human.token_digest, human.world, CAPABILITIES[operation])).fetchone()[0]
            if found is None:
                raise GoalSetupRejected('Action not published: ' + operation)
            if found.get('granted') is not True:
                raise GoalSetupRejected('no grant on the Action: ' + operation)
            return {'action_name': found['stable_name'], 'action_version': found['version']}
        if recipe['metric'] is not None:
            result = owner.approve_metric(**action('approve_metric'), **recipe['metric'])
            out.update(metric_id=recipe['metric']['metric_id'], metric_version=recipe['metric']['version'], metric_replayed=bool(result.get('replayed', False)))
        goal = recipe['goal']
        result = owner.publish_goal(**action('publish_goal'), **goal_fields(goal))
        out.update(goal_id=goal['goal_id'], goal_version_ref=f"goal:{goal['goal_id']}@{goal['expected_current_version'] + 1}",
                   goal_replayed=bool(result.get('replayed', False)))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description='The owner approves a metric and publishes an NX-022 goal version (G-GOAL)')
    for name in ('--recipe', '--database-url-file', '--identity-database-url-file', '--signing-key-file', '--artifact-root', '--username-file',
                 '--password-file', '--browser-rate-key-file'):
        p.add_argument(name, type=Path, required=True)
    p.add_argument('--signing-key-id', required=True)
    p.add_argument('--browser-origin', required=True, help='the workbench origin the login rate limit is bound to')
    a = p.parse_args(argv)
    try:
        from nexloop_eios.trusted_configuration import read_private_text
        recipe = validate_recipe(json.loads(read_private_text(a.recipe, maximum=65536)))
        out = publish(recipe, database_url_file=a.database_url_file, identity_database_url_file=a.identity_database_url_file,
                      signing_key_file=a.signing_key_file, signing_key_id=a.signing_key_id, artifact_root=a.artifact_root, username_file=a.username_file,
                      password_file=a.password_file, browser_rate_key_file=a.browser_rate_key_file, origin=a.browser_origin)
        print(json.dumps(out, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - fixed text only: a database or login error may carry connection details
        known = isinstance(error, GoalSetupRejected) or type(error).__name__ == 'ContextStrategySetupRejected'
        print(json.dumps({'ok': False, 'reason': str(error) if known else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
