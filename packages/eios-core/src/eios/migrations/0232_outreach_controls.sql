-- Proactive outreach, slice 2: tenant controls (temporary number 0232; docs/implementation/outreach-impl-plan.md, owner
-- decisions P3, P8 and O1-O5, 2026-10-19/20). Still no outreach Action and no real dispatch: the switch stays read only.
-- 1. control.nexloop_outreach_controls (one row per tenant, revision) and control.nexloop_outreach_control_events (append-only:
--    the settings, the confirmed consent mode when marketing was switched on, who, request, reason).
--    Settings: time_zone (IANA name), quiet_hours 'HH:MM-HH:MM' or null (none), per_24h, per_7d (every contact that is not a bound
--    reply, O2), marketing {enabled, consent_mode opt_out | opt_in, unsubscribe_notice, per_7d (<= per_7d), sender_label}.
-- 2. runtime.nexloop_outreach_settings(tenant): the stored settings, or the owner's defaults when none are stored: Asia/Shanghai,
--    21:00-09:00, 1 per 24 h, 2 per 7 days, marketing off, opt_out, the default unsubscribe notice, 1 marketing per 7 days.
-- 3. Governed owner Action outreach_controls.set / set_outreach_controls (0124 registry, owner only). O5: switching marketing on
--    (off -> on) requires confirm_consent_mode equal to the chosen consent mode; refused otherwise with 'outreach controls:
--    consent_mode_unconfirmed'; the confirmation is recorded in the event. Other refusals: 'outreach controls: invalid'.
-- 4. The slice 1 dry run (0231 bodies, changes marked 0232) and the consumer's quiet-hours preference (0228 body) read the
--    tenant's settings and time zone instead of the fixed defaults (O1). runtime.nexloop_outreach_defaults stays for 0231.

create table control.nexloop_outreach_controls (
 tenant_id text primary key,settings jsonb not null check(jsonb_typeof(settings)='object'),revision bigint not null check(revision>0),
 updated_by text not null,request_intent text not null,updated_at timestamptz not null default clock_timestamp(),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_outreach_control_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,revision bigint not null,settings jsonb not null,
 consent_mode_confirmed text check(consent_mode_confirmed in ('opt_out','opt_in')),set_by text not null,request_intent text not null,
 reason text not null check(char_length(reason) between 1 and 500),recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,revision),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_outreach_controls','control.nexloop_outreach_control_events'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';execute 'revoke all on '||t||' from public';
  execute format('create policy %I on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',
   'tenant_'||replace(t,'control.',''),t);
 end loop;
end $tables$;
create function control.nexloop_outreach_control_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'outreach control events are append-only' using errcode='42501';end $$;
alter function control.nexloop_outreach_control_events_append_only() owner to nexloop_owner;
create trigger outreach_control_events_append_only before update or delete on control.nexloop_outreach_control_events
 for each row execute function control.nexloop_outreach_control_events_append_only();

create function runtime.nexloop_outreach_default_settings() returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('time_zone','Asia/Shanghai','quiet_hours','21:00-09:00','per_24h',1,'per_7d',2,
  'marketing',jsonb_build_object('enabled',false,'consent_mode','opt_out','unsubscribe_notice','不想再收到这类消息，回复“退订”即可。','per_7d',1,'sender_label',''))
$$;
create function runtime.nexloop_outreach_settings(p_tenant text) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce((select settings||jsonb_build_object('revision',revision,'source','tenant') from control.nexloop_outreach_controls where tenant_id=p_tenant),
  runtime.nexloop_outreach_default_settings()||jsonb_build_object('revision',0,'source','default'))
$$;
-- Every field present and in range; the marketing limit within the tenant limit; the time zone a known name.
create function control.nexloop_outreach_settings_ok(s jsonb) returns boolean
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_typeof(s)='object'
  and (select array_agg(k order by k) from jsonb_object_keys(s) k)=array['marketing','per_24h','per_7d','quiet_hours','time_zone']
  and jsonb_typeof(s->'time_zone')='string' and exists(select 1 from pg_timezone_names where name=s->>'time_zone')
  and (s->'quiet_hours'='null'::jsonb or coalesce(s->>'quiet_hours','')~'^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$')
  and jsonb_typeof(s->'per_24h')='number' and (s->>'per_24h')::numeric between 0 and 100 and (s->>'per_24h')::numeric=floor((s->>'per_24h')::numeric)
  and jsonb_typeof(s->'per_7d')='number' and (s->>'per_7d')::numeric between 0 and 1000 and (s->>'per_7d')::numeric=floor((s->>'per_7d')::numeric)
  and jsonb_typeof(s->'marketing')='object'
  and (select array_agg(k order by k) from jsonb_object_keys(s->'marketing') k)=array['consent_mode','enabled','per_7d','sender_label','unsubscribe_notice']
  and jsonb_typeof(s->'marketing'->'enabled')='boolean' and s->'marketing'->>'consent_mode' in ('opt_out','opt_in')
  and jsonb_typeof(s->'marketing'->'unsubscribe_notice')='string' and char_length(s->'marketing'->>'unsubscribe_notice') between 1 and 200
  and jsonb_typeof(s->'marketing'->'sender_label')='string' and char_length(s->'marketing'->>'sender_label')<=40
  and jsonb_typeof(s->'marketing'->'per_7d')='number' and (s->'marketing'->>'per_7d')::numeric=floor((s->'marketing'->>'per_7d')::numeric)
  and (s->'marketing'->>'per_7d')::numeric between 0 and (s->>'per_7d')::numeric,false)
$$;

create function control.nexloop_governed_set_outreach_controls(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_settings jsonb;cur control.nexloop_outreach_controls;v_before jsonb;v_confirm text:=body->>'confirm_consent_mode';v_revision bigint;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','settings','confirm_consent_mode','reason'))
  or length(coalesce(body->>'reason','')) not between 1 and 500 or (v_confirm is not null and v_confirm not in ('opt_out','opt_in')) then
  raise exception 'outreach controls: invalid' using errcode='22023';end if;
 v_settings:=body->'settings';
 if control.nexloop_outreach_settings_ok(v_settings) is not true then raise exception 'outreach controls: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_outreach_controls where tenant_id=p_tenant for update;
 v_before:=runtime.nexloop_outreach_settings(p_tenant)-'revision'-'source';
 -- O5: switching marketing on confirms the consent mode (opt_out default, opt_in optional) in the same request.
 if (v_settings->'marketing'->>'enabled')::boolean and not (v_before->'marketing'->>'enabled')::boolean
  and v_confirm is distinct from v_settings->'marketing'->>'consent_mode' then
  raise exception 'outreach controls: consent_mode_unconfirmed' using errcode='22023';end if;
 if v_before=v_settings then
  return jsonb_build_object('settings',v_settings,'revision',coalesce(cur.revision,0),'changed',false);end if;
 v_revision:=coalesce(cur.revision,0)+1;
 insert into control.nexloop_outreach_controls(tenant_id,settings,revision,updated_by,request_intent) values(p_tenant,v_settings,v_revision,p_principal,p_intent)
  on conflict(tenant_id) do update set settings=excluded.settings,revision=excluded.revision,updated_by=excluded.updated_by,
   request_intent=excluded.request_intent,updated_at=clock_timestamp();
 insert into control.nexloop_outreach_control_events(tenant_id,revision,settings,consent_mode_confirmed,set_by,request_intent,reason)
  values(p_tenant,v_revision,v_settings,case when (v_settings->'marketing'->>'enabled')::boolean and not (v_before->'marketing'->>'enabled')::boolean then v_confirm end,
   p_principal,p_intent,body->>'reason');
 return jsonb_build_object('settings',v_settings,'revision',v_revision,'changed',true);
end $$;

-- 4. The dry run and the preference check read the tenant's settings (bodies copied, changes marked 0232) ----------------------
-- 0232: v_local is read in the tenant time zone (O1).
create or replace function runtime.nexloop_contact_preference_refusal(p_tenant text,p_world text,p_consumer text,p_channel text,p_topic text,p_intent uuid,p_at timestamptz)
 returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare x jsonb;v_local time:=(p_at at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'))::time;v_from time;v_to time;v_count bigint;v_only text[];
begin
 for x in select value from jsonb_array_elements(runtime.nexloop_contact_preferences(p_tenant,p_world,p_consumer)) where not (value->>'dismissed')::boolean loop
  if x->>'dimension'='quiet_hours' and x->>'polarity'='affirmed' then
   v_from:=split_part(x->>'value','-',1)::time;v_to:=split_part(x->>'value','-',2)::time;
   if (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to)) then return 'preference_quiet_hours';end if;
  elsif x->>'dimension'='frequency' and x->>'polarity'='affirmed' then
   select count(*) into v_count from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer
    and i.intent_id<>p_intent and i.created_at>p_at-interval '7 days'
    and i.state in ('dispatching','unknown','fulfilled','observed_fulfilled','confirmed')
    and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=i.tenant_id and o.world=i.world and o.intent_id=i.intent_id and o.trigger_message_id is not null)
    and coalesce((control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'category')='customer_contact'
      or (control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'notification')::boolean,false);
   if v_count>=floor((x->>'value')::numeric) then return 'preference_frequency';end if;
  elsif x->>'dimension'='channel' then
   if x->>'polarity'='negated' and p_channel is not null and lower(p_channel)=lower(x->>'value') then return 'preference_channel';end if;
   if x->>'polarity'='affirmed' then v_only:=array_append(v_only,lower(x->>'value'));end if;
  elsif x->>'dimension'='topic' and x->>'polarity'='negated' and p_topic is not null and lower(p_topic)=lower(x->>'value') then
   return 'preference_topic';
  end if;
 end loop;
 if v_only is not null and (p_channel is null or not lower(p_channel)=any(v_only)) then return 'preference_channel';end if;
 return null;
end $$;

-- 0232: d is the tenant's settings (the owner's defaults when none are stored).
create or replace function runtime.nexloop_outreach_gates(p_tenant text,p_world text,p_consumer text,p_purpose text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d jsonb:=runtime.nexloop_outreach_settings(p_tenant);g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;
begin
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','purpose','result','not_wired','detail','purpose list: slice 3');
 g:=g||jsonb_build_object('gate','persona_excluded','result',case when runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)<>'real' then 'refuse' else 'pass' end,
   'detail',runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer));
 g:=g||jsonb_build_object('gate','quiet_hours','result',case when (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to))
   then 'refuse' else 'pass' end,'detail',d->>'quiet_hours');
 v_n24:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '24 hours');
 v_n7:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '7 days');
 g:=g||jsonb_build_object('gate','frequency','result',case when v_n24>=(d->>'per_24h')::integer or v_n7>=(d->>'per_7d')::integer then 'refuse' else 'pass' end,
   'detail',jsonb_build_object('last_24h',v_n24,'last_7d',v_n7));
 v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,p_consumer,'native.webchat',p_purpose,gen_random_uuid(),p_at);
 g:=g||jsonb_build_object('gate','consumer_preference','result',case when v_code is null then 'pass' else 'refuse' end,'detail',v_code);
 v_layer:=control.nexloop_real_dispatch_closed_layer(p_tenant,p_world,'eios:action:nexloop.outreach.send:1');
 g:=g||jsonb_build_object('gate','real_dispatch','result',case when v_layer is null then 'pass' else 'refuse' end,'detail',coalesce(v_layer,'open'));
 g:=g||jsonb_build_object('gate','budget','result','not_wired','detail','outreach Action and budget: slice 4');
 return g;
end $$;

-- 0232: the result carries the tenant's settings instead of the fixed defaults.
create or replace function runtime.nexloop_outreach_candidates(p_tenant text,p_world text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_lead integer:=coalesce((select (definition->>'lead_seconds')::integer from control.nexloop_commitment_settings order by version desc limit 1),3600);
 x record;out jsonb:='[]'::jsonb;gates jsonb;
begin
 for x in
  select c.consumer_id,'commitment_followup' purpose,'commitment:'||c.commitment_id origin_ref,c.commitment_id||':due_soon' dedupe_key,
   (o.properties->>'due_at')::timestamptz at_time
  from runtime.nexloop_commitments c join ontology.objects o on o.tenant_id=c.tenant_id and o.world=c.world and o.type_name='Commitment' and o.object_id=c.commitment_id
  where c.tenant_id=p_tenant and c.world=p_world and o.properties->>'status' in ('open','in_progress') and o.properties->>'due_at' is not null
   and (o.properties->>'due_at')::timestamptz>p_at and (o.properties->>'due_at')::timestamptz-make_interval(secs=>v_lead)<=p_at
  union all
  select i.consumer_id,'service_followup','intent:'||i.intent_id::text,i.intent_id::text||':'||i.state,
   coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)
  from runtime.nexloop_effect_intents i
  where i.tenant_id=p_tenant and i.world=p_world and i.state in ('fulfilled','confirmed','failed')
   and not exists(select 1 from runtime.nexloop_outbound_messages m where m.tenant_id=i.tenant_id and m.world=i.world and m.intent_id=i.intent_id)
   and coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)>p_at-interval '24 hours'
  order by 5,3
 loop
  gates:=runtime.nexloop_outreach_gates(p_tenant,p_world,x.consumer_id,x.purpose,p_at);
  out:=out||jsonb_build_object('consumer_id',x.consumer_id,'purpose',x.purpose,'origin_ref',x.origin_ref,'dedupe_key',x.dedupe_key,'at',x.at_time,'gates',gates,
   'would_dispatch',not exists(select 1 from jsonb_array_elements(gates) y where y->>'result'<>'pass'),
   'blocked_by',(select coalesce(jsonb_agg(y->>'gate'),'[]'::jsonb) from jsonb_array_elements(gates) y where y->>'result'='refuse'));
 end loop;
 return jsonb_build_object('at',p_at,'settings',runtime.nexloop_outreach_settings(p_tenant),'candidates',out,
  'note','dry run: no outreach Action is published yet (slice 4); nothing is sent');
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_outreach_default_settings()','runtime.nexloop_outreach_settings(text)','control.nexloop_outreach_settings_ok(jsonb)',
   'control.nexloop_governed_set_outreach_controls(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('outreach_controls.set','set_outreach_controls','human','control.nexloop_governed_set_outreach_controls(text,text,text,text,text,jsonb)','NX-022');
