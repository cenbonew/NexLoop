-- Shadow copy of real inbound messages (stage X7 S5, release 37: copied 0): one inbound rule for every reader. The migration number lives
-- only in the file name and the catalog.
-- Every writer of runtime.nexloop_conversation_messages, as it writes the record:
--   * a Consumer's own message - authz.nexloop_conversation_command commit_message (0046, latest 0138): the caller must be the
--     conversation's owning principal (control.nexloop_consumer_owners); record {id, conversation_id, sequence, actor, body, accepted_at,
--     status}, no direction (all 118 real ones on stage);
--   * the agent's reply - the outbound recorder (0079): direction 'outbound', sender_kind agent;
--   * a person's reply in a takeover - staff reply (0127): direction 'outbound', sender_kind human_takeover.
-- So a message is inbound when its record says direction 'inbound' or says no direction, and carries the actor and body every
-- writer sets: every non-Consumer writer says 'outbound', and the only writer without a direction is the Consumer's own channel entry
-- (a record without actor or body - none is written - is not inbound). The record keeps its shape (the Consumer API returns
-- it); runtime.nexloop_message_is_inbound is the one rule. Before, the shadow copy (0288) required direction 'inbound' and so copied
-- nothing, while the inbound allocation (0309) took no direction as inbound.
-- Re-created from their latest bodies with only that test replaced (tests/test_function_copy_guard.py):
-- runtime.nexloop_shadow_copy_message (0288), runtime.nexloop_experiment_allocate_on_inbound (0309) and the shadow difference report's
-- inbound set, runtime.nexloop_shadow_report_freeze (0288; it too saw no real inbound message on stage).

create function runtime.nexloop_message_is_inbound(p_record jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_typeof(p_record)='object' and coalesce(p_record->>'direction','inbound')='inbound'
  and jsonb_typeof(p_record->'actor')='string' and jsonb_typeof(p_record->'body')='string',false)  -- never null: callers test not(...)
$$;
alter function runtime.nexloop_message_is_inbound(jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_message_is_inbound(jsonb) from public;

create or replace function runtime.nexloop_shadow_copy_message() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_world text;v_lock text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 -- Only a real inbound message (runtime.nexloop_message_is_inbound: a Consumer's own message carries no direction).
 if new.world<>'real' or not runtime.nexloop_message_is_inbound(new.record) then return null;end if;
 -- The switch first, outside any subtransaction (L6 fix review 2.2): with the copy off (D7 default) no savepoint is opened, so a bulk
 -- import never piles up subtransactions (pg_subtrans overflow past 64 per transaction).
 v_world:=runtime.nexloop_shadow_copy_target(new.tenant_id);
 if v_world is null then return null;end if;
 v_lock:=current_setting('lock_timeout');
 begin  -- a savepoint: whatever fails in the copy is rolled back here and never fails the real message (L6 review 1.2)
  -- A lock wait becomes a catchable 55P03 instead of running into the statement timeout (57014, which no handler catches).
  perform set_config('lock_timeout','200ms',true);
  perform set_config('eios.tenant_id',new.tenant_id,true);
  insert into runtime.nexloop_shadow_events(tenant_id,world,event_key,content_hash,copied_hour)
   values(new.tenant_id,v_world,runtime.nexloop_shadow_event_key(v_world,new.message_id),
    encode(sha256(convert_to(v_world||'|'||new.payload_digest,'UTF8')),'hex'),date_trunc('hour',clock_timestamp()))
   on conflict do nothing;
  perform set_config('lock_timeout',v_lock,true);  -- the real transaction's own lock timeout again (a failure rolls the setting back)
 exception when others then
  perform runtime.nexloop_shadow_copy_failed(new.tenant_id,sqlstate);
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;

create or replace function runtime.nexloop_experiment_allocate_on_inbound() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_consumer text;v_lock text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.world<>'real' or not runtime.nexloop_message_is_inbound(new.record) then return null;end if;
 if not runtime.nexloop_experiment_online_running(new.tenant_id) then return null;end if;  -- no subtransaction while none runs
 v_lock:=current_setting('lock_timeout');
 begin
  perform set_config('lock_timeout','200ms',true);
  perform set_config('eios.tenant_id',new.tenant_id,true);
  select cv.consumer_id into v_consumer from runtime.nexloop_conversations cv where cv.tenant_id=new.tenant_id and cv.world=new.world and cv.conversation_id=new.conversation_id;
  if v_consumer is not null then perform runtime.nexloop_experiment_strategy_for(new.tenant_id,'real',v_consumer,null);end if;
  perform set_config('lock_timeout',v_lock,true);
 exception when others then
  begin
   perform set_config('eios.tenant_id',new.tenant_id,true);
   insert into runtime.nexloop_experiment_accounting_failures(tenant_id,world,consumption_id,sqlstate) values(new.tenant_id,'real','allocation:inbound',sqlstate);
  exception when others then null;
  end;
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;

create or replace function runtime.nexloop_shadow_report_freeze(p_tenant text,p_world text,p_strategy text,p_version integer,p_since timestamptz,p_until timestamptz,p_by text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_min integer:=runtime.nexloop_shadow_minimum_consumers();v_report jsonb;v_id uuid;r runtime.nexloop_shadow_reports;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'')<>'shadow' or control.nexloop_sim_world_status(p_tenant,p_world) is null then
  raise exception 'shadow report: world_unknown' using errcode='22023';end if;
 if p_since is null or p_until is null or p_since>=p_until or coalesce(p_by,'')='' then raise exception 'shadow report: invalid' using errcode='22023';end if;
 -- L6 review 4.1 (as NX-042 T5): whole UTC days only, so two reports cannot differ by one hour's events; a window answers once (frozen).
 if p_since<>date_trunc('day',p_since at time zone 'UTC') at time zone 'UTC' or p_until<>date_trunc('day',p_until at time zone 'UTC') at time zone 'UTC' then
  raise exception 'shadow report: window_not_on_grid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 with dec as (
   select d.decision shadow,e.event_key from runtime.nexloop_shadow_decisions d join runtime.nexloop_shadow_events e
    on e.tenant_id=d.tenant_id and e.world=d.world and e.event_key=d.event_key
   where d.tenant_id=p_tenant and d.world=p_world and d.strategy_id=p_strategy and d.strategy_version=p_version
    and e.copied_hour>=date_trunc('hour',p_since) and e.copied_hour<p_until),
  inbound as (  -- the real inbound messages, keyed as the copy keyed them (identifiers stay inside this statement)
   select runtime.nexloop_shadow_event_key(p_world,m.message_id) event_key,cv.consumer_id,
    case when (select coalesce(n.record->>'direction','inbound') from runtime.nexloop_conversation_messages n where n.tenant_id=m.tenant_id and n.world=m.world
      and n.conversation_id=m.conversation_id and n.sequence>m.sequence order by n.sequence limit 1)='outbound' then 'reply' else 'none' end real
   from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
   where m.tenant_id=p_tenant and m.world='real' and runtime.nexloop_message_is_inbound(m.record)),
  joined as (select dec.shadow,i.real,i.consumer_id from dec join inbound i on i.event_key=dec.event_key),
  cells as (select shadow,real,count(*) events,count(distinct consumer_id) consumers from joined group by shadow,real)
 select jsonb_build_object(
   'schema','nexloop-shadow-report/1','world',p_world,'strategy',jsonb_build_object('strategy_id',p_strategy,'version',p_version),
   'window',jsonb_build_object('since',p_since,'until',p_until),'minimum_consumers',v_min,'data_mode','simulation',
   'conclusion_scope','observed in the shadow world only; not a real effect, not a causal estimate',
   'cells',coalesce((select jsonb_agg(jsonb_build_object('shadow',shadow,'real',real,'events',events,'consumers',consumers) order by shadow,real)
     from cells where consumers>=v_min),'[]'::jsonb),
   'suppressed_cells',case when exists(select 1 from cells where consumers<v_min) then 'some' else 'none' end,
   'total',case when (select count(distinct consumer_id) from joined)>=v_min then jsonb_build_object('events',(select count(*) from joined),
     'consumers',(select count(distinct consumer_id) from joined),
     'agreement_rate',round((select count(*) filter (where shadow=real)::numeric/nullif(count(*),0) from joined),4)) else null end,
   'unmatched_events',case when exists(select 1 from dec where not exists(select 1 from inbound i where i.event_key=dec.event_key)) then 'some' else 'none' end) into v_report;
 v_id:=md5(p_world||'|'||p_strategy||'|'||p_version||'|'||p_since::text||'|'||p_until::text)::uuid;
 select * into r from runtime.nexloop_shadow_reports where tenant_id=p_tenant and world=p_world and report_id=v_id;
 if not found then
  insert into runtime.nexloop_shadow_reports(tenant_id,world,report_id,strategy_id,strategy_version,window_since,window_until,minimum_consumers,report,report_digest,created_by)
   values(p_tenant,p_world,v_id,p_strategy,p_version,p_since,p_until,v_min,v_report,encode(sha256(convert_to(v_report::text,'UTF8')),'hex'),p_by)
   returning * into r;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return r.report||jsonb_build_object('report_id',r.report_id,'report_digest',r.report_digest,'frozen',true);
end $$;
