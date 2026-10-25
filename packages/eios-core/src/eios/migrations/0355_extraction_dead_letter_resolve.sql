-- Claim-extraction dead letters (stage 2026-10-26, docs/release/run-extraction-dead-letters-diagnosis.md): a way to deal with them and an
-- alert that counts only those still needing attention. Temporary number; the dispatcher renumbers. Published migrations are not edited.
--
-- Diagnosis: the alert data_health_extraction_dead_letters counted every dead-lettered claim-extraction job still in runtime.jobs, with
-- no window and nothing that could mark one dealt with; terminal jobs leave after 365 days (retention job_records). The five on stage
-- came from the model provider's early outage (10-15 to 10-17) and were long covered by later extraction windows; the alert began
-- with 0245 (10-21), not with the incident.
-- 1. runtime.nexloop_claim_extraction_dispositions: one decision per dead letter - requeue or dismiss, with a reason, the decider and
--    the governed intent. Append-only, owner only, forced RLS. The dead-letter row itself is never changed (it stays as evidence).
-- 2. Governed human capability claim_extraction.dead_letter_resolve (operation resolve_extraction_dead_letter; owner only by the
--    workbench role manifest): requeue puts the feed rows that job covered back as pending (task_id null), so the scheduler's normal
--    window extracts those Messages again; dismiss records the reason only. Refused for anything but a dead-lettered claim-extraction
--    job of the caller's tenant and world; one decision per job (the same one replays).
-- 3. runtime.nexloop_data_health is renamed once and wrapped: the extraction block gains
--      dead_lettered_total       every dead letter still in runtime.jobs (the old value; 'dead_lettered' stays as an alias for readers)
--      unresolved_dead_letters   dead letters with no decision and not fully covered by a later succeeded job of the same conversation
--                                (its message_ids contain all of them - parenthesised: (s.normalized_input->'message_ids') @> ...)
--      dead_letter_items         those unresolved ones, at most 20, ids and codes only (no Message text), for the workbench list
--    and the reason dead_letters follows unresolved_dead_letters. Alert rules v17 move the rule to unresolved_dead_letters.

create table runtime.nexloop_claim_extraction_dispositions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,job_id text not null,
 disposition text not null check(disposition in ('requeue','dismiss')),reason text not null check(char_length(btrim(reason)) between 1 and 500),
 decided_by text not null,intent_id text not null,requeued_feed_rows integer not null default 0 check(requeued_feed_rows>=0),
 decided_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,job_id)
);
alter table runtime.nexloop_claim_extraction_dispositions owner to nexloop_owner;
alter table runtime.nexloop_claim_extraction_dispositions enable row level security;
alter table runtime.nexloop_claim_extraction_dispositions force row level security;
create policy tenant_boundary on runtime.nexloop_claim_extraction_dispositions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_claim_extraction_dispositions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function runtime.nexloop_claim_extraction_dispositions_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'claim extraction dispositions are append-only' using errcode='42501';end $$;
alter function runtime.nexloop_claim_extraction_dispositions_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_claim_extraction_dispositions_append_only() from public;
create trigger claim_extraction_dispositions_append_only before update or delete or truncate on runtime.nexloop_claim_extraction_dispositions
 for each statement execute function runtime.nexloop_claim_extraction_dispositions_append_only();

create function runtime.nexloop_governed_resolve_extraction_dead_letter(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare j runtime.jobs;prior runtime.nexloop_claim_extraction_dispositions;n integer:=0;
begin
 if jsonb_typeof(body) is distinct from 'object' or body->>'operation' is distinct from 'resolve_extraction_dead_letter'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','task_id','disposition','reason'))
  or jsonb_typeof(body->'task_id') is distinct from 'string' or char_length(body->>'task_id') not between 1 and 200
  or coalesce(body->>'disposition','') not in ('requeue','dismiss')
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'extraction dead letter: invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'extraction dead letters are owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into j from runtime.jobs where tenant_id=p_tenant and world=p_world and queue='claim-extraction' and job_id=body->>'task_id' for update;
 if not found then raise exception 'extraction dead letter: not_found' using errcode='22023';end if;
 if j.status<>'dead_lettered' then raise exception 'extraction dead letter: not_dead_lettered' using errcode='22023';end if;
 select * into prior from runtime.nexloop_claim_extraction_dispositions where tenant_id=p_tenant and world=p_world and job_id=j.job_id;
 if found then
  if prior.disposition<>body->>'disposition' then raise exception 'extraction dead letter: already_resolved' using errcode='22023';end if;
  return jsonb_build_object('task_id',j.job_id,'disposition',prior.disposition,'requeued_feed_rows',prior.requeued_feed_rows,'replayed',true);
 end if;
 if body->>'disposition'='requeue' then
  -- The feed rows this job covered go back to pending: the scheduler's next window for the conversation extracts those Messages again.
  update runtime.nexloop_claim_extraction_feed set task_id=null,enqueued_at=null where tenant_id=p_tenant and world=p_world and task_id=j.job_id;
  get diagnostics n=row_count;
 end if;
 insert into runtime.nexloop_claim_extraction_dispositions(tenant_id,world,job_id,disposition,reason,decided_by,intent_id,requeued_feed_rows)
  values(p_tenant,p_world,j.job_id,body->>'disposition',btrim(body->>'reason'),p_principal,p_intent,n);
 return jsonb_build_object('task_id',j.job_id,'disposition',body->>'disposition','requeued_feed_rows',n,'replayed',false);
end $$;
alter function runtime.nexloop_governed_resolve_extraction_dead_letter(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_resolve_extraction_dead_letter(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('claim_extraction.dead_letter_resolve','resolve_extraction_dead_letter','human','runtime.nexloop_governed_resolve_extraction_dead_letter(text,text,text,text,text,jsonb)','NX-019');

-- 3. The data health block.
alter function runtime.nexloop_data_health(text,text) rename to nexloop_data_health_before_dead_letter_resolve;
revoke all on function runtime.nexloop_data_health_before_dead_letter_resolve(text,text) from public;
create function runtime.nexloop_data_health(p_tenant text,p_world text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare h jsonb:=runtime.nexloop_data_health_before_dead_letter_resolve(p_tenant,p_world);x jsonb;v_unresolved bigint;v_items jsonb;reasons jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with dead as (
  select j.job_id,j.attempts,j.max_attempts,j.result->>'code' code,j.normalized_input->>'conversation_id' conversation_id,
   jsonb_array_length(coalesce(j.normalized_input->'message_ids','[]'::jsonb)) message_count,j.created_at,j.updated_at
  from runtime.jobs j where j.tenant_id=p_tenant and j.world=p_world and j.queue='claim-extraction' and j.status='dead_lettered'
   and not exists(select 1 from runtime.nexloop_claim_extraction_dispositions d where d.tenant_id=j.tenant_id and d.world=j.world and d.job_id=j.job_id)
   and not exists(select 1 from runtime.jobs s where s.tenant_id=j.tenant_id and s.world=j.world and s.queue='claim-extraction' and s.status='succeeded'
    and s.normalized_input->>'conversation_id'=j.normalized_input->>'conversation_id' and s.created_at>j.created_at
    and (s.normalized_input->'message_ids') @> (j.normalized_input->'message_ids')))
 select count(*),coalesce((select jsonb_agg(jsonb_build_object('task_id',x2.job_id,'code',x2.code,'attempts',x2.attempts,'max_attempts',x2.max_attempts,
   'conversation_id',x2.conversation_id,'message_count',x2.message_count,'created_at',x2.created_at,'dead_lettered_at',x2.updated_at) order by x2.updated_at desc)
   from (select * from dead order by updated_at desc limit 20) x2),'[]'::jsonb)
  into v_unresolved,v_items from dead;
 x:=h->'extraction';
 -- The reason dead_letters follows the unresolved count; the others are kept.
 reasons:=coalesce((select jsonb_agg(r) from jsonb_array_elements_text(x->'reasons') r where r<>'dead_letters'),'[]'::jsonb);
 if v_unresolved>0 then reasons:='["dead_letters"]'::jsonb||reasons;end if;
 x:=x||jsonb_build_object('dead_lettered_total',x->'dead_lettered','unresolved_dead_letters',v_unresolved,'dead_letter_items',v_items,'reasons',reasons,
  'status',case when jsonb_array_length(reasons)=0 then 'ok' else 'attention' end);
 return jsonb_set(h,'{extraction}',x);
end $$;
alter function runtime.nexloop_data_health(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_data_health(text,text) from public;
