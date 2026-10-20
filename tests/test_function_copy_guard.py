"""Function copy guard (NX-041 integration; L6): every re-creation of an existing function from migration 0259 on must differ from that
function's latest earlier definition only by what the registry below allows. An unregistered redefinition, a difference beyond the
registered one, a registered change that is not there, or a registry entry that matches nothing fails.

How to register a new redefinition: add an Allowed(stem, function, why, reverts, header) - `stem` is the migration file name without its
temporary number; `reverts` turn the new body back into the earlier one (each must match at least once); `header` lists an allowed
header change. A difference you cannot explain is a stale copy: re-copy the body from the integration head instead of registering it.
The S5 (clock) and S6c (role check) guards of their own test files check the same functions; their changes are registered here too.
Pure file reading: no database.
"""
import json
import shutil

import pytest

from support.copy_guard import Allowed, MIGRATIONS, check, migrations, normalize, redefinitions

BASE = '0259'
WORLD_NOW = (r'runtime\.nexloop_world_now\((?:t|p_tenant),p_world\)', 'clock_timestamp()')
WALL_AT = (r'runtime\.nexloop_world_wall_at\((?:t|p_tenant),p_world,(v_next|p_at|p_due)\)', r'\1')
APP_ROLE = (r'authz\.nexloop_app_role\(p_world\)', 'session_user')
PG_TEMP = (('set.search_path', ['pg_catalog'], ['pg_catalog', 'pg_temp']),)
CLOCK = 'S5 (D5): the time expressions read the world clock (world_now / world_wall_at); nothing else'
ROLE = 'S6c (D3): the role check reads authz.nexloop_app_role(p_world); nothing else'


def clock(name, *reverts):
    return Allowed('nx041_sim_clock', name, CLOCK, tuple(reverts))


REGISTRY = [
    # S1 (0259): one world check (D1, D9).
    Allowed('nx041_world_check', 'runtime.nexloop_cost_data_mode', 'S1: the data mode from runtime.nexloop_world_data_mode',
            ((r'runtime\.nexloop_world_data_mode\(p_world\)', "case when p_world='real' then 'real' when p_world in ('simulation','shadow') then 'simulation' else 'test' end"),)),
    Allowed('nx041_world_check', 'ontology.nexloop_recall_tenant', 'S1: the world validity check; definer search_path gains pg_temp',
            ((r'not runtime\.nexloop_world_valid\(p_world\)', "p_world is null or p_world !~ '^[A-Za-z][A-Za-z0-9_.-]{0,63}$'"),), PG_TEMP),
    Allowed('nx041_world_check', 'authz.nexloop_claim_contract', 'S1: the Claim mode from runtime.nexloop_world_data_mode',
            ((r'runtime\.nexloop_world_data_mode\(x\.world\)', "case when x.world='real' then 'real' when x.world in ('simulation','shadow') then 'simulation' else 'test' end"),)),
    Allowed('nx041_world_check', 'authz.nexloop_metrics_export', "S1: 'simulation' is no world; real and test exported",
            ((r"\(values \('real'\),\('test'\)\)", "(values ('real'),('test'),('simulation'))"),)),
    # S2 (0260): simulation worlds.
    Allowed('nx041_sim_worlds', 'control.nexloop_retention_settings_check', 'S2: retention class sim_world_summary accepted',
            ((r",\s*'sim_world_summary'\)", ')'),)),
    Allowed('nx041_sim_worlds', 'runtime.nexloop_retention_sweep', 'S2: the sim_world_summary class dispatched first',
            ((r"\s*if p_class='sim_world_summary' then\s*return runtime\.nexloop_retention_sweep_sim_worlds\(p_tenant,p_world,p_class,p_batch,p_ref,p_by\);\s*end if;", ''),)),
    Allowed('nx041_sim_worlds', 'authz.nexloop_metrics_export', 'S2: registered active simulation worlds exported besides real and test',
            ((r'\(select jsonb_agg\(runtime\.nexloop_metrics_snapshot\(x\.tenant_id,x\.world\) order by x\.tenant_id,x\.world\) from \(\s*select t\.tenant_id,w\.world from '
              r"(control\.nexloop_tenants t cross join \(values \('real'\),\('test'\)\) w\(world\) where t\.status='active')\s*union all\s*select .*?\) x\)",
              r'(select jsonb_agg(runtime.nexloop_metrics_snapshot(t.tenant_id,w.world) order by t.tenant_id,w.world) from \1)'),)),
    Allowed('nx041_sim_worlds', 'runtime.nexloop_replay_deletions', 'S2: a sim_world deletion entry replayed after a restore',
            ((r'n_world integer:=0;', ''), (r",'sim_world'\)", ')'),
             (r"\s*elsif x->>'kind'='sim_world' then.*?if found then n_world:=n_world\+1;else n_skipped:=n_skipped\+1;end if;", ''),
             (r"'sim_worlds',n_world,", ''))),
    # S5 (0263): the simulation clock (the same changes the clock test's own guard reverts).
    clock('authz.nexloop_commitment_command', WORLD_NOW, WALL_AT),
    clock('runtime.nexloop_commitment_touch', WALL_AT),
    clock('runtime.nexloop_plan_mark_due', WORLD_NOW),
    clock('authz.nexloop_plan_feed_touch', WALL_AT),
    clock('runtime.nexloop_outreach_accept', WORLD_NOW),
    clock('control.nexloop_governed_outreach_approve', WORLD_NOW),
    clock('runtime.nexloop_outreach_expire_approvals', WORLD_NOW),
    clock('runtime.nexloop_outreach_metrics', WORLD_NOW),
    clock('authz.nexloop_outreach_command', WORLD_NOW),
    Allowed('nx041_sim_clock', 'runtime.nexloop_outreach_gates', CLOCK + ' (the budget period keeps the wall clock in a simulation world)',
            ((r"\n v_budget_at timestamptz:=case when runtime\.nexloop_world_kind\(p_world\) in \('simulation','shadow'\) then clock_timestamp\(\) else p_at end;", ''),
             (r'\bv_budget_at\b', 'p_at'))),
    # S6 review fixes (0265): the world time without a catch-all.
    Allowed('nx041_sim_files_review', 'runtime.nexloop_sim_world_time', 'S6 review 5.2: world_now called when present, no exception block',
            ((r"declare v timestamptz;\s*begin\s*if to_regprocedure\('runtime\.nexloop_world_now\(text,text\)'\) is null then return clock_timestamp\(\);end if;"
              r"(?:\s|--[^\n]*)*execute 'select runtime\.nexloop_world_now\(\$1,\$2\)' into v using p_tenant,p_world;\s*return v;",
              'begin return runtime.nexloop_world_now(p_tenant,p_world); exception when undefined_function then return clock_timestamp();'),)),
    # S6c (0268): the simulation roles.
    Allowed('nx041_sim_roles', 'authz.nexloop_queue_command_before_refusals_v0150', ROLE, (APP_ROLE,)),
    Allowed('nx041_sim_roles', 'authz.nexloop_runtime_activation_command_v0037', ROLE + '; definer search_path gains pg_temp', (APP_ROLE,), PG_TEMP),
    Allowed('nx041_sim_roles', 'authz.nexloop_runtime_activation_hint', ROLE + '; definer search_path gains pg_temp', (APP_ROLE,), PG_TEMP),
    Allowed('nx041_sim_roles', 'authz.nexloop_run_tool_timing', ROLE, (APP_ROLE,)),
    Allowed('nx041_sim_roles', 'authz.nexloop_record_run_tool_timing', ROLE, (APP_ROLE,)),
    Allowed('nx041_sim_roles', 'authz.nexloop_record_process_sample', 'S6c: the world-less role check (app_role_unscoped); review 1.5: simulation samples kept apart',
            ((r'authz\.nexloop_app_role_unscoped\(\)', 'session_user'), (r"\n -- Review 1\.5: a simulation role's samples.*?\n end if;", ''))),
    Allowed('nx041_sim_roles', 'authz.nexloop_sim_effect_submit', ROLE, (APP_ROLE,)),
    Allowed('nx041_sim_roles', 'authz.nexloop_sim_effect_query', ROLE, (APP_ROLE,)),
    # Host simulation context (0269): S7b's entry for the simulation event intake's role.
    Allowed('nx041_host_sim_context', 'authz.nexloop_sim_context_command', ROLE, (APP_ROLE,)),
    Allowed('nx041_d10_credentials', 'authz.nexloop_issue_run_credential', 'D10 wiring: a simulation role issues Run credentials of its own world only (app_role); definer search_path gains pg_temp',
            ((r"if authz\.nexloop_app_role\(p_world\) is distinct from 'nexloop_api' then raise exception 'Run issuance denied' using errcode='42501';end if;\s*", ''),),
            PG_TEMP),
    # D10 (0273): one active credential per world and use; the use immutable like the world.
    Allowed('nx041_d10_credentials', 'authz.nexloop_sim_world_credential_check', 'D10: one active credential per world and use (api, worker, agent); the use never changes',
            ((r'\(old\.worlds is distinct from new\.worlds or old\.sim_use is distinct from new\.sim_use\)', '(old.worlds is distinct from new.worlds)'),
             (r"if new\.sim_use is null then raise exception 'a simulation world credential names its use \(api, worker, agent\)' using errcode='42501';end if;\s*", ''),
             (r'c\.worlds=new\.worlds and c\.sim_use=new\.sim_use and', 'c.worlds=new.worlds and'),
             (r"'a simulation world has one active credential per use'", "'a simulation world has one active credential'"))),
]


def test_every_redefinition_from_0259_differs_only_by_its_registered_change():
    found = redefinitions(migrations(), BASE)
    assert len(found) >= len(REGISTRY)  # the registry covers what exists, no more
    problems = check(found, REGISTRY)
    assert problems == [], '\n'.join(problems)


def test_the_two_dedicated_guards_are_registered_here_too():
    names = {(a.stem, a.name) for a in REGISTRY}
    from test_nx041_sim_roles_pg import ROLE_CHECKED
    from test_nx041_sim_clock_pg import CONVERTED
    assert {('nx041_sim_roles', n) for n in ROLE_CHECKED} <= names
    assert {('nx041_sim_clock', n) for n in CONVERTED + ('runtime.nexloop_outreach_gates',)} <= names


def _scenario(tmp_path, extra_sql, name='0270_nx041_fake_copy.sql'):
    """The real migrations plus one more, in a scratch directory (nothing in the repository changes)."""
    directory = tmp_path / 'migrations'
    shutil.copytree(MIGRATIONS, directory, ignore=shutil.ignore_patterns('__pycache__', '*.py'))
    (directory / name).write_text(extra_sql)
    catalog = json.loads((directory / 'catalog.json').read_text())
    catalog['migrations'].append({'version': name[:4], 'name': name, 'sha256': '0' * 64})
    (directory / 'catalog.json').write_text(json.dumps(catalog))
    return redefinitions(migrations(directory), BASE)


def _latest(name):
    return [r for r in redefinitions(migrations(), BASE) if r.name == name][-1].new


def test_positive_control_a_stale_copy_fails(tmp_path):
    """A migration re-creating the run tool timing from its 0133 body (before S6c's role check) - the very mistake of S6c's stand-in
    submit and of 0269 - is reported, registered or not; so is a copy that keeps the allowed change but loses an earlier fix."""
    stale_source = (MIGRATIONS / '0133_nx030_ui.sql').read_text()
    start = stale_source.index('create function authz.nexloop_run_tool_timing')
    stale = 'create or replace function' + stale_source[start + len('create function'):stale_source.index('end $$;', start) + len('end $$;')]
    found = _scenario(tmp_path, stale)
    unregistered = check(found, REGISTRY)
    assert any('0270_nx041_fake_copy.sql: authz.nexloop_run_tool_timing' in p and 'without a registry entry' in p for p in unregistered), unregistered
    registered = check(found, REGISTRY + [Allowed('nx041_fake_copy', 'authz.nexloop_run_tool_timing', ROLE, (APP_ROLE,))])
    assert any("the registered change" in p and 'nexloop_app_role' in p for p in registered), registered
    # The allowed change is there, but a line of the latest body (an earlier branch's fix) is gone: a difference beyond the change.
    latest = _latest('authz.nexloop_sim_effect_submit')
    body = latest.body
    lost = body.replace("raise exception 'stand-in: business_intent_open'", "raise exception 'stand-in: intent_conflict'", 1)
    assert lost != body
    text = 'create or replace function authz.nexloop_sim_effect_submit' + latest.text.replace(body, lost) + ';'
    problems = check(_scenario(tmp_path / 'b', text), REGISTRY + [Allowed('nx041_fake_copy', 'authz.nexloop_sim_effect_submit', 'role check', ())])
    assert any('beyond "role check"' in p for p in problems), problems


def test_positive_control_a_header_change_and_an_unused_entry_fail(tmp_path):
    latest = _latest('authz.nexloop_run_tool_timing')
    changed = latest.text.replace('set search_path=pg_catalog,pg_temp', 'set search_path=public,pg_catalog,pg_temp', 1)
    assert changed != latest.text
    text = 'create or replace function authz.nexloop_run_tool_timing' + changed + ';'
    problems = check(_scenario(tmp_path, text), REGISTRY + [Allowed('nx041_fake_copy', 'authz.nexloop_run_tool_timing', 'none', ())])
    assert any('header differs' in p for p in problems), problems
    assert any('matches no redefinition' in p for p in check(redefinitions(migrations(), BASE), REGISTRY + [Allowed('nx041_nothing', 'authz.x', 'none')]))


def test_layout_and_comments_do_not_count_and_a_rename_keeps_its_definition():
    assert normalize('a  b -- c\n /* d */ e') == normalize('ab e')
    renamed = [r for r in redefinitions(migrations(), BASE) if r.name == 'authz.nexloop_runtime_activation_command_v0037'][0]
    assert renamed.old.migration.startswith('0037_')  # created by 0037 under the entry's own name, renamed later


@pytest.mark.parametrize('stem', sorted({a.stem for a in REGISTRY}))
def test_each_registered_migration_exists(stem):
    assert any(m.name[5:].removesuffix('.sql') == stem for m in migrations()), stem
