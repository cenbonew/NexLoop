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


DENIAL = 'signature denial causes: the denial raise carries its cause (DETAIL) and the excess over the bound (HINT); nothing else'


def denial(name, message, header=()):
    return Allowed('signature_denial_causes', name, DENIAL + (' (and pg_temp last in search_path)' if header else ''),
                   ((r"raise exception '" + message + r"' using errcode='42501',detail=authz\.nexloop_signed_denial_cause\(.*?\),"
                     r"hint=(?:authz\.nexloop_signed_ahead_hint\([^)]*\)|'');", "raise exception '" + message + "' using errcode='42501';"),), header)


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
    # NX-042 T6 (D1): the twin's step text from the text layer (default deterministic template) instead of T3's fixed placeholder.
    Allowed('nx042_twin_text', 'authz.nexloop_twin_act', 'NX-042 T6: the step text from runtime.nexloop_twin_render_text and three text attributes; nothing else',
            ((r'v_result jsonb;v_text jsonb;', 'v_result jsonb;'),
             (r"\s*-- T6 text layer \(D1\)[^\n]*\n\s*v_text:=runtime\.nexloop_twin_render_text\(t\.generator_version,b\.seed\|\|'\|'\|\|b\.twin_digest\|\|'\|'\|\|p_consumer,p_step,v_choice\.behavior,v_choice\.kind\);", ''),
             (r"\(case when v_text->>'text' is not null then jsonb_build_object\('text',v_text->>'text'\) else '\{\}'::jsonb end\)",
              "(case when v_choice.kind='message' then jsonb_build_object('text','合成消息（孪生行为 '||v_choice.behavior||'）') else '{}'::jsonb end)"),
             (r"\s*\|\|case when v_text->>'text' is not null then jsonb_build_object\('text_template',v_text->>'template_id','text_generator',v_text->>'generator_version',\s*'text_marker','simulated'\) else '\{\}'::jsonb end\)",
              ')'))),
    # AT-048 (0286, L3, S6 stage finding): the evaluation opening also refuses when one Consumer has cases on both sides; nothing else.
    Allowed('at048_consumer_leakage', 'authz.nexloop_evo_evaluation_open', 'AT-048: leakage_detected also when the cases of both sides share a Consumer (one linear helper)',
            ((r"\s*-- AT-048 \(S6 stage finding\)[^\n]*\n\s*or runtime\.nexloop_evo_consumer_leak\(v_tenant,x\.design_case_ids,x\.validation_case_ids\)", ''),)),
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
