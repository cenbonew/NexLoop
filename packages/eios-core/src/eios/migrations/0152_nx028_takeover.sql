-- NX-028 slice 3 (temporary number 0152; design placeholder 0118): human takeover of a conversation or a consumer (AT-044,
-- D3–D6, owner 2026-10-10 "all as recommended").
--
-- 1. Versioned policy (deploy/configuration/takeover.v1.json, byte-equal canonical JSON; deployment configuration without a
--    tenant dimension, like 0109 reply_policies): default 2 h, at most 24 h.
-- 2. control.nexloop_takeovers: one active takeover per scope (conversation or consumer); start and hand-back are human
--    Actions registered in the 0150 governed entry (conversation.takeover / conversation.handback). Both advance the control
--    revision with a consumer-scoped control event (takeover / handback), so queued Agent work is stale (NXC02) and NX-024 T3
--    marks the consumer's plans for reevaluation on hand-back. Active means: not ended and not past expires_at (the check
--    never waits for the expiry worker).
-- 3. The Agent does not compete while taken over:
--    * dispatch: control.nexloop_contact_assert_intent (0112) is kept as a private alias and wrapped; an intent of the
--      consumer (consumer scope) or an Agent reply in the conversation (conversation scope) is refused, NXC06 taken_over;
--    * the message relay may not issue a message Run for a message in a taken-over conversation (0049 command kept as a
--      private alias, wrapper refuses 'issue' with NXC06; the item is retried later);
--    * no fallback reply Run either (0110 command wrapped the same way); the reply guarantee port reports taken_over and the
--      reply worker waits instead of escalating.
-- 4. End (hand-back or expiry), D5: of the inbound messages accepted during the takeover that are still unanswered, only the
--    latest returns to normal pending-reply handling (due again after the fallback start time) and stays routable; earlier
--    ones are recorded as settled by the takeover (their pending reply and relay item closed) and never replayed. Expiry is
--    processed by the reply guarantor (takeover-expiry feed, nexloop.takeover.expire) and escalates to the owner.
-- Plans: 0106 trigger body; a takeover start wakes nothing, a hand-back marks the consumer's plans ('handback').
create or replace function runtime.nexloop_plan_on_control_event() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;kind text;
begin
 -- NX-028: a takeover start does not wake plans (a person answers now); its hand-back does (D5: reevaluate, never replay).
 if new.event_kind='takeover' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 kind:=case new.event_kind when 'pause' then 'control_paused' when 'resume' then 'resume' when 'handback' then 'handback' when 'goal_version' then 'goal_version_stale'
  when 'agent_goal' then 'goal_version_stale' else 'control_revision' end;
 for r in select p.plan_id from runtime.nexloop_active_plans(new.tenant_id,new.world) p
  where new.scope_kind='tenant'
   or (new.scope_kind='goal' and p.recipe->'goal_chain' ? new.scope_ref)
   or (new.scope_kind='budget' and p.control_snapshot->'budgets' ? new.scope_ref)
   or exists(select 1 from jsonb_array_elements(p.control_snapshot->'scopes') s where s->>'kind'=new.scope_kind and s->>'ref'=new.scope_ref) loop
  perform authz.nexloop_plan_feed_touch(new.tenant_id,new.world,r.plan_id,
   jsonb_build_object('kind',kind,'cause','external','control_revision',new.revision,'at',clock_timestamp()),null);
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;

-- 5. Customer view (D4): authz.nexloop_conversation_takeover_state, for the conversation's own browser Human only.
-- Staff replies (message.staff_send) are not in this migration (dispatcher ruling pending on the delivery path).

-- 1. Policy -------------------------------------------------------------------------------------------------------
create table control.nexloop_takeover_policies (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check((definition->>'default_seconds')::integer between 60 and (definition->>'max_seconds')::integer),
 check((definition->>'max_seconds')::integer between 60 and 604800)
);
alter table control.nexloop_takeover_policies owner to nexloop_owner;
create trigger nx028_append_only before update or delete on control.nexloop_takeover_policies for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_takeover_policies from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_takeover_policies(version,definition,definition_digest,published_by)
 values(1,'{"decision":"NX-028 D3–D5 (owner 2026-10-10, \"all as recommended\"): a staff member takes over one conversation (or one whole consumer); default 2 hours, at most 24 hours. While taken over the Agent does not compete: no message Run, no fallback reply Run, Agent outbound refused at dispatch (NXC06). Expiry ends it automatically and escalates to the owner. On hand-back or expiry only the latest still unanswered inbound message returns to normal pending-reply handling; earlier ones are recorded as settled by the takeover and never replayed; plan reevaluation takes it from there. Initial values; owner-adjustable by a new version.","default_seconds":7200,"max_seconds":86400,"schema":"nexloop-takeover/1","version":1,"worker":{"batch":20,"lease_seconds":60}}'::jsonb,'20cbadcf478167fe436eb47465ea191314d106e46ccce991b244130d52fd6c5e','deploy/configuration/takeover.v1.json');
create function control.nexloop_takeover_setting(p_key text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select (definition->>p_key)::integer from control.nexloop_takeover_policies order by version desc limit 1
$$;
alter function control.nexloop_takeover_setting(text) owner to nexloop_owner;
revoke all on function control.nexloop_takeover_setting(text) from public;

-- 2. State --------------------------------------------------------------------------------------------------------
create table control.nexloop_takeovers (
 tenant_id text not null,world text not null,takeover_id uuid primary key,
 scope_kind text not null check(scope_kind in ('conversation','consumer')),scope_ref text not null check(scope_ref~'^[a-f0-9]{64}$'),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),taken_by text not null,start_intent text not null,reason text not null check(length(reason) between 1 and 500),
 started_at timestamptz not null,expires_at timestamptz not null,start_revision bigint not null,
 ended_at timestamptz,end_reason text check(end_reason in ('handback','expired')),ended_by text,end_intent text,end_revision bigint,
 unique(tenant_id,world,start_intent),check(expires_at>started_at),
 check((ended_at is null)=(end_reason is null) and (end_reason is null)=(end_revision is null))
);
create unique index nexloop_takeovers_one_open on control.nexloop_takeovers(tenant_id,world,scope_kind,scope_ref) where ended_at is null;
create index nexloop_takeovers_consumer on control.nexloop_takeovers(tenant_id,world,consumer_id) where ended_at is null;
-- What happened to each unanswered inbound message of a takeover when it ended (D5).
create table control.nexloop_takeover_settlements (
 tenant_id text not null,world text not null,takeover_id uuid not null references control.nexloop_takeovers(takeover_id),
 message_id text not null,disposition text not null check(disposition in ('settled_by_takeover','resumed')),recorded_at timestamptz not null default clock_timestamp(),
 primary key(takeover_id,message_id)
);
-- Owner-visible record of a takeover that expired without hand-back.
create table control.nexloop_takeover_escalations (
 tenant_id text not null,world text not null,takeover_id uuid primary key references control.nexloop_takeovers(takeover_id),
 reason text not null check(reason~'^[a-z_]{1,64}$'),detail jsonb not null check(jsonb_typeof(detail)='object'),escalated_at timestamptz not null default clock_timestamp()
);
do $tables$
declare t text;
begin
 foreach t in array array['nexloop_takeovers','nexloop_takeover_settlements','nexloop_takeover_escalations'] loop
  execute format('alter table control.%I owner to nexloop_owner',t);
  execute format('alter table control.%I enable row level security',t);
  execute format('alter table control.%I force row level security',t);
  execute format('create policy tenant_boundary on control.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on control.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;
create trigger nx028_append_only before update or delete on control.nexloop_takeover_settlements for each row execute function control.nexloop_nx022_append_only();
create trigger nx028_append_only before update or delete on control.nexloop_takeover_escalations for each row execute function control.nexloop_nx022_append_only();
-- A takeover is immutable except its one ending.
create function control.nexloop_takeover_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' or old.ended_at is not null or new.ended_at is null
  or (to_jsonb(new)-'ended_at'-'end_reason'-'ended_by'-'end_intent'-'end_revision') is distinct from (to_jsonb(old)-'ended_at'-'end_reason'-'ended_by'-'end_intent'-'end_revision') then
  raise exception 'takeover record is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_takeover_guard() owner to nexloop_owner;
revoke all on function control.nexloop_takeover_guard() from public;
create trigger nx028_takeover_guard before update or delete on control.nexloop_takeovers for each row execute function control.nexloop_takeover_guard();

alter table control.nexloop_control_events drop constraint nexloop_control_events_event_kind_check;
alter table control.nexloop_control_events add constraint nexloop_control_events_event_kind_check
 check(event_kind in ('pause','resume','budget','goal_version','agent_goal','metric','contact_restricted','contact_released','takeover','handback'));
alter table runtime.nexloop_work_feed drop constraint nexloop_work_feed_feed_check;
alter table runtime.nexloop_work_feed add constraint nexloop_work_feed_feed_check
 check(feed in ('recall-instance','claim-match','plan-reevaluate','reply-due','commitment-register','commitment-monitor','takeover-expiry'));

-- The active takeover covering a consumer or one of its conversations (null: none). Never waits for expiry processing.
create function control.nexloop_takeover_active(p_tenant text,p_world text,p_consumer text,p_conversation text) returns uuid
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v uuid;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select x.takeover_id into v from control.nexloop_takeovers x where x.tenant_id=p_tenant and x.world=p_world and x.ended_at is null and x.expires_at>clock_timestamp()
  and ((x.scope_kind='consumer' and x.scope_ref=p_consumer) or (x.scope_kind='conversation' and x.scope_ref=p_conversation))
  order by x.started_at limit 1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_takeover_active(text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_takeover_active(text,text,text,text) from public;

-- 4. End: D5 settlement, control event (plans reevaluate), record --------------------------------------------------
create function control.nexloop_takeover_end(p_tenant text,p_world text,p_takeover uuid,p_reason text,p_principal text,p_intent text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);x control.nexloop_takeovers;m record;v_latest text;rev bigint;n_settled integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into x from control.nexloop_takeovers where takeover_id=p_takeover and tenant_id=p_tenant and world=p_world for update;
 if not found or x.ended_at is not null then raise exception 'takeover not open' using errcode='22023';end if;
 -- Inbound messages accepted during the takeover that no reply answered (channel-accepted), newest first.
 for m in select cm.message_id,cm.conversation_id,cv.consumer_id,(cm.record->>'accepted_at')::timestamptz accepted_at
   from runtime.nexloop_conversation_messages cm join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
  where cm.tenant_id=p_tenant and cm.world=p_world and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%'
   and (cm.record->>'accepted_at')::timestamptz>=x.started_at
   and ((x.scope_kind='conversation' and cm.conversation_id=x.scope_ref) or (x.scope_kind='consumer' and cv.consumer_id=x.scope_ref))
   and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=cm.tenant_id and o.world=cm.world and o.trigger_message_id=cm.message_id
     and o.delivery_state in ('provider_accepted','delivered'))
  order by (cm.record->>'accepted_at')::timestamptz desc,cm.sequence desc loop
  if v_latest is null then
   -- The latest one is a pending reply again (normal handling resumes; its relay item stays routable).
   v_latest:=m.message_id;
   insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload,available_at)
    values(p_tenant,p_world,'reply-due','reply:'||m.message_id,jsonb_build_object('message_id',m.message_id,'conversation_id',m.conversation_id,'consumer_id',m.consumer_id),
     clock_timestamp()+make_interval(secs=>control.nexloop_reply_fallback_after_seconds()))
   on conflict(tenant_id,world,feed,item_key) do update set available_at=excluded.available_at,change_seq=f.change_seq+1,changed_at=clock_timestamp(),
    attempts=0,status='pending',last_code=null;
   insert into control.nexloop_takeover_settlements(tenant_id,world,takeover_id,message_id,disposition) values(p_tenant,p_world,p_takeover,m.message_id,'resumed');
  else
   -- Earlier ones were the person's to answer: settled by the takeover, never replayed.
   delete from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='reply-due' and item_key='reply:'||m.message_id;
   update runtime.nexloop_message_outbox set status='delivered' where tenant_id=p_tenant and world=p_world and message_id=m.message_id and status='pending';
   insert into control.nexloop_takeover_settlements(tenant_id,world,takeover_id,message_id,disposition) values(p_tenant,p_world,p_takeover,m.message_id,'settled_by_takeover');
   n_settled:=n_settled+1;
  end if;
 end loop;
 rev:=control.nexloop_nx022_bump(p_tenant,p_world,'handback','consumer',x.consumer_id,
  jsonb_build_object('takeover_id',p_takeover,'scope_kind',x.scope_kind,'scope_ref',x.scope_ref,'reason',p_reason,'resumed',v_latest,'settled',n_settled),p_principal,p_intent);
 update control.nexloop_takeovers set ended_at=clock_timestamp(),end_reason=p_reason,ended_by=p_principal,end_intent=p_intent,end_revision=rev where takeover_id=p_takeover;
 delete from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='takeover-expiry' and item_key='takeover:'||p_takeover;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('takeover_id',p_takeover,'end_reason',p_reason,'control_revision',rev,'resumed_message_id',v_latest,'settled',n_settled);
end $$;
alter function control.nexloop_takeover_end(text,text,uuid,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_takeover_end(text,text,uuid,text,text,text) from public;

-- Human Actions through the 0150 registry --------------------------------------------------------------------------
create function control.nexloop_governed_takeover(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_consumer text;v_seconds integer;v_id uuid:=gen_random_uuid();rev bigint;v_expires timestamptz;
begin
 if body->>'operation' is distinct from 'take_over_conversation' or body->>'scope_kind' not in ('conversation','consumer')
  or coalesce(body->>'scope_ref','')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500
  or (body ? 'duration_seconds' and jsonb_typeof(body->'duration_seconds') is distinct from 'number') then
  raise exception 'takeover request invalid' using errcode='22023';end if;
 v_seconds:=coalesce((body->>'duration_seconds')::integer,control.nexloop_takeover_setting('default_seconds'));
 if v_seconds<60 or v_seconds>control.nexloop_takeover_setting('max_seconds') then raise exception 'takeover duration outside policy' using errcode='22023';end if;
 if body->>'scope_kind'='conversation' then
  select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=p_tenant and world=p_world and conversation_id=body->>'scope_ref';
 else
  select object_id into v_consumer from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=body->>'scope_ref';
 end if;
 if v_consumer is null then raise exception 'takeover target unavailable' using errcode='22023';end if;
 -- One person at a time: a consumer takeover conflicts with any open takeover of that consumer; a conversation takeover
 -- with a consumer takeover or the same conversation.
 if exists(select 1 from control.nexloop_takeovers x where x.tenant_id=p_tenant and x.world=p_world and x.ended_at is null and x.expires_at>clock_timestamp()
   and ((body->>'scope_kind'='consumer' and x.consumer_id=v_consumer) or (x.scope_kind='consumer' and x.consumer_id=v_consumer)
    or (x.scope_kind='conversation' and x.scope_ref=body->>'scope_ref'))) then
  raise exception 'already taken over' using errcode='22023';end if;
 -- An expired but not yet processed takeover of the same scope is ended first (expiry is never skipped).
 perform control.nexloop_takeover_end(p_tenant,p_world,x.takeover_id,'expired',p_principal,p_intent||':expired')
  from control.nexloop_takeovers x where x.tenant_id=p_tenant and x.world=p_world and x.ended_at is null and x.scope_kind=body->>'scope_kind' and x.scope_ref=body->>'scope_ref';
 v_expires:=clock_timestamp()+make_interval(secs=>v_seconds);
 rev:=control.nexloop_nx022_bump(p_tenant,p_world,'takeover','consumer',v_consumer,
  jsonb_build_object('takeover_id',v_id,'scope_kind',body->>'scope_kind','scope_ref',body->>'scope_ref','reason',body->>'reason','expires_at',v_expires),p_principal,p_intent);
 insert into control.nexloop_takeovers(tenant_id,world,takeover_id,scope_kind,scope_ref,consumer_id,taken_by,start_intent,reason,started_at,expires_at,start_revision)
  values(p_tenant,p_world,v_id,body->>'scope_kind',body->>'scope_ref',v_consumer,p_principal,p_intent,body->>'reason',clock_timestamp(),v_expires,rev);
 insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,available_at)
  values(p_tenant,p_world,'takeover-expiry','takeover:'||v_id,jsonb_build_object('takeover_id',v_id),v_expires);
 return jsonb_build_object('takeover_id',v_id,'scope_kind',body->>'scope_kind','scope_ref',body->>'scope_ref','consumer_id',v_consumer,'expires_at',v_expires,'control_revision',rev);
end $$;
create function control.nexloop_governed_handback(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if body->>'operation' is distinct from 'hand_back_conversation' or coalesce(body->>'takeover_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'handback request invalid' using errcode='22023';end if;
 return control.nexloop_takeover_end(p_tenant,p_world,(body->>'takeover_id')::uuid,'handback',p_principal,p_intent);
end $$;
do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_governed_takeover(text,text,text,text,text,jsonb)','control.nexloop_governed_handback(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('conversation.takeover','take_over_conversation','human','control.nexloop_governed_takeover(text,text,text,text,text,jsonb)','NX-028'),
 ('conversation.handback','hand_back_conversation','human','control.nexloop_governed_handback(text,text,text,text,text,jsonb)','NX-028');

-- Expiry port (reply guarantor; eios:action:nexloop.takeover.expire:1): ends an expired takeover and escalates it.
create function authz.nexloop_takeover_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-takeover-expire-v1','eios:action:nexloop.takeover.expire:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;x control.nexloop_takeovers;a jsonb:=p_text::jsonb;v_result jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb' is distinct from 'expire' or coalesce(c->>'takeover_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  raise exception 'takeover command invalid' using errcode='22023';end if;
 select * into x from control.nexloop_takeovers where takeover_id=(c->>'takeover_id')::uuid and tenant_id=t and world=p_world for update;
 if not found then raise exception 'takeover unavailable' using errcode='42501';end if;
 if x.ended_at is not null then return jsonb_build_object('takeover_id',x.takeover_id,'status','ended');end if;
 if x.expires_at>clock_timestamp() then return jsonb_build_object('takeover_id',x.takeover_id,'status','active','expires_at',x.expires_at);end if;
 v_result:=control.nexloop_takeover_end(t,p_world,x.takeover_id,'expired',a->>'principal_id','expiry:'||x.takeover_id);
 insert into control.nexloop_takeover_escalations(tenant_id,world,takeover_id,reason,detail)
  values(t,p_world,x.takeover_id,'expired_without_handback',jsonb_build_object('taken_by',x.taken_by,'expires_at',x.expires_at,'resumed_message_id',v_result->'resumed_message_id',
   'settled',v_result->'settled')) on conflict do nothing;
 return v_result||jsonb_build_object('status','expired');
end $$;
alter function authz.nexloop_takeover_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_takeover_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_takeover_command(text,text,text,text,text) to nexloop_domain_worker;

-- Work feed port: 0111 body, 'takeover-expiry' added.
create or replace function authz.nexloop_work_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_feed text:=c->>'feed';v_verb text:=c->>'verb';
 v_result jsonb;v_limit integer;v_lease integer;v_delay integer;v_max integer;r runtime.nexloop_work_feed%rowtype;ident jsonb;
begin
 if session_user<>'nexloop_domain_worker' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or v_feed is null or v_feed not in ('recall-instance','claim-match','plan-reevaluate','reply-due','commitment-register','commitment-monitor','takeover-expiry')
  or v_verb is null or v_verb not in ('claim','complete','retry','backlog')
  or a->>'protocol' is distinct from 'nexloop-work-feed-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:NexLoop.feed.'||v_feed||':1' or a->>'resource_id' is distinct from a->>'action_resource'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-work-feed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb then raise exception 'work feed unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='claim' then
  v_limit:=(c->>'limit')::integer;v_lease:=(c->>'lease_seconds')::integer;
  if v_limit is null or v_limit not between 1 and 50 or v_lease is null or v_lease not between 1 and 600 then raise exception 'work feed input invalid' using errcode='22023';end if;
  with due as (
   select f.item_key from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'
    and f.available_at<=clock_timestamp() and (f.lease_until is null or f.lease_until<=clock_timestamp())
   order by f.available_at,f.changed_at,f.item_key limit v_limit for update skip locked),
  leased as (
   update runtime.nexloop_work_feed f set lease_until=clock_timestamp()+make_interval(secs=>v_lease),lease_seq=f.change_seq,attempts=f.attempts+1
   from due where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.item_key=due.item_key
   returning f.item_key,f.payload,f.change_seq,f.attempts,f.available_at)
  select coalesce(jsonb_agg(jsonb_build_object('item_key',l.item_key,'payload',l.payload,'fence',l.change_seq,'attempts',l.attempts)
   order by l.available_at,l.item_key),'[]'::jsonb) into v_result from leased l;
 elsif v_verb in ('complete','retry') then
  select * into r from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=c->>'item_key' for update;
  -- A lost or replaced lease never completes or retries someone else's work.
  if not found or r.lease_seq is distinct from (c->>'fence')::bigint or r.lease_until<=clock_timestamp() then
   v_result:=jsonb_build_object('status','lease_lost');
  elsif v_verb='complete' then
   if r.change_seq=r.lease_seq then
    delete from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','completed');
   else
    -- A newer change arrived while working: keep it pending (it is processed again from scratch).
    update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,attempts=0
     where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','changed');
   end if;
  else
   v_delay:=(c->>'delay_seconds')::integer;v_max:=(c->>'max_attempts')::integer;
   if v_delay is null or v_delay not between 0 and 3600 or v_max is null or v_max not between 1 and 20 or coalesce(c->>'code','')!~'^[a-z0-9_]{1,64}$' then
    raise exception 'work feed input invalid' using errcode='22023';end if;
   update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,last_code=c->>'code',
    status=case when r.change_seq=r.lease_seq and r.attempts>=v_max then 'dead_lettered' else 'pending' end,
    available_at=case when r.change_seq=r.lease_seq then clock_timestamp()+make_interval(secs=>v_delay) else clock_timestamp() end,
    attempts=case when r.change_seq=r.lease_seq then r.attempts else 0 end
   where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key
   returning jsonb_build_object('status',status) into v_result;
  end if;
 else
  select jsonb_build_object(
   'pending',(select count(*) from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'oldest_pending_seconds',(select floor(extract(epoch from clock_timestamp()-min(f.changed_at)))::bigint from runtime.nexloop_work_feed f
     where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'dead_lettered',(select coalesce(jsonb_agg(jsonb_build_object('item_key',x.item_key,'attempts',x.attempts,'code',x.last_code) order by x.changed_at desc),'[]'::jsonb)
     from (select * from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='dead_lettered'
      order by f.changed_at desc limit 20) x)) into v_result;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;

-- 3. The Agent does not compete ----------------------------------------------------------------------------------
alter function control.nexloop_contact_assert_intent(text,text,uuid) rename to nexloop_contact_assert_intent_before_takeover_v0112;
create function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;v_conversation text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if found then
  select o.conversation_id into v_conversation from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=p_intent;
  -- Consumer scope: no Agent effect at all; conversation scope: no Agent reply in that conversation.
  if exists(select 1 from control.nexloop_takeovers x where x.tenant_id=p_tenant and x.world=p_world and x.ended_at is null and x.expires_at>clock_timestamp()
    and ((x.scope_kind='consumer' and x.scope_ref=i.consumer_id) or (x.scope_kind='conversation' and x.scope_ref=v_conversation))) then
   raise exception 'taken over by a person: the Agent does not reply' using errcode='NXC06';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 perform control.nexloop_contact_assert_intent_before_takeover_v0112(p_tenant,p_world,p_intent);
end $$;
alter function control.nexloop_contact_assert_intent(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_contact_assert_intent(text,text,uuid) from public;

-- Message relay: no message Run for a message in a taken-over conversation (the item is retried later).
alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) rename to nexloop_message_run_issuance_command_before_takeover_v0049;
revoke all on function authz.nexloop_message_run_issuance_command_before_takeover_v0049(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_message_run_issuance_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_result jsonb:=authz.nexloop_message_run_issuance_command_before_takeover_v0049(p_digest,p_world,p_text,p_signature,p_payload);
 c jsonb:=p_payload::jsonb;v_tenant text:=p_text::jsonb->>'tenant_id';v_conv runtime.nexloop_conversations;
begin
 if c->>'verb'='issue' then
  perform set_config('eios.tenant_id',v_tenant,true);
  select cv.* into v_conv from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
   where b.tenant_id=v_tenant and b.world=p_world and b.message_id=c->>'message_id';
  if control.nexloop_takeover_active(v_tenant,p_world,v_conv.consumer_id,v_conv.conversation_id) is not null then
   raise exception 'conversation taken over by a person: no message Run' using errcode='NXC06';end if;  -- rolls back the issuance
 end if;
 return v_result;
end $$;
alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) to nexloop_api;

-- Fallback reply Run (ADR-023 §2.7): not while taken over.
alter function authz.nexloop_reply_fallback_command(text,text,text,text,text) rename to nexloop_reply_fallback_command_before_takeover_v0110;
revoke all on function authz.nexloop_reply_fallback_command_before_takeover_v0110(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_reply_fallback_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_result jsonb:=authz.nexloop_reply_fallback_command_before_takeover_v0110(p_digest,p_world,p_text,p_signature,p_payload);
 c jsonb:=p_payload::jsonb;v_tenant text:=p_text::jsonb->>'tenant_id';v_conv runtime.nexloop_conversations;
begin
 if c->>'verb'='issue' then
  perform set_config('eios.tenant_id',v_tenant,true);
  select cv.* into v_conv from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
   where b.tenant_id=v_tenant and b.world=p_world and b.message_id=c->>'message_id';
  if control.nexloop_takeover_active(v_tenant,p_world,v_conv.consumer_id,v_conv.conversation_id) is not null then
   raise exception 'conversation taken over by a person: no fallback reply Run' using errcode='NXC06';end if;
 end if;
 return v_result;
end $$;
alter function authz.nexloop_reply_fallback_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_reply_fallback_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_reply_fallback_command(text,text,text,text,text) to nexloop_api;

-- Reply guarantee port: 0110 body; the state reports an active takeover.
create or replace function authz.nexloop_reply_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-reply-guarantee-v1','eios:action:nexloop.reply.guarantee:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;m runtime.nexloop_conversation_messages;v_consumer text;
begin
 perform set_config('eios.tenant_id',t,true);
 select cm.* into m from runtime.nexloop_conversation_messages cm where cm.tenant_id=t and cm.world=p_world and cm.message_id=c->>'message_id'
  and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%';
 if not found then raise exception 'reply message unavailable' using errcode='42501';end if;
 select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=t and world=p_world and conversation_id=m.conversation_id;
 if c->>'verb'='state' then
  return jsonb_build_object('message_id',m.message_id,'conversation_id',m.conversation_id,'consumer_id',v_consumer,
   'accepted_at',m.record->>'accepted_at',
   'settled',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
     and o.delivery_state in ('provider_accepted','delivered')),
   'in_flight',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
     and o.delivery_state in ('persisted','dispatching','unknown')),
   'restricted',exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world and r.consumer_id=v_consumer and r.active),
   'escalated',exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=t and e.world=p_world and e.message_id=m.message_id),
   'fallback_run_id',(select f.run_id from authz.nexloop_message_fallback_issuances f where f.tenant_id=t and f.world=p_world and f.message_id=m.message_id),
   -- NX-028 D3/D5: while a takeover covers the conversation a person answers; no fallback, no escalation until it ends.
   'taken_over',control.nexloop_takeover_active(t,p_world,v_consumer,m.conversation_id) is not null,
   'takeover_expires_at',(select x.expires_at from control.nexloop_takeovers x where x.takeover_id=control.nexloop_takeover_active(t,p_world,v_consumer,m.conversation_id)));
 elsif c->>'verb'='escalate' then
  if coalesce(c->>'reason','')!~'^[a-z_]{1,64}$' or jsonb_typeof(c->'detail') is distinct from 'object' then raise exception 'reply escalation invalid' using errcode='22023';end if;
  -- Evidence only for a message still unanswered: a settled message is never escalated.
  if exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
    and o.delivery_state in ('provider_accepted','delivered')) then raise exception 'reply already settled' using errcode='40001';end if;
  insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail)
   values(t,p_world,m.message_id,m.conversation_id,v_consumer,c->>'reason',c->'detail') on conflict do nothing;
  return jsonb_build_object('message_id',m.message_id,'escalated',true);
 end if;
 raise exception 'reply command invalid' using errcode='22023';
end $$;

-- 5. Customer view (D4) ---------------------------------------------------------------------------------------------
create function authz.nexloop_conversation_takeover_state(p_digest text,p_world text,p_conversation text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';cv runtime.nexloop_conversations;v uuid;
begin
 if ident->'binding'->>'subject_kind' is distinct from 'human' or coalesce(p_conversation,'')!~'^[a-f0-9]{64}$' then
  raise exception 'conversation state unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into cv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=p_conversation;
 -- Only the conversation's own Human; nothing about who took it over.
 if not found or cv.principal_id is distinct from ident->'binding'->>'subject_principal_id' then raise exception 'conversation state unavailable' using errcode='42501';end if;
 v:=control.nexloop_takeover_active(v_tenant,p_world,cv.consumer_id,cv.conversation_id);
 return jsonb_build_object('conversation_id',p_conversation,'handled_by',case when v is null then 'agent' else 'human' end);
end $$;
alter function authz.nexloop_conversation_takeover_state(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_conversation_takeover_state(text,text,text) from public;
grant execute on function authz.nexloop_conversation_takeover_state(text,text,text) to nexloop_api;
