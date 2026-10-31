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
import re
import shutil

import pytest

from support.copy_guard import Allowed, MIGRATIONS, check, migrations, normalize, redefinitions
from support.remediation_r2_edits import EDITS as R2_EDITS

BASE = '0259'
WORLD_NOW = (r'runtime\.nexloop_world_now\((?:t|p_tenant),p_world\)', 'clock_timestamp()')
WALL_AT = (r'runtime\.nexloop_world_wall_at\((?:t|p_tenant),p_world,(v_next|p_at|p_due)\)', r'\1')
APP_ROLE = (r'authz\.nexloop_app_role\(p_world\)', 'session_user')
PG_TEMP = (('set.search_path', ['pg_catalog'], ['pg_catalog', 'pg_temp']),)
CLOCK = 'S5 (D5): the time expressions read the world clock (world_now / world_wall_at); nothing else'
ROLE = 'S6c (D3): the role check reads authz.nexloop_app_role(p_world); nothing else'


def clock(name, *reverts):
    return Allowed('nx041_sim_clock', name, CLOCK, tuple(reverts))


DENIAL = 'signature denial causes: the denial raise carries its cause (DETAIL) and the excess over the bound (HINT); nothing else'


def denial(name, message, header=()):
    return Allowed('signature_denial_causes', name, DENIAL + (' (and pg_temp last in search_path)' if header else ''),
                   ((r"raise exception '" + message + r"' using errcode='42501',detail=authz\.nexloop_signed_denial_cause\(.*?\),"
                     r"hint=(?:authz\.nexloop_signed_ahead_hint\([^)]*\)|'');", "raise exception '" + message + "' using errcode='42501';"),), header)


# NX-043 online opened: the twin check of D9 through runtime.nexloop_experiment_twin_ok (negated or positive form of the copied body).
TWIN_NEG = ((r"not runtime\.nexloop_experiment_twin_ok\(p_tenant,(\w+(?:\.\w+)?)\)",
            r"\1->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,\1->'twin'->>'twin_id',(\1->'twin'->>'version')::integer)"),)
TWIN_POS = ((r"runtime\.nexloop_experiment_twin_ok\(p_tenant,(\w+(?:\.\w+)?)\)",
            r"(\1->'twin'='null'::jsonb or runtime.nexloop_consumer_twin_selectable(p_tenant,\1->'twin'->>'twin_id',(\1->'twin'->>'version')::integer))"),)


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
    # Sim world limits (0272, owner 2026-10-21): at most 20 active worlds per tenant, expiry at most 30 days, expired worlds deleted.
    Allowed('nx041_sim_world_limits', 'control.nexloop_governed_create_sim_world', 'limits: expiry optional (default 30), at most 30; 20 active worlds per tenant under a lock',
            ((r"coalesce\(body->'expires_in_days','30'::jsonb\)", "body->'expires_in_days'"),
             (r"coalesce\(body->>'expires_in_days','30'\)", "(body->>'expires_in_days')"),
             (r"\s*if v_days>30 then raise exception 'sim world: expiry_over_30_days' using errcode='22023';end if;", ''),
             (r"\s*-- At most 20 active worlds.*?'sim world: limit_reached' using errcode='22023';end if;", ''))),
    Allowed('nx041_sim_world_limits', 'runtime.nexloop_retention_sweep_sim_worlds', 'limits: expired worlds deleted first; a failing or blocked world skipped, least recently continued first',
            ((r'n_expired integer:=0;n_failed integer:=0;', ''),
             (r"\s*-- Expired active worlds.*?n_failed:=n_failed\+1;\s*end;\s*perform set_config\('eios\.tenant_id',p_tenant,true\);\s*end loop;", ''),
             (r"\s*-- Worlds still deleting.*?limit 10 loop\s*begin\s*(perform runtime\.nexloop_sim_world_purge\(p_tenant,x\.world,p_batch\);n_resumed:=n_resumed\+1;)"
              r"\s*exception when others then n_failed:=n_failed\+1;\s*end;",
              r" for x in select world from control.nexloop_sim_worlds where tenant_id=p_tenant and status='deleting' order by created_at limit 10 loop \1"),
             (r",'worlds_expired',n_expired,'worlds_failed',n_failed", ''))),
    # D10 grants review P1 (0275): S6c's second defence on the two entries D10 granted to the simulation roles.
    Allowed('nx041_d10_grants_review', 'authz.nexloop_read_action_bundle', 'D10 review P1: a simulation role outside its world is refused (app_role); definer search_path gains pg_temp',
            ((r"if authz\.nexloop_app_role\(p_world\)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;\s*", ''),),
            PG_TEMP),
    Allowed('nx041_d10_grants_review', 'authz.nexloop_role_run_hint', 'D10 review P1: a simulation role outside its world is refused (app_role); definer search_path gains pg_temp',
            ((r"if authz\.nexloop_app_role\(p_world\)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;\s*", ''),),
            PG_TEMP),
    # 0310 (AT-046 stage fixes, L6 batch-12 review 2.1): the simulation worker's dispatch-control entries refuse it outside its world.
    Allowed('sim_worker_dispatch_controls', 'authz.nexloop_assert_task_dispatch_controls', 'batch-12 review 2.1: a simulation role outside its world is refused (app_role); definer search_path gains pg_temp',
            ((r"\s*-- L6 batch-12 review 2\.1[^\n]*\n\s*if authz\.nexloop_app_role\(p_world\)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;\s*", ''),), PG_TEMP),
    # 0310 (AT-046 stage fixes, L6 batch-12 review 2.1): the simulation worker's dispatch-control entries refuse it outside its world.
    Allowed('sim_worker_dispatch_controls', 'authz.nexloop_bind_run_goal', 'batch-12 review 2.1: a simulation role outside its world is refused (app_role); definer search_path gains pg_temp',
            ((r"\s*-- L6 batch-12 review 2\.1[^\n]*\n\s*if authz\.nexloop_app_role\(p_world\)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;\s*", ''),), PG_TEMP),
    # 0310 (AT-046 stage fixes, L6 batch-12 review 2.1): the simulation worker's dispatch-control entries refuse it outside its world.
    Allowed('sim_worker_dispatch_controls', 'authz.nexloop_budget_status', 'batch-12 review 2.1: a simulation role outside its world is refused (app_role); definer search_path gains pg_temp',
            ((r"\s*-- L6 batch-12 review 2\.1[^\n]*\n\s*if authz\.nexloop_app_role\(p_world\)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;\s*", ''),), PG_TEMP),
    # 0310 (AT-046 stage fixes, L6 batch-12 review 2.1): the simulation worker's dispatch-control entries refuse it outside its world.
    Allowed('sim_worker_dispatch_controls', 'authz.nexloop_reserve_budget', 'batch-12 review 2.1: a simulation role outside its world is refused (app_role); definer search_path gains pg_temp',
            ((r"\s*-- L6 batch-12 review 2\.1[^\n]*\n\s*if authz\.nexloop_app_role\(p_world\)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;\s*", ''),), PG_TEMP),
    # 0311 (L6 AT-046 batch-12 fix review): online still waives the twin's calibration but not its withdrawal; the synthetic twin of the
    # acceptance never backs an online or shadow registration.
    Allowed('nx043_twin_withdrawn_online', 'runtime.nexloop_experiment_twin_ok', 'online: a selected twin must not be retired (calibration still waived)',
            ((r"\(r->>'stage'='online' and runtime\.nexloop_consumer_twin_unretired\(p_tenant,r->'twin'->>'twin_id',\(r->'twin'->>'version'\)::integer\)\)",
              "r->>'stage'='online'"),)),
    Allowed('nx043_twin_withdrawn_online', 'control.nexloop_governed_register_experiment', 'online and shadow registrations refuse the synthetic twin (twin_synthetic)',
            ((r"\s*-- L6 AT-046 batch-12 fix review[^\n]*\n\s*if r->>'stage' in \('online','shadow'\) and coalesce\(r->'twin'->>'twin_id',''\) like 'at046-synthetic-%' then\s*raise exception 'experiment: twin_synthetic' using errcode='22023';end if;", ''),)),
    # 0312 (AT-046 stage judgement): a Run command's mode must be the kind of the world it is presented in (server side, not only the client).
    Allowed('run_command_mode_is_world_kind', 'authz.nexloop_runtime_activation_command_v0037', 'the command mode must be the world kind',
            ((r"\s*or command->>'mode' is distinct from runtime\.nexloop_world_kind\(p_world\)", ''),)),
    # 0317 (stage 2026-10-22, run retry errors): the v6 bind compares current_constraints without the ledger's reserved_units counter.
    Allowed('context_v6_reserved_units_volatile', 'authz.nexloop_context_v6_command', 'current_constraints compared without reserved_units',
            ((r"\(case when key='current_constraints' then \(pack->key\)-'reserved_units' else pack->key end\)\s*is distinct from \(case when key='current_constraints' then \(snapshot->key\)-'reserved_units' else snapshot->key end\)",
              'pack->key is distinct from snapshot->key'),)),
    # 0334 (NX-058 slice B review I-2): the operator reset shares the password write of the owner-issued codes and ends their unused codes.
    Allowed('operator_reset_revokes_codes', 'control.nexloop_operator_reset_browser_password',
            'the password write through control.nexloop_browser_password_write (same effect) and every unused reset code revoked; nothing else',
            ((r"body:=control\.nexloop_browser_password_write\(p_tenant,old,p_hash,now_at\);",
              "history:=(case when old.password_hash is null then old.password_history else array[old.password_hash]||old.password_history end)[1:10];"
              "body:=old.payload||jsonb_build_object('revision',p_expected+1,'session_epoch',(old.payload->>'session_epoch')::bigint+1,'failed_attempts',0,"
              "'lockout_level',0,'locked_until',null,'updated_at',to_jsonb(now_at));"
              "update control.nexloop_browser_accounts a set payload=body,password_hash=p_hash,password_history=history "
              "where a.tenant_id=p_tenant and a.local_account_id=p_account;"),
             (r"\s*perform control\.nexloop_workbench_reset_revoke\(p_tenant,old\.subject_id,'operator_reset'\);", ''))),
    # 0341 (NX-058 slice C, dispatcher option 1): the browser directory hash takes the tenant revision through
    # authz.nexloop_effective_tenant_revision (the revision before a governed handler's own authority change in this transaction).
    Allowed('governed_authority_change', 'authz.nexloop_browser_identity_snapshot',
            'the tenant revision of the directory hash through authz.nexloop_effective_tenant_revision; definer search_path gains pg_temp; nothing else',
            ((r"'tenant_revision',authz\.nexloop_effective_tenant_revision\(realm\.tenant_id,realm\.authority_revision\)", "'tenant_revision',realm.authority_revision"),),
            PG_TEMP),
    # 0341 (NX-058 slice C review I-2): the workbench member lock is NX-056's tenant approval lock.
    Allowed('governed_authority_change', 'control.nexloop_workbench_member_lock', 'the tenant approval lock of NX-056 instead of its own key; nothing else',
            ((r"select control\.nexloop_approval_tenant_lock\(p_tenant\)", "select pg_advisory_xact_lock(hashtextextended('nexloop-approval-eligibility:'||p_tenant,0))"),)),
    # owner_member_actions (NX-058 slice D2): approval_open checks the body's operation against the capability's registered one (L6 review
    # of B1); demote_owner's policy may show new_role.
    Allowed('owner_member_actions', 'control.nexloop_approval_open', "the body's operation is the capability's registered operation; a suspended target opens too; nothing else",
            ((r"\s*-- only an invited, active or suspended member[^\n]*\n\s*--[^\n]*\n\s*and \(m\.payload->>'status' in \('invited','active'\) or \(m\.payload->>'status'='suspended' and p_capability<>'workbench_member\.promote_owner'\)\)\) then",
              "\n   and m.payload->>'status' in ('invited','active')) then  -- only an invited or active member (L6 review 9)"),
             (r"\s*-- NX-058 slice D \(L6 review of B1\)[^\n]*\n\s*if p_body->>'operation' is distinct from \(select c\.operation from control\.nexloop_governed_capabilities c where c\.capability=p_capability\) then\n\s*raise exception 'approval: operation_mismatch' using errcode='22023';end if;", ''),)),
    Allowed('owner_member_actions', 'control.nexloop_approval_summary_allowed', 'demote_owner shows new_role; nothing else',
            ((r"\s*when 'workbench_member\.demote_owner' then array\['new_role'\][^\n]*", ''),)),
    # vote_cooldown (NX-058 slice D §2, ADR-039 R1 §6.7): an approver's vote counts 24 hours after its current holding of an approver role began.
    Allowed('vote_cooldown', 'control.nexloop_approval_approver_eligible', 'the 24-hour vote cooldown from the role history; nothing else',
            ((r"\s*and coalesce\(control\.nexloop_workbench_vote_eligible_from\(p_tenant,'real',control\.nexloop_approval_subject_of\(p_tenant,p_principal\),p_roles\)<=clock_timestamp\(\),false\)", ''),)),
    # D10 trusted configuration (0274): a manifest names a simulation credential's use; real and test name none.
    Allowed('nx041_d10_manifest', 'control.nexloop_configure_manifest', 'D10: a sim credential of the manifest names its use (checked, written, compared on replay)',
            ((r"\s*--[^\n]*\n\s*or \(case when jsonb_array_length\(item->'worlds'\)=1 and item->'worlds'->>0~'\^\(sim\|shadow\):'\s*then coalesce\(item->>'sim_use',''\) not in \('api','worker','agent'\) else item \? 'sim_use' end\)", ''),
             (r",item->>'sim_use'\)\s*on conflict\(token_digest\) do nothing;", ') on conflict(token_digest) do nothing;'),
             (r'insert into authz\.nexloop_service_credentials\(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,sim_use\) values',
              'insert into authz.nexloop_service_credentials values'),
             (r"\s*and sim_use is not distinct from item->>'sim_use'\) then raise exception 'configuration conflict'", ") then raise exception 'configuration conflict'"))),
    Allowed('nx041_d10_credentials', 'authz.nexloop_issue_run_credential', 'D10 wiring: a simulation role issues Run credentials of its own world only (app_role); definer search_path gains pg_temp',
            ((r"if authz\.nexloop_app_role\(p_world\) is distinct from 'nexloop_api' then raise exception 'Run issuance denied' using errcode='42501';end if;\s*", ''),),
            PG_TEMP),
    # D10 (0273): one active credential per world and use; the use immutable like the world.
    Allowed('nx041_d10_credentials', 'authz.nexloop_sim_world_credential_check', 'D10: one active credential per world and use (api, worker, agent); the use never changes',
            ((r'\(old\.worlds is distinct from new\.worlds or old\.sim_use is distinct from new\.sim_use\)', '(old.worlds is distinct from new.worlds)'),
             (r"if new\.sim_use is null then raise exception 'a simulation world credential names its use \(api, worker, agent\)' using errcode='42501';end if;\s*", ''),
             (r'c\.worlds=new\.worlds and c\.sim_use=new\.sim_use and', 'c.worlds=new.worlds and'),
             (r"'a simulation world has one active credential per use'", "'a simulation world has one active credential'"))),
    # Signature denial causes (0276, L6): the denial raise of each signed-command gate carries its cause as DETAIL (and the excess
    # over the bound as HINT); errcode and message unchanged; nothing else.
    denial('authz.nexloop_queue_command_before_refusals_v0150', 'queue signature denied'),
    denial('authz.nexloop_runtime_activation_command_v0037', 'activation denied'),
    denial('authz.nexloop_effect_execution_command_v0052', 'effect provider authority unavailable', PG_TEMP),
    denial('authz.nexloop_context_v6_command', 'context v6 unavailable'),
    # Clock skew metric (0277, L6): process sample kind 'clock' accepted; nothing else.
    Allowed('clock_skew_metric', 'authz.nexloop_record_process_sample', "the process sample kind 'clock' (skew_ms, skew_abs_ms, round_trip_ms)",
            ((r"\s*when 'clock' then array\['skew_ms','skew_abs_ms','round_trip_ms'\]", ''),)),
    # EIOS gap E4 (NX-061, 0323): the process sample kind 'api' (read/write; calls, errors, slow, latency, histogram); nothing else.
    Allowed('api_request_metrics', 'authz.nexloop_record_process_sample', "the process sample kind 'api' (service read or write, the guard histogram)",
            ((r"\s*when 'api' then array\['calls','errors','slow','p50_ms','p95_ms','max_ms'\]", ''),
             (r"\s*or \(p_kind='api' and p_service not in \('read','write'\)\)", ''),
             (r"\('guard','guard_op','relay_step','api'\)", "('guard','guard_op','relay_step')"))),
    # NX-057 R0 (effect_failures): the three paths that fail an effect intent write its failure row; nothing else.
    *[Allowed('effect_failures', name, 'NX-057 R0: the failure row (reason and evidence ids) in the same transaction; nothing else',
              ((r'\s*insert into runtime\.nexloop_effect_failures\(.*?\);', ''),))
      for name in ('authz.nexloop_fail_refused_effect', 'authz.nexloop_effect_origin_expired', 'runtime.nexloop_governed_confirm_not_delivered')],
    # NX-057 R0 (effect_failures): the outcome_classes snapshot counts the effect outcomes by event time and reads them with the tenant set; nothing else.
    Allowed('effect_failures', 'runtime.nexloop_metrics_snapshot', 'NX-057 R0: effect outcomes by event time (fulfilled, first unknown, '
            'failure rows), read with the tenant set; refusals and dead letters unchanged',
            ((r"\n prior text:=current_setting\('eios\.tenant_id',true\);", ''),
             (r"\n perform set_config\('eios\.tenant_id',p_tenant,true\);", ''),
             (r"\n perform set_config\('eios\.tenant_id',coalesce\(prior,''\),true\);", ''),
             (r"intents as \(\s*select 'succeeded' k,count\(distinct e\.intent_id\) n from runtime\.nexloop_effect_events e.*?"
              r"f\.recorded_at>now_at-interval '1 hour'\),",
              "intents as (select case i.state when 'fulfilled' then 'succeeded' when 'confirmed' then 'succeeded' when 'unknown' then "
              "'outcome_unknown' when 'failed' then 'defect' end k,count(*) n from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant "
              "and i.world=p_world and i.created_at>now_at-interval '1 hour' and i.state in ('fulfilled','confirmed','unknown','failed') and not "
              "(i.state='failed' and exists(select 1 from runtime.nexloop_dispatch_refusals r where r.tenant_id=i.tenant_id and r.world=i.world "
              "and r.intent_id=i.intent_id)) group by 1),"))),
    # NX-057 R2 (remediation_wake): three R1 functions re-created with exactly the edits listed in support/remediation_r2_edits.py.
    *[Allowed('remediation_wake', name, 'NX-057 R2: reevaluate opens, the wake, origin, breaker and the manual wake; nothing else',
              tuple((re.escape(new), old.replace('\\', '\\\\')) for old, new in reversed(edits)))
      for name, edits in R2_EDITS.items()],
    # NX-042 T6 (D1): the twin's step text from the text layer (default deterministic template) instead of T3's fixed placeholder.
    Allowed('nx042_twin_text', 'authz.nexloop_twin_act', 'NX-042 T6: the step text from runtime.nexloop_twin_render_text and three text attributes; nothing else',
            ((r'v_result jsonb;v_text jsonb;', 'v_result jsonb;'),
             (r"\s*-- T6 text layer \(D1\)[^\n]*\n\s*v_text:=runtime\.nexloop_twin_render_text\(t\.generator_version,b\.seed\|\|'\|'\|\|b\.twin_digest\|\|'\|'\|\|p_consumer,p_step,v_choice\.behavior,v_choice\.kind\);", ''),
             (r"\(case when v_text->>'text' is not null then jsonb_build_object\('text',v_text->>'text'\) else '\{\}'::jsonb end\)",
              "(case when v_choice.kind='message' then jsonb_build_object('text','合成消息（孪生行为 '||v_choice.behavior||'）') else '{}'::jsonb end)"),
             (r"\s*\|\|case when v_text->>'text' is not null then jsonb_build_object\('text_template',v_text->>'template_id','text_generator',v_text->>'generator_version',\s*'text_marker','simulated'\) else '\{\}'::jsonb end\)",
              ')'))),
    # AT-048 (0286, L3, S6 stage finding): the evaluation opening also refuses when one Consumer has cases on both sides; nothing else.
    Allowed('at048_consumer_leakage', 'authz.nexloop_evo_evaluation_open', 'AT-048: leakage_detected also when the cases of both sides share a Consumer',
            ((r"\s*-- AT-048 \(S6 stage finding\)[^\n]*\n\s*or exists\(select 1 from unnest\(x\.design_case_ids\) dc join unnest\(x\.validation_case_ids\) vc\s*"
              r"on runtime\.nexloop_evo_run_consumer\(v_tenant,'real',dc\)=runtime\.nexloop_evo_run_consumer\(v_tenant,'real',vc\)\)", ''),)),
    # AT-048 performance (L3 4c1cf6b0) in its own migration: 0286's pairwise Consumer comparison becomes one linear helper.
    Allowed('at048_consumer_leak_perf', 'runtime.nexloop_evo_run_consumer',
            "AT-048 performance: the same three sources in the same order (message route, fallback binding, role binding), looked up step by step by the Run's uuid; a non-uuid case has no Consumer; nothing else",
            ((r"v text;v_run uuid;v_message text;v_conversation text;\s*begin.*?(?=\s*perform set_config\('eios\.tenant_id',coalesce\(v_prior)",
              "v text;begin perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);v:=coalesce("
              "(select cv.consumer_id from runtime.nexloop_message_routes mr join runtime.nexloop_conversation_messages m on m.tenant_id=mr.tenant_id and m.world=mr.world "
              "and m.message_id=mr.message_id join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id "
              "where mr.tenant_id=p_tenant and mr.world=p_world and mr.run_id::text=p_run limit 1),"
              "(select cv.consumer_id from runtime.nexloop_fallback_context_bindings fb join runtime.nexloop_conversation_messages m on m.tenant_id=fb.tenant_id and m.world=fb.world "
              "and m.message_id=fb.message_id join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id "
              "where fb.tenant_id=p_tenant and fb.world=p_world and fb.run_id::text=p_run limit 1),"
              "(select rb.consumer_id from authz.nexloop_role_run_bindings rb where rb.tenant_id=p_tenant and rb.world=p_world and rb.run_id::text=p_run limit 1));"),)),
    Allowed('at048_consumer_leak_perf', 'authz.nexloop_evo_evaluation_open', 'AT-048 performance: the pairwise Consumer comparison as one linear helper; nothing else',
            ((r"or runtime\.nexloop_evo_consumer_leak\(v_tenant,x\.design_case_ids,x\.validation_case_ids\)",
              "or exists(select 1 from unnest(x.design_case_ids) dc join unnest(x.validation_case_ids) vc\n     on runtime.nexloop_evo_run_consumer(v_tenant,'real',dc)=runtime.nexloop_evo_run_consumer(v_tenant,'real',vc))"),)),
    # NX-042 T5 (L6 nx042-t5-fix-verify 1.3, K = 3): calibrated also needs none of the last 3 verdicts failed.
    Allowed('nx042_twin_calibration', 'runtime.nexloop_consumer_twin_calibration', 'NX-042 T5: the last 3 verdicts without a failed one; nothing else',
            ((r'v text;v_recent_failed boolean;', 'v text;'),
             (r"\s*select bool_or\(x\.verdict='failed'\) into v_recent_failed from \(select c\.verdict from control\.nexloop_consumer_twin_calibrations c\s*where c\.tenant_id=p_tenant and c\.twin_id=p_twin and c\.version=p_version order by c\.event_id desc limit 3\) x;", ''),
             (r"v='passed' and not coalesce\(v_recent_failed,false\)", "v='passed'"))),
    # NX-042 T3 / D10 extended (owner 2026-10-21): a fourth credential use, twin.
    Allowed('nx042_twin_credential', 'authz.nexloop_sim_world_credential_check', 'D10 extended: the message names the four uses (twin added); nothing else',
            ((r'\(owner, as recommended; extended by the owner 2026-10-21 with twin\): one active credential per world and use \(api, worker, agent, twin\)',
              '(owner, as recommended): one active credential per world and use (api, worker, agent)'),
             (r"names its use \(api, worker, agent, twin\)'", "names its use (api, worker, agent)'"))),
    Allowed('nx042_twin_credential', 'control.nexloop_configure_manifest', 'D10 extended: a simulation credential may name the use twin; nothing else',
            ((r"not in \('api','worker','agent','twin'\)", "not in ('api','worker','agent')"),)),
    # NX-042 T3: the twin's observe entry (owner-run, the port role's only way in) reaches the S7b assembler.
    Allowed('nx042_twin_port', 'authz.nexloop_sim_context_command', 'NX-042 T3: the port role nexloop_twin_port passes the role check, for a sim:/shadow: world only; nothing else',
            ((r" and not \(session_user='nexloop_twin_port' and coalesce\(runtime\.nexloop_world_kind\(p_world\),''\) in \('simulation','shadow'\)\)", ''),)),
    # NX-043 X1 follow-up (dispatcher 2026-10-21): a role holds up to 128 Actions, as nexloop_eios.workbench_roles (owner 65).
    Allowed('nx043_workbench_role_cap', 'control.nexloop_configure_workbench', 'the per-role Action bound 64 -> 128; nothing else',
            ((r'jsonb_array_length\(r\.value\) not between 1 and 128', 'jsonb_array_length(r.value) not between 1 and 64'),)),
    # NX-043 X4 (design §3): the eligibility rule joins the frozen preregistration (default only, never looser).
    Allowed('nx043_online_allocation', 'runtime.nexloop_experiment_registration_problem', 'X4: eligibility is an allowed member, checked against its default; nothing else',
            ((r",'eligibility'\)\) then return 'invalid'", ")) then return 'invalid'"),
             (r"\n -- X4 \(design §3\)[^\n]*\n if r \? 'eligibility'[^\n]*'eligibility_looser_than_default';end if;\n", "\n"))),
    Allowed('nx043_online_allocation', 'runtime.nexloop_experiment_registration_frozen', 'X4: the eligibility default is written into the frozen registration; nothing else',
            ((r"\n  'eligibility',runtime\.nexloop_experiment_eligibility_default\(\)\|\|coalesce\(r->'eligibility','\{\}'::jsonb\),", ''),)),
    # NX-043 X1 review 2.1: start rechecks the purposes of both strategies against the current list.
    Allowed('nx043_start_purpose_recheck', 'control.nexloop_governed_start_experiment', 'X1 review 2.1: the purpose recheck at start; nothing else',
            ((r"\n   if exists\(select 1 from control\.nexloop_strategy_versions s,jsonb_object_keys[^\n]*'experiment: purpose_not_allowed' using errcode='22023';end if;", ''),)),
    # NX-043 X4 review 3.1, 1.1, 4.1: the candidate cluster check, the erasing exclusion and the strategy state at decision.
    Allowed('nx043_allocation_review', 'runtime.nexloop_experiment_assign', 'X4 review: a candidate cluster member checked against the control size; erasing excluded; nothing else',
            ((r"\n elsif runtime\.nexloop_consumer_erasing\(p_tenant,r->>'world',p_consumer\) then v_reason:='erasing';", ''),
             (r"\(v_cand\+1>v_cap_cand or v_cand\+1>v_ctrl\)", 'v_cand+1>v_cap_cand'))),
    Allowed('nx043_allocation_review', 'runtime.nexloop_experiment_strategy_for', 'X4 review 4.1: retired strategies and a withdrawn twin fall back at decision; nothing else',
            ((r" and runtime\.nexloop_strategy_status\(p_tenant,x\.registration->'candidate'->>'strategy_id',\(x\.registration->'candidate'->>'version'\)::integer\)='published' and \(x\.registration->'twin'='null'::jsonb or runtime\.nexloop_consumer_twin_selectable\([^\n]*?\)::integer\)\) then v_ref:=x\.registration->'candidate';", " then v_ref:=x.registration->'candidate';"),
             (r"\n if v_ref<>'null'::jsonb and v_ref=x\.registration->'control' and runtime\.nexloop_strategy_status[^\n]*", ''))),
    # NX-041 D10 follow-up (0294): a simulation world's Action definitions do not advance the tenant's authority revision.
    Allowed('nx041_sim_catalog_publish', 'authz.nexloop_authority_epoch_guard', 'sim catalog: simulation and shadow Action definitions skip the epoch; definer search_path gains pg_temp',
            ((r"\s*if tg_table_name='nexloop_action_definitions' then\s*(--[^\n]*\s*)?if coalesce\(runtime\.nexloop_world_kind\(\(case when tg_op='DELETE' then to_jsonb\(old\) else to_jsonb\(new\) end\)->>'world'\),''\) in \('simulation','shadow'\)"
              r"\s*and \(tg_op<>'UPDATE' or coalesce\(runtime\.nexloop_world_kind\(to_jsonb\(old\)->>'world'\),''\) in \('simulation','shadow'\)\) then\s*return null;end if;\s*end if;", ''),), PG_TEMP),
    # NX-043 review of the L5 fixes and X5 (3.1, 3.3.b, 2.1.a, 2.1.c, 2.2.a): erasure and review changes only.
    Allowed('nx043_erasure_and_review', 'control.nexloop_experiment_frozen', 'review 3.1: the erasure propagation update passes; nothing else',
            ((r"\n if tg_op='UPDATE' and runtime\.nexloop_erasure_permits\([^\n]*", ''),)),
    Allowed('nx043_erasure_and_review', 'runtime.nexloop_experiment_assign', 'review 3.1: an erasing Consumer gets no row (early answer); nothing else',
            ((r"\n -- Review 3\.1:[^\n]*\n if runtime\.nexloop_consumer_erasing\(p_tenant,r->>'world',p_consumer\) then[^\n]*", ''),
             (r"(then v_reason:='in_takeover';)", "\\1\n elsif runtime.nexloop_consumer_erasing(p_tenant,r->>'world',p_consumer) then v_reason:='erasing';"))),
    Allowed('nx043_erasure_and_review', 'runtime.nexloop_experiment_strategy_for', 'review 3.3.b: an erasing Consumer gets no strategy; nothing else',
            ((r"\n if runtime\.nexloop_consumer_erasing\(p_tenant,p_world,p_consumer\) then[^\n]*", ''),)),
    Allowed('nx043_erasure_and_review', 'runtime.nexloop_experiment_autostop', 'review 2.1.c: the stop request id cannot be an owner request id; nothing else',
            ((r"'guard:'\|\|p_experiment\|\|':'\|\|gen_random_uuid\(\)\);", "'guard:'||p_experiment);"),)),
    Allowed('nx043_erasure_and_review', 'runtime.nexloop_experiment_guard', 'review 2.1.a: every guardrail hit goes with the stop metrics; nothing else',
            ((r"m\|\|jsonb_build_object\('guardrails_hit',runtime\.nexloop_experiment_guardrails_hit\(p_tenant,p_experiment,m\)\)\);", 'm);'),)),
    Allowed('nx043_erasure_and_review', 'runtime.nexloop_experiment_spend', 'review 2.2.a: the amount is rounded to the ledger scale first; nothing else',
            ((r"\n p_amount:=round\(p_amount,4\);[^\n]*", ''),)),
    # NX-043 X5 wiring review (2.3, 1.2, 2.1 control arm): failure isolation, every cluster key on erasure, the cap on the candidate only.
    Allowed('nx043_wiring_review', 'authz.nexloop_experiment_guard_tick', 'review 2.3: each experiment guarded apart, failures counted; nothing else',
            ((r";v_failed integer:=0;", ';'),
             (r"  begin v:=runtime\.nexloop_experiment_guard\(t,x\.experiment_id\);exception when others then v_failed:=v_failed\+1;continue;end;[^\n]*", '  v:=runtime.nexloop_experiment_guard(t,x.experiment_id);'),
             (r",'failed',v_failed\);", ');'))),
    Allowed('nx043_wiring_review', 'control.nexloop_experiment_erasure', 'review 1.2: every cluster key of the erased Consumer is replaced; nothing else',
            ((r";v_keys text\[\];", ';'),
             (r"\n select array_agg\(distinct a\.cluster_key\) into v_keys[^\n]*", ''),
             (r"\(a\.cluster_key=new\.consumer_id or a\.cluster_key=any\(coalesce\(v_keys,'\{\}'\)\)\)", 'a.cluster_key=new.consumer_id'))),
    Allowed('nx043_wiring_review', 'runtime.nexloop_experiment_metrics', 'D5 (dispatcher): the cap counts the candidate increment, control spend reported apart; nothing else',
            ((r" and s\.arm is distinct from 'control'\),\n   'control_model_cny',\(select[^\n]*\),", '),'),)),
    Allowed('nx043_wiring_review', 'runtime.nexloop_experiment_spend', 'D5 (dispatcher): control rows never count toward the cap; nothing else',
            ((r" and arm is distinct from 'control';", ';'),)),
    # NX-043 pooled results (owner 2026-10-21): the registration result carries a notice (a single round under the minimum sample).
    Allowed('nx043_pooled_reports', 'control.nexloop_governed_register_experiment', 'NX-043: the result carries a notice; nothing else',
            ((r",'notice',runtime\.nexloop_experiment_notice\(x\.registration\)\)", ')'),)),
    # NX-043 X6 wiring (L3): the same-comparison leave-out, no row written.
    Allowed('nx043_same_comparison', 'runtime.nexloop_experiment_assign', 'X6 wiring: in_same_comparison left out without a row; nothing else',
            ((r"\n -- X6 wiring \(L3\):[^\n]*\n if exists\(select 1 from control\.nexloop_experiment_allocations o join[^\n]*'in_same_comparison','allocated',false\);end if;", ''),)),
    # NX-043 X5 review of d4de09d2: the caller's lock_timeout restored; the tick names the failing SQLSTATEs.
    Allowed('nx043_lock_timeout_restore', 'control.nexloop_experiment_budget_attribution', "review 2.1: the caller's lock_timeout restored after the block; nothing else",
            ((r";v_lock text:=current_setting\('lock_timeout'\);", ';'),
             (r"\n  perform set_config\('lock_timeout',v_lock,true\);[^\n]*", ''))),
    Allowed('nx043_lock_timeout_restore', 'authz.nexloop_experiment_guard_tick', 'review 3.2: the SQLSTATE of each failing experiment reported; nothing else',
            ((r";v_states text\[\]:='\{\}';v_state text;", ';'),
             (r"get stacked diagnostics v_state=returned_sqlstate;v_failed:=v_failed\+1;v_states:=v_states\|\|v_state;", 'v_failed:=v_failed+1;'),
             (r",'failed_sqlstates',to_jsonb\(v_states\)\);", ');'))),
    # NX-043 report leave-out: the same comparison also in the same world (as X4's in_same_comparison).
    Allowed('nx043_leave_out_world', 'runtime.nexloop_experiment_leave_out', 'the same comparison is also the same world; nothing else',
            ((r"\n\s*and o\.registration->>'world'=e\.registration->>'world'[^\n]*", ''),)),
    # NX-043 X4 wiring: the candidate strategy's levers (runtime.nexloop_experiment_levers) in the decision paths; nothing else.
    Allowed('nx043_x4_wiring', 'runtime.nexloop_operator_persona_section', 'the candidate strategy persona version (source unchanged)',
            ((r";v_lever jsonb;", ';'),
             (r"v_lever:=runtime\.nexloop_experiment_levers\(.*?else v_lever:=null;end if;", ''),
             (r"coalesce\(\(v_lever->>'version'\)::integer,(runtime\.nexloop_operator_persona_usable\(p_tenant,d\.persona_id\))\)", r'\1'))),
    Allowed('nx043_x4_wiring', 'runtime.nexloop_outreach_gates', 'the candidate strategy outreach_purposes lever, with its gate entry',
            ((r";v_lever jsonb;", ';'),
             (r"v_lever:=runtime\.nexloop_experiment_levers\(.*?'@'\|\|\(v_lever->>'version'\)\);\s*end if;", ''))),
    Allowed('nx043_x4_wiring', 'runtime.nexloop_outreach_candidates', 'the candidate strategy follow_up_delay_hours; others keep 24 hours',
            ((r";v_lever jsonb;", ';'),
             (r"interval '336 hours'", "interval '24 hours'"),
             (r"if x\.purpose='service_followup' then\s*v_lever:=.*?end if;\s*end if;", ''))),
    Allowed('nx043_x4_wiring', 'runtime.nexloop_outreach_accept', 'the decision\'s candidate strategy frozen in experiment_strategy',
            ((r"\n v_lever jsonb;  -- NX-043 X4 wiring", ''),
             (r"\n v_lever:=runtime\.nexloop_experiment_levers\(p_tenant,p_world,p_consumer\)-'levers';", ''),
             (r",state,experiment_strategy\)", ',state)'),
             (r" else 'accepted' end,v_lever\);", " else 'accepted' end);"))),
    # NX-029 erasure, owner 2026-10-21: keyed deterministic ids from now on (the legacy outreach id kept for replay).
    Allowed('erasure_keyed_ids', 'runtime.nexloop_outreach_accept', 'owner 2026-10-21: the outreach intent id through the keyed helper; nothing else',
            ((r" v_id:=runtime\.nexloop_outreach_intent_id\(p_tenant,p_world,p_consumer,p_purpose,p_origin,v_day\);[^\n]*",
              " v_id:='outreach:'||encode(sha256(convert_to(jsonb_build_array(p_tenant,p_consumer,p_purpose,p_origin,v_day)::text,'UTF8')),'hex');"),)),
    Allowed('erasure_keyed_ids', 'authz.nexloop_prepare_browser_identity_link', 'owner 2026-10-21: the request_id keyed; definer search_path gains pg_temp',
            ((r"'identity-link-'\|\|runtime\.nexloop_tenant_hmac\(owner\.tenant_id,owner\.tenant_id\|\|':'\|\|p_world\|\|':'\|\|owner\.consumer_id\|\|':'\|\|ref\),[^\n]*",
              "'identity-link-'||encode(sha256(convert_to(owner.tenant_id||':'||p_world||':'||owner.consumer_id||':'||ref,'UTF8')),'hex'),"),), PG_TEMP),
    # NX-043 online opened (owner 2026-10-21): start loses online_not_available; D9 waived online through one helper; nothing else.
    Allowed('nx043_online_open', 'control.nexloop_governed_start_experiment', 'online_not_available removed; D9 waived online with notice calibration_waived',
            ((r"v_persona jsonb;v_waived boolean;", 'v_persona jsonb;'),
             (r"\n if not runtime\.nexloop_experiment_twin_ok\(p_tenant,r\) then raise exception 'experiment: twin_not_calibrated' using errcode='22023';end if;\n v_waived:=.*?::integer\);",
              "\n if r->>'stage'='online' then raise exception 'experiment: online_not_available' using errcode='22023';end if;\n"
              " if r->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer) then\n"
              "  raise exception 'experiment: twin_not_calibrated' using errcode='22023';end if;"),
             (r"\s*\|\|case when v_waived then jsonb_build_object\('notice','calibration_waived'\) else '\{\}'::jsonb end", ''))),
    Allowed('nx043_online_open', 'runtime.nexloop_experiment_allocate_on_inbound', 'a record with direction inbound is inbound too',
            ((r"coalesce\(new\.record->>'direction','inbound'\)<>'inbound' then return null;end if;  -- inbound: no direction \(0046\) or 'inbound'",
              "new.record ? 'direction' then return null;end if;  -- a Consumer's own message carries no direction (0046)"),)),
    # NX-038 E3 wiring (option A): a Run is known to the record port by its Run credential from the same Source, too.
    Allowed('nx038_e3_wiring', 'authz.nexloop_semantic_access_record', 'a Run credential issued by this Source in this world also binds the Run',
            ((r"\n  and not exists\(select 1 from authz\.nexloop_run_credentials rc where .*?rc\.status='active'\) then", ' then'),)),
    # NX-038 E3 wiring (owner 2026-10-22, failure basis A): reply_fallback per message Run in the trace read; nothing else.
    Allowed('nx038_e3_wiring', 'runtime.nexloop_evo_trace_runs', 'reply_fallback: the message got the fallback reply Run',
            ((r",\s*'reply_fallback',p\.kind='message' and exists\(.*?m2\.run_id=p\.run_id\)\)", ')'),)),
    # World model visibility (stage 2026-10-22): object_fields answers readable=false for an object whose Consumer is being erased.
    Allowed('world_model_visibility', 'authz.nexloop_workbench_object_fields',
            "NX-029: also not readable when the object's Consumer is being erased (runtime.nexloop_read_consumer); nothing else",
            ((r"\n  -- World model visibility noise:[^\n]*\n  or runtime\.nexloop_consumer_erasing\(p_tenant,p_world,runtime\.nexloop_read_consumer\(.*?\)\)\) then", ' then'),)),
    # Erasing indirect read (L6 review 1-1): the guard and object_fields resolve an object's Consumers through one function.
    Allowed('erasing_indirect_read', 'runtime.nexloop_assert_not_erasing',
            'the target\'s Consumers through runtime.nexloop_object_consumers (any object, indirect links too); nothing else',
            ((r"v:=substring\(r from '\^eios:\(\?:object\|property\):\(\[A-Z\].*?p_claims->>'object_id'\);", "c:=substring(r from '^eios:(?:object|property):Consumer/([a-f0-9]{64})');if c is null and p_claims->>'type_name'='Consumer' then c:=p_claims->>'object_id';end if;if c is null then v:=substring(r from '^eios:(?:object|property):Conversation/([a-f0-9]{64})');if v is not null then select cv.consumer_id into c from runtime.nexloop_conversations cv where cv.tenant_id=t and cv.world=p_world and cv.conversation_id=v;else v:=substring(r from '^eios:(?:object|property):Message/([a-f0-9]{64})');if v is not null then select cv.consumer_id into c from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id where m.tenant_id=t and m.world=p_world and m.message_id=v limit 1;end if;end if;end if;"),
             (r"if v is not null and c is not null then\s*hit:=exists\(select 1 from control\.nexloop_consumer_erasures e where e\.tenant_id=t and e\.world=p_world "
              r"and e\.state<>'pending_confirmation'\s*and e\.consumer_id=any\(runtime\.nexloop_object_consumers\(t,p_world,v,c\)\)\);",
              "if c is not null then hit:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.consumer_id=c and e.state<>'pending_confirmation');"))),
    Allowed('erasing_indirect_read', 'authz.nexloop_workbench_object_fields',
            'the erasing check through runtime.nexloop_object_consumers (the read guard\'s resolution); nothing else',
            ((r"or exists\(select 1 from control\.nexloop_consumer_erasures e where .*?runtime\.nexloop_object_consumers\(p_tenant,p_world,p_type,p_object\)\)\)",
              "or runtime.nexloop_consumer_erasing(p_tenant,p_world,runtime.nexloop_read_consumer(p_world,jsonb_build_object('tenant_id',p_tenant,'type_name',p_type,'object_id',p_object,'target_resource','eios:object:'||p_type||'/'||p_object)))"),)),
    # One inbound rule (stage X7 S5, release 37): runtime.nexloop_message_is_inbound in the shadow copy and the inbound allocation.
    Allowed('message_inbound_rule', 'runtime.nexloop_shadow_copy_message', 'the inbound test through runtime.nexloop_message_is_inbound',
            ((r"not runtime\.nexloop_message_is_inbound\(new\.record\)", "new.record->>'direction' is distinct from 'inbound'"),)),
    Allowed('message_inbound_rule', 'runtime.nexloop_experiment_allocate_on_inbound', 'the inbound test through runtime.nexloop_message_is_inbound',
            ((r"not runtime\.nexloop_message_is_inbound\(new\.record\) then return null;end if;",
              "coalesce(new.record->>'direction','inbound')<>'inbound' then return null;end if;  -- inbound: no direction (0046) or 'inbound'"),)),
    Allowed('message_inbound_rule', 'runtime.nexloop_shadow_report_freeze', 'the report\'s real inbound set through runtime.nexloop_message_is_inbound',
            ((r"runtime\.nexloop_message_is_inbound\(m\.record\)\),", "m.record->>'direction'='inbound'),"),)),
    # NX-056 A1 (L6 C5): 0240's self-approval is compared by subject too; nothing else.
    Allowed('nx056_unified_approvals', 'control.nexloop_governed_outreach_approve', 'self-approval by subject as well as by principal',
            ((r"\n  or control\.nexloop_approval_subject_of\(p_tenant,p_principal\) in \(.*?\)\)\n then", ' then'),)),
    # NX-056 A2: the governed entry asks the unified Approval store before it calls the handler; nothing else.
    Allowed('nx056_approval_gate', 'authz.nexloop_goal_governed_action', 'the Approval gate before the handler, the approved call in a savepoint',
            ((r"\n v_gate jsonb;v_consumed text;  -- NX-056 A2", ''),
             (r"v_gate:=control\.nexloop_approval_gate\(.*?\n end if;\n v_id:=", "execute format('select %s($1,$2,$3,$4,$5,$6)',cap.handler::oid::regproc) into v_result using v_tenant,p_world,v_principal,v_subject,stored.intent_id,body;\n v_id:="),
             # the action claim ends permanent_failure when nothing ran or the handler refused (L6 A2 review important 4)
             (r"'outcome_revision',1,\n  -- Not executed.*?\n  'status',case when .*? end,", "'outcome_revision',1,'status','succeeded',"))),
    # NX-056 A3: the retention keeper's port also ends waiting approvals past due_by; nothing else.
    # NX-056 follow-up (L6 reviews 11-14): subject tenant locks in order; the list's count
    # leaves out a self vote that does not count now.
    Allowed('nx056_followup', 'control.nexloop_approval_eligibility_trigger', 'several tenant locks in tenant order',
            ((r"\n    order by m\.tenant_id loop  -- one order for several tenant locks", " loop"),)),
    Allowed('nx056_followup', 'runtime.nexloop_approval_view_row', 'a self vote counts in the list only while the mode takes effect',
            ((r"and \(d\.actor_subject<>r\.requester_subject\n.*?\n     or \(d\.self_approved and control\.nexloop_approval_solo_effective\(r\.tenant_id,r\.world,r\.capability,r\.policy_version,r\.requester_subject\)\)\)",
              "and (d.actor_subject<>r.requester_subject or d.self_approved)"),)),
    # NX-056 follow-up (day 1 drill, L6 S1): every view row says whether the approval was executed (consumption record).
    Allowed('approval_view_consumed', 'runtime.nexloop_approval_view_row', 'consumed and consumed_at from the consumption record',
            ((r"\n  \|\|coalesce\(\(select jsonb_build_object\('consumed',true.*?jsonb_build_object\('consumed',false,'consumed_at',null\)\)", ""),)),
    # NX-056 B1 (ADR-039 §6): target policies approved by owners only; every view row carries target_member; two alert metrics.
    Allowed('nx056_target_member', 'control.nexloop_approval_policy_guard', 'a target policy is approved by owners only',
            ((r" -- ADR-039 §6\.7 \(R1, B1\).*?\n  raise exception 'approval policy: a target policy is approved by owners only' using errcode='22023';end if;\n", "\n"),)),
    Allowed('nx056_target_member', 'runtime.nexloop_approval_view', 'each row carries target_member',
            ((r"\|\|jsonb_build_object\('target_member',runtime\.nexloop_approval_target_member\(p_tenant,r,pol,p_principal\)\) v,\n   r\.due_by", " v,r.due_by"),
             (r",\n    'target_member',runtime\.nexloop_approval_target_member\(p_tenant,r,pol,p_principal\)\) v,", ") v,"))),
    Allowed('nx056_target_member', 'runtime.nexloop_approval_metrics', 'first_time_approvers_24h and same_person_flags_24h',
            ((r"\n -- ADR-039 §6\.6 \(R1, B1\).*?'same_person_flags_24h',\(select count\(\*\) from runtime\.audit_events a where a\.tenant_id=p_tenant and a\.action='approval\.same_person_flagged'\n    and a\.created_at>clock_timestamp\(\)-interval '24 hours'\)\);", ""),)),
    # NX-056 B2 (ADR-039 §7): execution-time fields out of the digest and the frozen body; open-time quorum_unreachable; the reserved
    # erase_member branch; requester_no_longer_eligible; execution refusal codes.
    Allowed('nx056_execution_fields', 'control.nexloop_approval_digest', 'execution-time fields are not part of the digest',
            ((r"-control\.nexloop_approval_execution_fields_of\(p_capability\)\)", ")"),),
            (('volatility', 'immutable', 'stable'), ('security_definer', False, True))),
    Allowed('nx056_execution_fields', 'control.nexloop_approval_policy_guard', 'an execution-time field is never a summary, target or condition',
            ((r"\n if control\.nexloop_approval_execution_fields_of\(new\.capability\)&&.*?using errcode='22023';end if;", ""),)),
    Allowed('nx056_execution_fields', 'control.nexloop_approval_open', 'target statuses per capability, execution field check, quorum_unreachable at open, frozen body without execution-time fields',
            ((r";v_target text;v_from timestamptz\[\];", ";"),
             (r"and m\.payload->>'status'=any\(control\.nexloop_approval_target_statuses_of\(p_capability\)\)\) then",
              "and (m.payload->>'status' in ('invited','active') or (m.payload->>'status'='suspended' and p_capability<>'workbench_member.promote_owner'))) then"),
             (r"\n if exists\(select 1 from unnest\(control\.nexloop_approval_execution_fields_of\(p_capability\)\) f.*?execution_field_invalid' using errcode='22023';end if;", ""),
             (r"\n if pol\.target_subject_field is not null or pol\.solo_mode='not_applicable' then.*?\n  end if;\n end if;", ""),
             (r"p_body-control\.nexloop_approval_execution_fields_of\(p_capability\),", "p_body,"))),
    # budget_loosen (NX-056 budget approval, design §2): Budget.set's handler passes a set_budget body through the tighten-only check; a
    # loosening opens only against the current row it names; its policy may show the old and new limit and period.
    Allowed('budget_loosen', 'control.nexloop_governed_owner_change', 'a set_budget body passes the tighten-only check first; nothing else',
            ((r"control\.nexloop_budget_set_tighten_only\(p_tenant,p_world,body\)", "body"),)),
    Allowed('budget_loosen', 'control.nexloop_approval_summary_allowed', 'goals.budget.loosen shows the kind, the unit and the old and new limit and period; nothing else',
            ((r"\s*when 'goals\.budget\.loosen' then array\[[^\]]*\]", ''),)),
    Allowed('budget_loosen', 'control.nexloop_approval_open', 'a budget loosening opens only against the current row it names; nothing else',
            ((r"\n if p_capability='goals\.budget\.loosen' then perform control\.nexloop_budget_loosen_check\(p_tenant,p_world,p_body,false\);end if;", ''),)),
    # single_clock_issue (L6 S1 after CI seq 186): one clock reading per issued invitation, reset code, OIDC state and remediation.
    Allowed('single_clock_issue', 'control.nexloop_governed_invite_workbench_member', 'issued_at, expires_at and the audit time from one clock reading; nothing else',
            ((r"v_hours integer;v_now timestamptz;", "v_hours integer;"),
             (r"\n v_now:=clock_timestamp\(\);  -- one reading: the issue time, the expiry and the audit time are the same instant\n", "\n"),
             (r"p_principal,p_subject,v_now,\n   v_now\+make_interval", "p_principal,p_subject,clock_timestamp(),\n   clock_timestamp()+make_interval"),
             (r"'sso_provider',r\.sso_provider\),v_now\);", "'sso_provider',r.sso_provider),clock_timestamp());"))),
    Allowed('single_clock_issue', 'control.nexloop_governed_issue_workbench_owner_reset', 'issued_at, expires_at and the audit time from one clock reading; nothing else',
            ((r"v_approval uuid;v_now timestamptz;", "v_approval uuid;"),
             (r"\n v_now:=clock_timestamp\(\);  -- one reading: the issue time, the expiry and the audit time are the same instant\n", "\n"),
             (r"p_principal,p_subject,v_now,\n   v_now\+interval", "p_principal,p_subject,clock_timestamp(),\n   clock_timestamp()+interval"),
             (r"true\)\),v_now\);", "true)),clock_timestamp());"))),
    Allowed('single_clock_issue', 'control.nexloop_governed_invite_workbench_owner', 'issued_at, expires_at and the audit time from one clock reading; nothing else',
            ((r"v_approval uuid;v_now timestamptz;", "v_approval uuid;"),
             (r"\n v_now:=clock_timestamp\(\);  -- one reading: the issue time, the expiry and the audit time are the same instant\n", "\n"),
             (r"p_principal,p_subject,v_now,\n   v_now\+make_interval", "p_principal,p_subject,clock_timestamp(),\n   clock_timestamp()+make_interval"),
             (r"'approval_id',v_approval\),v_now\);", "'approval_id',v_approval),clock_timestamp());"))),
    Allowed('single_clock_issue', 'control.nexloop_governed_issue_workbench_reset', 'issued_at, expires_at and the audit time from one clock reading; nothing else',
            ((r"target_subject text;v_now timestamptz;", "target_subject text;"),
             (r"\n v_now:=clock_timestamp\(\);  -- one reading: the issue time, the expiry and the audit time are the same instant\n", "\n"),
             (r"p_principal,p_subject,v_now,\n   v_now\+interval", "p_principal,p_subject,clock_timestamp(),\n   clock_timestamp()+interval"),
             (r"'expires_at',r\.expires_at\),v_now\);", "'expires_at',r.expires_at),clock_timestamp());"))),
    Allowed('single_clock_issue', 'control.nexloop_workbench_oidc_begin', 'created_at and expires_at from one clock reading; nothing else',
            ((r"\ndeclare v_now timestamptz:=clock_timestamp\(\);  -- one reading: created_at and expires_at are the same instant\n", "\n"),
             (r"expires_at<v_now-interval", "expires_at<clock_timestamp()-interval"),
             (r"p_binding_hmac,v_now,v_now\+interval", "p_binding_hmac,clock_timestamp(),clock_timestamp()+interval"))),
    Allowed('single_clock_issue', 'authz.nexloop_remediation_plan_command', 'created_at, updated_at, due_by and the proposed event time from one clock reading; nothing else',
            ((r"x record;v_now timestamptz;", "x record;"),
             (r"\n    v_now:=clock_timestamp\(\);  -- one reading: created_at, due_by and the proposed event are the same instant\n", "\n"),
             (r"state,created_at,updated_at,due_by,", "state,due_by,"),
             (r"\n      v_now,v_now,v_now\+make_interval", "\n      clock_timestamp()+make_interval"),
             (r"actor_kind,actor,recorded_at\)", "actor_kind,actor)"),
             (r"'service',v_actor,v_now\);", "'service',v_actor);"))),
    # approval_single_now (CI seq 186): one clock reading per request - requested_at, due_by and the audit time.
    Allowed('approval_single_now', 'control.nexloop_approval_open', 'requested_at, due_by and the audit time from one clock reading; nothing else',
            ((r";v_now timestamptz;", ";"),
             (r"\n v_now:=clock_timestamp\(\);  -- one reading: requested_at, due_by and the audit time are the same instant\n", "\n"),
             (r",requester_principal,requested_at,due_by\)", ",requester_principal,due_by)"),
             (r"p_principal,v_now,v_now\+make_interval", "p_principal,clock_timestamp()+make_interval"),
             (r"'due_by',r\.due_by\),v_now\);", "'due_by',r.due_by),clock_timestamp());"))),
    Allowed('nx056_execution_fields', 'control.nexloop_approval_gate', 'the execution-time HMACs checked before the handler',
            ((r"\n if exists\(select 1 from unnest\(control\.nexloop_approval_execution_fields_of\(p_capability\)\) f.*?execution_field_invalid' using errcode='22023';end if;\n return jsonb_build_object\('proceed',true", "\n return jsonb_build_object('proceed',true"),)),
    Allowed('nx056_execution_fields', 'control.nexloop_approval_terminate', 'one more reason, requester_no_longer_eligible',
            ((r",'requester_no_longer_eligible'\)", ")"),)),
    Allowed('nx056_execution_fields', 'authz.nexloop_goal_governed_action', 'the handler\'s refusal code in the consumption result',
            ((r";v_reason text;", ";"),
             (r"\n   v_reason:=substring\(sqlerrm from '.*?'\);", ""),
             (r"\|\|coalesce\(':'\|\|v_reason,''\);", ";"),
             (r",'reason',v_reason\);", ");"))),
    # owner_reset_and_invite (NX-058 slice D3, ADR-039 R3 §8.4 option a): the claim's request digest may leave out the capability's
    # execution-time fields, checked after the capability is known instead of with the permit.
    Allowed('owner_reset_and_invite', 'authz.nexloop_goal_governed_action', "the claim digest over the body with or without the execution-time fields; nothing else",
            ((r"  or \(a->>'expires_at'\)::timestamptz>clock_timestamp\(\)\+interval '30 seconds' then\n",
              "  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'\n  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then\n"),
             (r"\n -- NX-058 slice D3 \(ADR-039 R3 §8\.4 option a.*?'goal permit rejected' using errcode='42501';end if;", ""))),
    Allowed('owner_reset_and_invite', 'control.nexloop_complete_workbench_reset', "the code's origin decides the target's standing (C-D3-1); nothing else",
            ((r"\s*-- NX-058 slice D3 \(L6 C-D3-1, S-D3b-1\)[^\n]*\n\s*--[^\n]*\n\s*or not control\.nexloop_workbench_member_active\(p_tenant,r\.target_principal,r\.approval_id is not null\)\n\s*or \(r\.approval_id is not null and not control\.nexloop_approval_executed_for\(p_tenant,r\.approval_id,'workbench_member\.issue_owner_reset'\)\) then",
              "\n  or not control.nexloop_workbench_member_active(p_tenant,r.target_principal,false) then"),)),
    # member_erasure (NX-058 slice E1): the deletion replay learns workbench_member; the approval view's erased (the staff erasure ledger,
    # an erased- invitee); the request guard's invite_owner username rewrite (ADR-039 R3 §8.6, §8.7).
    Allowed('member_erasure', 'runtime.nexloop_replay_deletions', 'one more kind, workbench_member (the staff erasure replayed)',
            ((r";n_member integer:=0;", ";"), (r",'sim_world','workbench_member'\)", ",'sim_world')"),
             (r"\n  elsif x->>'kind'='workbench_member' then\n.*?else n_skipped:=n_skipped\+1;end if;\n  elsif x->>'kind'='sim_world' then", "\n  elsif x->>'kind'='sim_world' then"),
             (r",'workbench_members',n_member", ""))),
    Allowed('member_erasure', 'runtime.nexloop_approval_target_member', 'erased by the staff erasure ledger and the erased- invitee; the pseudonym: placeholder dropped',
            ((r"\n   -- NX-058 slice E \(ADR-039 R3 §8\.6\): an invitee username the staff erasure rewrote is erased\.\n   if jsonb_typeof\(r\.body->'username'\)='string' and r\.body->>'username' like 'erased-%' then\n    v:=jsonb_build_object\('kind','erased'\);\n   elsif jsonb_typeof", "\n   if jsonb_typeof"),
             (r"   -- 4\. Erased \(ADR-039 R3 §8\.6, NX-058 slice E\)[^\n]*\n   if exists\(select 1 from control\.nexloop_workbench_member_erasures e where e\.tenant_id=p_tenant and e\.subject_id=m\.subject_id\) then",
              "   -- 4. Erased (a pseudonymised member identity; no staff erasure path exists yet, §6.5).\n   if m.principal_id like 'pseudonym:%' or m.subject_id like 'pseudonym:%' or coalesce(a.username,'') like 'pseudonym:%' then"))),
    Allowed('member_erasure', 'control.nexloop_approval_request_guard', "the staff erasure's invite_owner username rewrite; nothing else",
            ((r"\n    -- NX-058 slice E \(ADR-039 R3 §8\.7\)[^\n]*\n    or \(old\.capability='workbench_member\.invite_owner'.*?like 'workbench-erasure:%'\)\)\) then", ") then"),)),
    Allowed('member_erasure', 'control.nexloop_journal_on_deletion', 'the deletion entry carries its actor (the staff erasure executor)',
            ((r",'actor',new\.actor\)\);", "));"),)),
    Allowed('member_erasure', 'runtime.nexloop_restore_replay_before_contact_keys_v0240', 'the deletion entry keeps its actor; nothing else',
            ((r",'actor',e->'actor'\)\);  -- NX-058 slice E: the executor", "));"),)),
    Allowed('member_erasure', 'control.nexloop_initial_identity_create', "an erased member's receipt is refused (42501); nothing else",
            ((r"\n  -- NX-058 slice E: an erased member's initial account[^\n]*\n  if exists\(select 1 from control\.nexloop_workbench_member_erasures e[^\n]*\n   raise exception 'initial identity erased' using errcode='42501';end if;", ""),)),
    Allowed('member_login_clearing', 'authz.nexloop_retention_command', 'one more verb, member_login_clear (NX-058 slice E2)',
            ((r" elsif c->>'verb'='member_login_clear' then\n.*?\n elsif c->>'verb'='status'", " elsif c->>'verb'='status'"),)),
    Allowed('nx056_approval_expiry', 'authz.nexloop_retention_command', 'one more verb, approvals_expire (the expiry sweep)',
            ((r" elsif c->>'verb'='approvals_expire' then\n.*?\n elsif c->>'verb'='status'", " elsif c->>'verb'='status'"),)),
    # NX-054 P4a: the erasing readers resolve an object's Consumers as relations united with the property rule.
    Allowed('relation_read_union', 'runtime.nexloop_assert_not_erasing', 'Consumers through relations united with the property rule; nothing else',
            ((r"runtime\.nexloop_object_consumers_read\(", "runtime.nexloop_object_consumers("),)),
    Allowed('relation_read_union', 'authz.nexloop_workbench_object_fields', 'Consumers through relations united with the property rule; nothing else',
            ((r"runtime\.nexloop_object_consumers_read\(", "runtime.nexloop_object_consumers("),)),
    # NX-054 P4c: the purge also takes the objects the Consumer's ownership relations reach, frozen when the purge starts (L6 C-1).
    Allowed('relation_purge_union', 'runtime.nexloop_erasure_step',
            'the relation targets frozen before the first phase; references and commitments also take them; the consumer phase drops them; nothing else',
            ((r"\n -- NX-054 P4c \(L6 C-1\): the relation targets frozen[^\n]*\n perform runtime\.nexloop_erasure_freeze_relation_targets\(p_tenant,p_world,e\.request_id,e\.consumer_id\);", ""),
             (r"\n     or \(o\.type_name,o\.object_id\) in \(select r\.type_name,r\.object_id from control\.nexloop_erasure_relation_targets r\n      where r\.tenant_id=p_tenant and r\.world=p_world and r\.request_id=e\.request_id\)", ""),
             (r"\(properties->>'made_to'=e\.consumer_id\n    or object_id in \(select r\.object_id from control\.nexloop_erasure_relation_targets r\n     where r\.tenant_id=p_tenant and r\.world=p_world and r\.request_id=e\.request_id and r\.type_name='Commitment'\)\);",
              "properties->>'made_to'=e.consumer_id;"),
             (r"\n  delete from control\.nexloop_erasure_relation_targets where tenant_id=p_tenant and world=p_world and request_id=e\.request_id;", ""))),
    # I36 (owner 2026-10-31, L6 option A): the keeper's settle and the erasure share one status event; the completion trigger cancels
    # the Consumer's undated (or cancel-requested) open commitments before the registry rows go.
    Allowed('erasure_cancels_undated_commitments', 'authz.nexloop_commitment_command',
            'the status event through runtime.nexloop_commitment_status_event; I38: a missing registry row is P0002; nothing else',
            ((r"raise exception 'commitment registry row absent' using errcode='P0002';", "raise exception 'commitment unavailable' using errcode='42501';"),
             (r"perform runtime\.nexloop_commitment_status_event\(t,p_world,o,v_last,null\);",
              "insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)\n    values(t,p_world,v_subject,'status',jsonb_build_object('status',v_status,'from',v_last,'late',o.properties->'late','revision',o.nexloop_revision,\n     'fulfillment_evidence',o.properties->'fulfillment_evidence'));"),)),
    Allowed('erasure_cancels_undated_commitments', 'control.nexloop_consumer_reference_erasure',
            'before the registry delete, the undated or cancel-requested open commitments are cancelled through runtime.nexloop_commitment_cancel_apply and counted; dated open ones keep their registry row under the pseudonym (I36-c); nothing else',
            ((r"tn text:=new\.tenant_id;x record;", "tn text:=new.tenant_id;"),
             (r"\n -- I36: before the registry rows go.*?\n n:=n\|\|jsonb_build_object\('commitments_cancelled',c\);", ""),
             (r"\n -- I36-c \(owner\): a dated.*?\n perform runtime\.nexloop_erasure_open\('runtime\.nexloop_commitments',array\['consumer_id'\],true,v_ref\);.*?n:=n\|\|jsonb_build_object\('commitments_kept',c\);",
              "\n perform runtime.nexloop_erasure_open('runtime.nexloop_commitments',null,true,v_ref);"))),
    Allowed('erasure_cancels_undated_commitments', 'runtime.nexloop_commitment_registry_guard',
            'I36-c: inside a consumer erasure a registered row may become the kept, non-identifying shape; nothing else',
            ((r"\n -- I36-c: inside a consumer erasure.*?then return new;end if;", ""),)),
    Allowed('erasure_cancels_undated_commitments', 'runtime.nexloop_commitment_mark_plans',
            'L6 I36-c b: an erasing or erased Consumer gets no plan mark and no no_active_plan exception; nothing else',
            ((r"\n if runtime\.nexloop_consumer_erasing\(p_tenant,p_world,p_consumer\) then return 0;end if;", ""),)),
    # NX-054 P4a follow-up: the gap check says how many source types it covers (0: catalog not published).
    Allowed('relation_read_gaps_coverage', 'authz.nexloop_relation_read_gaps', 'returns ownership_types, the source types covered; nothing else',
            ((r"v_types integer;", ""),
             (r"\n select count\(distinct m\.source_type\) into v_types from control\.nexloop_relation_derivations m join control\.nexloop_relation_type_profiles f\n  on f\.tenant_id=m\.tenant_id and f\.relation_name=m\.relation_name and f\.version=m\.version where m\.tenant_id=p_tenant and f\.consumer_ownership is not null;", ""),
             (r"\n  'ownership_types',v_types,", ""))),
    # NX-054 §8: an erasure request marker ('erasure:<request id>', the commitments phase) is an erased value like a pseudonym.
    Allowed('relation_erased_values', 'ontology.nexloop_relation_derive_one', 'erased values through runtime.nexloop_relation_value_erased; nothing else',
            ((r"not runtime\.nexloop_relation_value_erased\(v\)", "v not like 'pseudonym:%'"),)),
    Allowed('relation_erased_values', 'authz.nexloop_relation_precheck', 'erased values through runtime.nexloop_relation_value_erased; nothing else',
            ((r"runtime\.nexloop_relation_value_erased\(o\.properties->>\(s->>'property'\)\)", "o.properties->>(s->>'property') like 'pseudonym:%'"),)),
    # NX-043 X7 item 10 (owner 2026-10-28, option A): the budget drill waives the twin's calibration for that drill only, and its notice.
    Allowed('nx043_budget_drill', 'runtime.nexloop_experiment_twin_ok', 'a budget drill (runtime.nexloop_experiment_budget_drill_ok) passes; nothing else',
            ((r"\n  or runtime\.nexloop_experiment_budget_drill_ok\(p_tenant,r\)", ""),)),
    Allowed('nx043_budget_drill', 'runtime.nexloop_experiment_notice', "a budget drill's registration gets the drill notice first; nothing else",
            ((r"when p_registration->'budget_drill'='true'::jsonb then runtime\.nexloop_experiment_budget_drill_notice\(\)\n  when ", "when "),)),
    # NX-058 slice F (owner 2026-10-28): an invitation may name the member SSO only; while pending no password and no reset code.
    Allowed('nx058_invite_sso', 'control.nexloop_governed_invite_workbench_member', 'the optional sso_provider: accepted, checked, stored, replay-compared, audited and returned; nothing else',
            ((r",'sso_provider'\)\)", "))"),
             (r"\s*or \(body \? 'sso_provider' and \(jsonb_typeof\(body->'sso_provider'\) is distinct from 'string' or body->>'sso_provider'!~'\^\[a-z\]\[a-z0-9_-\]\{0,31\}\$'\)\)", ""),
             (r",r\.sso_provider\) is distinct from \(body->>'username',body->>'role',body->>'token_hmac',body->>'sso_provider'\)", ") is distinct from (body->>'username',body->>'role',body->>'token_hmac')"),
             (r",'sso_provider',r\.sso_provider", ""),
             (r",state,sso_provider\)", ",state)"),
             (r",'issued',body->>'sso_provider'\)", ",'issued')"))),
    Allowed('nx058_invite_sso', 'control.nexloop_workbench_invitation_lookup', 'sso_only in the lookup result; nothing else',
            ((r",'sso_only',r\.sso_provider is not null\)", ")"),)),
    Allowed('nx058_invite_sso', 'control.nexloop_workbench_password_allowed', 'a member pending SSO (control.nexloop_workbench_sso_pending) is refused as sso_only; nothing else',
            ((r"\s*or exists\(select 1 from control\.nexloop_workbench_sso_pending where tenant_id=p_tenant and subject_id=p_subject\)", ""),
             (r"\bblocked\b", "found_link"))),
    # I26 (L6, S6 semantic reads): miss reasons and glossary hits on the access record; glossary aliases reported by source in recall;
    # the tokenizer constant in insert and scope; short property glosses kept out of the term list.
    Allowed('semantic_terms_reflow', 'authz.nexloop_semantic_access_record', 'miss_reason stored on an empty result; nothing else',
            ((r",miss_reason\)", ")"),
             (r",\s*case when jsonb_array_length\(v_refs\)=0 then nullif\(c->>'miss_reason',''\) end\) on conflict", ") on conflict"))),
    Allowed('glossary_recall', 'authz.nexloop_semantic_access_record', 'gloss_hits stored (an object or {}); nothing else',
            ((r",gloss_hits\)", ")"),
             (r",\s*case when jsonb_typeof\(c->'gloss_hits'\)='object' then c->'gloss_hits' else '\{\}'::jsonb end\) on conflict", ") on conflict"))),
    Allowed('glossary_recall', 'control.nexloop_recall_search', "a gloss row's field reads gloss_<source> (and pg_temp last in search_path); nothing else",
            ((r"case when e\.gloss_source is null then e\.field else 'gloss_'\|\|e\.gloss_source end field", "e.field"),), PG_TEMP),
    Allowed('recall_terms_round3', 'ontology.nexloop_recall_insert', 'the tokenizer version from ontology.nexloop_recall_tokenizer() (and pg_temp last in search_path); nothing else',
            ((r"ontology\.nexloop_recall_tokenizer\(\)", "'nexloop-cjk-bigram-v1'"),), PG_TEMP),
    Allowed('recall_terms_round3', 'ontology.nexloop_recall_scope', 'the tokenizer version from ontology.nexloop_recall_tokenizer() (and pg_temp last in search_path); nothing else',
            ((r"ontology\.nexloop_recall_tokenizer\(\)", "'nexloop-cjk-bigram-v1'"),), PG_TEMP),
    Allowed('recall_terms_round3', 'control.nexloop_recall_terms', 'property glosses shorter than 3 characters left out; nothing else',
            ((r"\s*and not \(e\.gloss_source is not null and e\.target_kind='property' and char_length\(btrim\(e\.body\)\)<3\)", ""),)),
    # I29 (0381): definition_read_rule, the relay Source's definition-level READ for the recall gate (stage-principals v9).
    Allowed('definition_read_rule', 'control.nexloop_configure_manifest', 'one more fact kind, definition_read_rule, and its exact shape; nothing else',
            ((r",'role_source_read_rule','definition_read_rule'\)", ",'role_source_read_rule')"),
             (r"\n  -- I29 \(0381\): definition_read_rule \[service principal\].*?raise exception 'definition read rule rejected' using errcode='22023';end if;", ""))),
    # R60 (stage 2026-10-31): a Consumer pseudonymized by the completed erasure (0305) settles as consumer_erased.
    Allowed('relay_backlog_pseudonym', 'control.nexloop_governed_relay_backlog_settle', "a 'pseudonym:' Consumer reads as erased; nothing else",
            ((r" or v_consumer like 'pseudonym:%'", ""),)),
    # I28: every reader of a pseudonymized row sees the erased Consumer as erasing; the outreach candidates skip an erasing Consumer.
    Allowed('erasing_pseudonym', 'runtime.nexloop_consumer_erasing', "a 'pseudonym:' id is erasing; nothing else",
            ((r"\s*if p_consumer like 'pseudonym:%' then return true;end if;", ""),)),
    Allowed('erasing_pseudonym', 'runtime.nexloop_outreach_candidates', 'an erasing Consumer is no candidate; nothing else',
            ((r"\s*continue when runtime\.nexloop_consumer_erasing\(p_tenant,p_world,x\.consumer_id\);", ""),)),
    # I31 (dispatcher 2026-10-31): a rotated credential keeps one id over two tokens, so a revocation names its token.
    Allowed('service_credential_rotation', 'control.nexloop_journal_on_service_credential', 'the revocation entry names the revoked token; nothing else',
            ((r",'token_digest',new\.token_digest", ""),)),
    Allowed('service_credential_rotation', 'runtime.nexloop_restore_replay_before_contact_keys_v0240',
            'a revocation entry naming a token revokes that token only; nothing else',
            ((r"\s*and \(not e \? 'token_digest' or token_digest=e->>'token_digest'\)", ""),)),
] + [Allowed('nx043_online_open', name, 'D9 waived online: the twin check through runtime.nexloop_experiment_twin_ok; nothing else', reverts)
     for name, reverts in (('runtime.nexloop_experiment_assign', TWIN_NEG), ('runtime.nexloop_experiment_strategy_for', TWIN_POS),
                           ('runtime.nexloop_experiment_guardrails_hit', TWIN_NEG), ('runtime.nexloop_experiment_guard', TWIN_NEG),
                           ('runtime.nexloop_experiment_levers', TWIN_POS))]



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
