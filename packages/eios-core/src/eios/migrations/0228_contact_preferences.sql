-- Consumer communication preferences, slice 2 of the operator persona design (temporary number 0228; docs/design/
-- operator-persona-design.md §4, owner decisions D6 and D9, 2026-10-19).
-- 1. Source: only the consumer's own statements. runtime.nexloop_contact_preferences reads the Consumer's Claims at the time of
--    the check (no projection to go stale): speaker consumer, epistemic_kind preference or constraint, modality asserted or
--    requested, not a hypothesis, not superseded, not rejected, value not erased; the predicate names the dimension (fixed v1
--    vocabulary, English and Chinese spellings):
--      quiet_hours  contact_quiet_hours | 勿扰时段 | 免打扰时段   string 'HH:MM-HH:MM' (Asia/Shanghai; may cross midnight)
--      frequency    contact_frequency_max | 联系频率上限           number: at most N contacts in 7 days
--      channel      contact_channel | 联系渠道                     string; affirmed = only these channels, negated = not this one
--      topic        contact_topic | 联系主题                       string; negated = not this topic (affirmed adds nothing)
--      tone         communication_tone | 沟通语气                  string (Context only, never enforced)
--    A correction supersedes the earlier Claim (NX-019), so the later statement wins; an erased Claim no longer counts. A
--    statement that does not parse (bad window, non-number) is not enforceable and is ignored here (it stays evidence).
-- 2. Enforcement at dispatch (D6): the outermost control.nexloop_contact_assert_intent (0177 kept underneath: contact
--    restrictions and the one-reply rule run first, ADR-023 stays the hardest stop) checks, for an intent that contacts the
--    customer (customer_contact category, or a notification parameter set) and is not a reply bound to an inbound message:
--    quiet hours, frequency, channel (from the intent's channel parameter; unknown channel under an "only these" preference
--    is refused), topic (from the intent's topic or purpose parameter). Refusal: errcode NXC06, text 'contact preference:
--    <preference_quiet_hours | preference_frequency | preference_channel | preference_topic>'. Replies to the customer are
--    never held back by time or frequency (design §4.2).
-- 3. Only tightening (D9): preferences only ever refuse; nothing here relaxes a contact restriction, a pause or any other
--    control (a "contact me any time" statement has no effect).
-- 4. The owner may decline a preference (not add one): governed owner Action contact_preference.dismiss /
--    dismiss_contact_preference (claim_id, dismissed true|false, reason; revision). Rows leave when the Consumer's erasure
--    completes.
-- 5. Context: the operator persona section (0227) carries the effective preferences (claim id, dimension, value) when there are
--    any, so the reply Run sees tone and the rest as data; SQL re-derives the section at bind as before.
-- 6. Workbench observe verb contact_preferences (owner only): a Consumer's preferences (no quote), each with its dismissal.

-- 1. Preferences from Claims -------------------------------------------------------------------------------------------------
create table control.nexloop_contact_preference_dismissals (
 tenant_id text not null,world text not null,claim_id text not null check(claim_id~'^[0-9a-f]{64}$'),consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 dismissed boolean not null,revision bigint not null check(revision>0),set_by text not null,request_intent text not null,
 reason text not null check(char_length(reason) between 1 and 500),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,claim_id),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
alter table control.nexloop_contact_preference_dismissals owner to nexloop_owner;
alter table control.nexloop_contact_preference_dismissals enable row level security;
alter table control.nexloop_contact_preference_dismissals force row level security;
revoke all on control.nexloop_contact_preference_dismissals from public;
create policy contact_preference_dismissal_tenant on control.nexloop_contact_preference_dismissals to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));

create function runtime.nexloop_contact_preference_dimension(p_predicate text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when lower(btrim(p_predicate)) in ('contact_quiet_hours','勿扰时段','免打扰时段') then 'quiet_hours'
  when lower(btrim(p_predicate)) in ('contact_frequency_max','联系频率上限') then 'frequency'
  when lower(btrim(p_predicate)) in ('contact_channel','联系渠道') then 'channel'
  when lower(btrim(p_predicate)) in ('contact_topic','联系主题') then 'topic'
  when lower(btrim(p_predicate)) in ('communication_tone','沟通语气') then 'tone' end
$$;
-- Every current, parseable preference of the Consumer: [{claim_id, dimension, polarity, value, dismissed}], oldest first.
create function runtime.nexloop_contact_preferences(p_tenant text,p_world text,p_consumer text) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_agg(jsonb_build_object('claim_id',c.claim_id,'dimension',p.dimension,'polarity',c.polarity,'value',c.value->'value',
   'dismissed',coalesce(d.dismissed,false)) order by c.source_sequence nulls last,c.claim_id),'[]'::jsonb)
 from ontology.nexloop_claims c
  cross join lateral (select runtime.nexloop_contact_preference_dimension(c.predicate) dimension) p
  left join control.nexloop_contact_preference_dismissals d on d.tenant_id=c.tenant_id and d.world=c.world and d.claim_id=c.claim_id
 where c.tenant_id=p_tenant and c.world=p_world and c.consumer_id=p_consumer and p.dimension is not null
  and c.speaker='consumer' and c.epistemic_kind in ('preference','constraint') and c.modality in ('asserted','requested')
  and c.resolution_state not in ('hypothesis_only','superseded','rejected_definition') and c.superseded_at is null
  and coalesce(c.value->>'type','')<>'erased'
  and case p.dimension
   when 'quiet_hours' then c.value->>'type'='string' and coalesce(c.value->>'value','')~'^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$'
   when 'frequency' then c.value->>'type'='number' and jsonb_typeof(c.value->'value')='number' and (c.value->>'value')::numeric between 0 and 1000
    and (c.value->>'value')::numeric=floor((c.value->>'value')::numeric)
   else c.value->>'type'='string' and char_length(coalesce(c.value->>'value','')) between 1 and 64 end
$$;

-- 2. The check ---------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_contact_preference_refusal(p_tenant text,p_world text,p_consumer text,p_channel text,p_topic text,p_intent uuid,p_at timestamptz)
 returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare x jsonb;v_local time:=(p_at at time zone 'Asia/Shanghai')::time;v_from time;v_to time;v_count bigint;v_only text[];
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

alter function control.nexloop_contact_assert_intent(text,text,uuid) rename to nexloop_contact_assert_intent_before_preferences_v0228;
revoke all on function control.nexloop_contact_assert_intent_before_preferences_v0228(text,text,uuid) from public;
create function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;cat jsonb;v_code text;
begin
 -- ADR-023 first: contact restriction, bound replies, one reply per inbound message (0110/0126/0177).
 perform control.nexloop_contact_assert_intent_before_preferences_v0228(p_tenant,p_world,p_intent);
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 cat:=control.nexloop_intent_effect_category(p_tenant,p_world,p_intent);
 if i.intent_id is not null and i.consumer_id is not null and coalesce(cat->>'category'='customer_contact' or (cat->>'notification')::boolean,true)
  and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=p_intent and o.trigger_message_id is not null) then
  v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,i.consumer_id,i.frozen_request->'parameters'->>'channel',
   coalesce(i.frozen_request->'parameters'->>'topic',i.frozen_request->'parameters'->>'purpose'),p_intent,clock_timestamp());
  if v_code is not null then raise exception 'contact preference: %',v_code using errcode='NXC06';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;

-- 4. The owner declines (or reinstates) a preference ------------------------------------------------------------------------
create function control.nexloop_governed_dismiss_contact_preference(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_claim text:=body->>'claim_id';v_consumer text;cur control.nexloop_contact_preference_dismissals;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','claim_id','dismissed','reason'))
  or coalesce(v_claim,'')!~'^[0-9a-f]{64}$' or jsonb_typeof(body->'dismissed') is distinct from 'boolean'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'contact preference: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select c.consumer_id into v_consumer from ontology.nexloop_claims c where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=v_claim
  and c.speaker='consumer' and runtime.nexloop_contact_preference_dimension(c.predicate) is not null;
 if not found then raise exception 'contact preference: not_a_preference' using errcode='22023';end if;
 select * into cur from control.nexloop_contact_preference_dismissals where tenant_id=p_tenant and world=p_world and claim_id=v_claim for update;
 if coalesce(cur.dismissed,false)=(body->>'dismissed')::boolean then
  return jsonb_build_object('claim_id',v_claim,'dismissed',coalesce(cur.dismissed,false),'revision',coalesce(cur.revision,0),'changed',false);end if;
 insert into control.nexloop_contact_preference_dismissals(tenant_id,world,claim_id,consumer_id,dismissed,revision,set_by,request_intent,reason)
  values(p_tenant,p_world,v_claim,v_consumer,(body->>'dismissed')::boolean,1,p_principal,p_intent,body->>'reason')
  on conflict(tenant_id,world,claim_id) do update set dismissed=excluded.dismissed,revision=control.nexloop_contact_preference_dismissals.revision+1,
   set_by=excluded.set_by,request_intent=excluded.request_intent,reason=excluded.reason,updated_at=clock_timestamp();
 return jsonb_build_object('claim_id',v_claim,'dismissed',(body->>'dismissed')::boolean,'changed',true,
  'revision',(select revision from control.nexloop_contact_preference_dismissals where tenant_id=p_tenant and world=p_world and claim_id=v_claim));
end $$;

-- The owner's dismissals (they name the Consumer and carry a reason) leave when the Consumer's erasure completes.
create function control.nexloop_contact_preference_erased() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if new.state='completed' and old.state is distinct from 'completed' then
  delete from control.nexloop_contact_preference_dismissals where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 end if;
 return new;
end $$;
create trigger contact_preference_erased after update of state on control.nexloop_consumer_erasures
 for each row execute function control.nexloop_contact_preference_erased();

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_contact_preference_dimension(text)','runtime.nexloop_contact_preferences(text,text,text)',
   'runtime.nexloop_contact_preference_refusal(text,text,text,text,text,uuid,timestamp with time zone)',
   'control.nexloop_contact_assert_intent(text,text,uuid)','control.nexloop_governed_dismiss_contact_preference(text,text,text,text,text,jsonb)',
   'control.nexloop_contact_preference_erased()'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('contact_preference.dismiss','dismiss_contact_preference','human','control.nexloop_governed_dismiss_contact_preference(text,text,text,text,text,jsonb)','NX-023');

-- 5. Context: the effective preferences ride in the operator persona section (0227 body, one marked addition) --------------
create or replace function runtime.nexloop_operator_persona_section(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d record;v integer;doc jsonb;prefs jsonb;
begin
 select * into d from runtime.nexloop_operator_persona_desired(p_tenant,p_world,p_consumer);
 if d.persona_id is null then return null;end if;
 v:=runtime.nexloop_operator_persona_usable(p_tenant,d.persona_id);
 select document into doc from control.nexloop_operator_personas where tenant_id=p_tenant and persona_id=d.persona_id and version=v;
 -- 0228: the consumer's own, not dismissed communication preferences (claim id, dimension, polarity, value), if any.
 select coalesce(jsonb_agg(x-'dismissed'),'[]'::jsonb) into prefs from jsonb_array_elements(runtime.nexloop_contact_preferences(p_tenant,p_world,p_consumer)) x
  where not (x->>'dismissed')::boolean;
 return jsonb_build_object('persona_ref','operator-persona:'||d.persona_id||'@'||v,'persona_id',d.persona_id,'version',v,'source',d.source,
  'document',doc,'ai_disclosure',runtime.nexloop_ai_disclosure_setting(p_tenant)-'revision')
  ||case when jsonb_array_length(prefs)>0 then jsonb_build_object('preferences',prefs) else '{}'::jsonb end;
end $$;

-- 6. The owner's view of a Consumer's preferences (workbench observe; the 0227 body kept for every other verb) ----------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_contact_preferences_v0228;
revoke all on function authz.nexloop_workbench_observe_read_before_contact_preferences_v0228(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;
begin
 if c->>'verb' is distinct from 'contact_preferences' then
  return authz.nexloop_workbench_observe_read_before_contact_preferences_v0228(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as 0210/0220/0227).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_consumer:=c->>'consumer_id';
 if (select count(*) from jsonb_object_keys(c))<>2 or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' then raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return jsonb_build_object('consumer_id',v_consumer,'preferences',(select coalesce(jsonb_agg(x||coalesce((select jsonb_build_object('dismissal',jsonb_build_object(
   'dismissed',d.dismissed,'revision',d.revision,'updated_at',d.updated_at)) from control.nexloop_contact_preference_dismissals d where d.tenant_id=v_tenant and d.world=p_world
   and d.claim_id=x->>'claim_id'),'{}'::jsonb)),'[]'::jsonb) from jsonb_array_elements(runtime.nexloop_contact_preferences(v_tenant,p_world,v_consumer)) x));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
