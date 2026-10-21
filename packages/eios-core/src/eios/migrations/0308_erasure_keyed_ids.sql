-- NX-029 erasure: the owner's decisions of 2026-10-21 (both as recommended, L6 erasure-coverage-3 review 3.1 and §4). The migration
-- number is temporary; it lives only in the file name and the catalog.
-- 1. Tombstones of a Consumer erasure keep a keyed HMAC of the Consumer id, never the id: runtime.nexloop_tenant_hmac (the tenant's
--    erasure key, control.nexloop_erasure_salts, 0132) gives 'hmac:<hex>'. A BEFORE INSERT trigger rewrites subject_key (and the
--    tombstone_id derived from it) when the key is the Consumer of an erasure request; the existing tombstones are rewritten once here
--    (their append-only guard is set aside for this statement only, as 0273 does). A reader looks a receipt up by
--    runtime.nexloop_erasure_subject_key(tenant, consumer). Limit: whoever holds an original id and may call that function can still
--    confirm a match; the HMAC only keeps the id from being read back out of the table.
-- 2. Deterministic ids made from the Consumer id are keyed for what is generated from now on (existing ones are an accepted residue,
--    owner 2026-10-21): the outreach intent id (runtime.nexloop_outreach_intent_id; the same inputs still give the same id, so
--    idempotency holds) and the identity link request_id. Replay on the day of the change: when the keyed id has no send yet but the
--    legacy id of the same inputs does, the legacy id is used, so a request replayed across the change never sends twice. The
--    identity link replays its stored payload by identity_ref (0061), so a link prepared before the change keeps its old request_id.
-- Re-created functions take their latest bodies (0263, 0061) with only these changes (tests/test_function_copy_guard.py).

create function runtime.nexloop_tenant_hmac(p_tenant text,p_text text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare k bytea;v_prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 insert into control.nexloop_erasure_salts(tenant_id,salt) values(p_tenant,extensions.gen_random_bytes(32)) on conflict do nothing;
 select salt into k from control.nexloop_erasure_salts where tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return encode(extensions.hmac(convert_to(p_text,'UTF8'),k,'sha256'),'hex');
end $$;
alter function runtime.nexloop_tenant_hmac(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_tenant_hmac(text,text) from public;

-- 1. Tombstones.
create function runtime.nexloop_erasure_subject_key(p_tenant text,p_consumer text) returns text
 language sql security definer set search_path=pg_catalog,pg_temp as $$ select 'hmac:'||runtime.nexloop_tenant_hmac(p_tenant,'erasure-subject:'||p_consumer) $$;
alter function runtime.nexloop_erasure_subject_key(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_erasure_subject_key(text,text) from public;

create function runtime.nexloop_tombstone_keyed() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if new.subject_key~'^[a-f0-9]{64}$' and exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=new.tenant_id and e.world=new.world
   and e.consumer_id=new.subject_key) then
  new.subject_key:=runtime.nexloop_erasure_subject_key(new.tenant_id,new.subject_key);
  new.tombstone_id:=encode(sha256(convert_to(jsonb_build_array(new.request_ref,new.item_class,new.subject_kind,new.subject_key)::text,'UTF8')),'hex');
 end if;
 return new;
end $$;
alter function runtime.nexloop_tombstone_keyed() owner to nexloop_owner;
revoke all on function runtime.nexloop_tombstone_keyed() from public;
create trigger nx029_tombstone_keyed before insert on runtime.nexloop_erasure_tombstones for each row execute function runtime.nexloop_tombstone_keyed();

alter table runtime.nexloop_erasure_tombstones disable trigger nx029_append_only;
update runtime.nexloop_erasure_tombstones t set subject_key=runtime.nexloop_erasure_subject_key(t.tenant_id,t.subject_key),
  tombstone_id=encode(sha256(convert_to(jsonb_build_array(t.request_ref,t.item_class,t.subject_kind,runtime.nexloop_erasure_subject_key(t.tenant_id,t.subject_key))::text,'UTF8')),'hex')
 where t.subject_key~'^[a-f0-9]{64}$' and exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t.tenant_id and e.world=t.world and e.consumer_id=t.subject_key);
alter table runtime.nexloop_erasure_tombstones enable trigger nx029_append_only;

-- 2. Keyed ids from now on.
create function runtime.nexloop_outreach_intent_id(p_tenant text,p_world text,p_consumer text,p_purpose text,p_origin text,p_day text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_in text:=jsonb_build_array(p_tenant,p_consumer,p_purpose,p_origin,p_day)::text;v_id text;v_legacy text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 v_id:='outreach:'||runtime.nexloop_tenant_hmac(p_tenant,'outreach-intent:'||v_in);
 v_legacy:='outreach:'||encode(sha256(convert_to(v_in,'UTF8')),'hex');
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 if not exists(select 1 from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.intent_id=v_id)
   and exists(select 1 from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.intent_id=v_legacy) then
  v_id:=v_legacy;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v_id;
end $$;
alter function runtime.nexloop_outreach_intent_id(text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_intent_id(text,text,text,text,text,text) from public;

CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_accept(p_tenant text, p_world text, p_consumer text, p_purpose text, p_origin text, p_body text, p_conversation text, p_initiator text, p_principal text, p_intent text, p_run uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_purpose jsonb;v_id text;v_digest text;v_head bigint;old runtime.nexloop_outreach_sends;v_day text;
 v_approval boolean;  -- Outreach slice 6
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then raise exception 'outreach send: consumer_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_conversation is not null and not exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world
   and cv.conversation_id=p_conversation and cv.consumer_id=p_consumer) then raise exception 'outreach send: conversation_unavailable' using errcode='22023';end if;
 -- Topic, version and frequency class are frozen from the current list; whether the purpose is enabled is a dispatch gate.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 if not (v_purpose->>'listed')::boolean then raise exception 'outreach send: purpose_not_allowed' using errcode='22023';end if;
 if char_length(p_body)>(v_purpose->'definition'->'content_rules'->>'max_chars')::integer then raise exception 'outreach send: body_too_long' using errcode='22023';end if;
 -- Outreach slice 6 (P5): a purpose configured to need approval waits for it, whoever asked (owner or reevaluation Run); nothing is admitted before.
 v_approval:=coalesce((v_purpose->'definition'->>'requires_approval')::boolean,false);
 v_day:=to_char(runtime.nexloop_world_now(p_tenant,p_world) at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'),'YYYY-MM-DD');
 v_id:=runtime.nexloop_outreach_intent_id(p_tenant,p_world,p_consumer,p_purpose,p_origin,v_day);  -- owner 2026-10-21: keyed, the legacy id kept
 v_digest:=encode(sha256(convert_to(p_body,'UTF8')),'hex');
 select * into old from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_id;
 if found then
  if old.body_digest is distinct from v_digest or old.conversation_id is distinct from p_conversation then
   raise exception 'outreach send: intent_exists' using errcode='22023';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,origin_ref,conversation_id,
   body,body_digest,initiator,principal_id,request_intent,control_snapshot,run_id,state)
  values(p_tenant,p_world,v_id,p_consumer,p_purpose,(v_purpose->>'list_version')::integer,v_purpose->'definition'->>'topic',
   v_purpose->'definition'->>'frequency_class','native.webchat',p_origin,p_conversation,p_body,v_digest,p_initiator,p_principal,p_intent,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.outreach.send')),'goals','[]'::jsonb,'objects','[]'::jsonb,'budgets','[]'::jsonb),p_run,
   case when v_approval then 'pending_approval' else 'accepted' end);
 if v_approval then  -- Outreach slice 6: the approval window starts at acceptance
  insert into runtime.nexloop_outreach_approvals(tenant_id,world,intent_id,requested_by,expires_at)
   values(p_tenant,p_world,v_id,p_principal,runtime.nexloop_world_now(p_tenant,p_world)+runtime.nexloop_outreach_approval_window());
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-send:'||v_id,'nexloop.outreach.send.accepted',p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',v_id,'purpose',p_purpose,'origin_ref',p_origin,'initiator',p_initiator),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',v_id,'state',case when v_approval then 'pending_approval' else 'accepted' end,'replayed',false);
end $function$;

create or replace function authz.nexloop_prepare_browser_identity_link(p_digest text,p_world text,p_text text,p_signature text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb;identity jsonb;binding jsonb;key_material bytea;owner control.nexloop_consumer_owners;
 object ontology.objects;ref text;prior control.nexloop_consumer_identity_link_receipts;refs jsonb;result jsonb;
begin
 if session_user<>'nexloop_api' then raise exception 'identity link unavailable' using errcode='42501';end if;
 a:=p_text::jsonb;select sk.key_material into key_material from authz.nexloop_authority_signing_keys sk where sk.key_id=a->>'key_id' and active;
 if key_material is null or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-identity-link-prepare-v1:'||p_text,'UTF8'),key_material,'sha256'),'hex')
  or a->>'action_resource' is distinct from 'eios:action:nexloop.consumer.link_authenticated_identity:1' then
  raise exception 'identity link unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',binding->>'tenant_id',true);
 select * into owner from control.nexloop_consumer_owners where tenant_id=binding->>'tenant_id' and world=p_world and principal_id=binding->>'subject_principal_id' for share;
 if not found or not exists(select 1 from ontology.objects o where o.tenant_id=owner.tenant_id and o.world=p_world and o.type_name='ConsumerOwnership'
  and o.object_id=owner.ownership_id and o.properties=jsonb_build_object('consumer_id',owner.consumer_id,'principal_id',owner.principal_id)) then
  raise exception 'identity ownership unavailable' using errcode='42501';end if;
 select * into object from ontology.objects o where o.tenant_id=owner.tenant_id and o.world=p_world and o.type_name='Consumer' and o.object_id=owner.consumer_id for share;
 if not found then raise exception 'identity ownership unavailable' using errcode='42501';end if;
 ref:='nexloop:browser-local:'||encode(sha256(convert_to(owner.tenant_id||':'||p_world||':'||(binding->>'subject_id')||':'||(binding->>'credential_id'),'UTF8')),'hex');
 select * into prior from control.nexloop_consumer_identity_link_receipts where tenant_id=owner.tenant_id and world=p_world and identity_ref=ref;
 if found then
  if prior.consumer_id<>owner.consumer_id then raise exception 'identity link conflict' using errcode='23505';end if;
  return prior.request_payload;
 end if;
 refs:=coalesce(object.properties->'external_id_refs','[]'::jsonb);
 if jsonb_typeof(refs)<>'array' then raise exception 'identity profile unavailable' using errcode='23514';end if;
 select jsonb_agg(value order by value) into refs from (select distinct value from jsonb_array_elements(refs||jsonb_build_array(ref))) x;
 result:=jsonb_build_object('request_id','identity-link-'||runtime.nexloop_tenant_hmac(owner.tenant_id,owner.tenant_id||':'||p_world||':'||owner.consumer_id||':'||ref),  -- owner 2026-10-21: keyed
  'type_name','Consumer','object_id',owner.consumer_id,'expected_revision',object.nexloop_revision,'properties',jsonb_build_object('external_id_refs',refs));
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return result;
end $$;
