-- NX-026 / ADR-023 §3, D6 (temporary number 0112): a contact restriction stops only effects that reach the customer.
--
-- 1. Effect category per published Action definition, in a side table written only by trusted configuration
--    (nexloop_configurator, control.nexloop_configure_effect_categories): customer_contact or non_contact_service,
--    plus the parameters of a non-contact Action that carry a customer notification. Frozen by the digest of the
--    published definition: an intent whose frozen definition has no row with the same digest is undeclared.
--    This side table is the implementation of ADR-023 §3 "declared in the Action definition" (dispatcher
--    confirmation 2026-10-10): the vendored EIOS ActionDefinition and published definition digests stay unchanged.
-- 2. Dispatch under a restriction (0109/0110 control.nexloop_contact_assert_intent, kept as a private alias): an
--    intent passes directly only when its Action is declared non_contact_service with the same digest, it has no
--    NX-047 outbound record and every declared notification parameter is empty. An attached notification refuses the
--    whole intent (NXC05 attached_notification: one intent is one provider request, it cannot be split). Undeclared,
--    customer-contact and anything else keep the 0109/0110 rules (only a bound reply to an inbound message passes).
-- 3. Commitment evidence (0111) uses the same categories: only a declared non-contact service delivery without a
--    notification is an effect receipt; any other fulfilled effect bound to a commitment is a delivered message.

create table control.nexloop_action_effect_categories (
 tenant_id text not null,world text not null,resource_id text not null check(resource_id~'^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),
 category text not null check(category in ('customer_contact','non_contact_service')),
 notification_parameters text[] not null check(cardinality(notification_parameters)<=32),
 manifest_version integer not null check(manifest_version>=1),configured_by text not null default session_user,
 configured_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,resource_id,definition_digest),
 check(category='non_contact_service' or cardinality(notification_parameters)=0)
);
alter table control.nexloop_action_effect_categories owner to nexloop_owner;
alter table control.nexloop_action_effect_categories enable row level security;
alter table control.nexloop_action_effect_categories force row level security;
create policy tenant_boundary on control.nexloop_action_effect_categories to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_action_effect_categories from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger nx026_append_only before update or delete on control.nexloop_action_effect_categories for each row execute function control.nexloop_nx022_append_only();

create function control.nexloop_definition_digest(p_definition jsonb) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(p_definition::text,'UTF8')),'hex')
$$;
alter function control.nexloop_definition_digest(jsonb) owner to nexloop_owner;

-- Trusted configuration only: categories of the tenant's currently published Action definitions.
create function control.nexloop_configure_effect_categories(p_tenant text,p_world text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e jsonb;d jsonb;v_resource text;v_digest text;v_params text[];p text;n integer:=0;
 cur control.nexloop_action_effect_categories;
begin
 if session_user<>'nexloop_configurator' then raise exception 'effect categories are trusted configuration only' using errcode='42501';end if;
 if p_world is distinct from 'real' or p_manifest->>'schema_version' is distinct from 'nexloop-action-effect-categories/1'
  or jsonb_typeof(p_manifest->'manifest_version') is distinct from 'number' or jsonb_typeof(p_manifest->'actions') is distinct from 'array'
  or jsonb_array_length(p_manifest->'actions') not between 1 and 256 then raise exception 'effect categories manifest invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 for e in select value from jsonb_array_elements(p_manifest->'actions') loop
  if jsonb_typeof(e) is distinct from 'object' or (select count(*) from jsonb_object_keys(e))<>5
   or not (e ?& array['stable_name','version','category','notification_parameters','purpose']) or e->>'category' not in ('customer_contact','non_contact_service')
   or jsonb_typeof(e->'notification_parameters') is distinct from 'array' or jsonb_typeof(e->'version') is distinct from 'number' then
   raise exception 'effect category entry invalid' using errcode='22023';end if;
  v_resource:='eios:action:'||(e->>'stable_name')||':'||(e->>'version');
  select definition into d from control.nexloop_action_definitions where tenant_id=p_tenant and world=p_world and resource_id=v_resource and active;
  if not found then raise exception 'effect category for an unpublished Action: %',v_resource using errcode='22023';end if;
  select coalesce(array_agg(x order by x),'{}') into v_params from jsonb_array_elements_text(e->'notification_parameters') x;
  -- Notification parameters must be declared inputs of the Action.
  foreach p in array v_params loop
   if not (coalesce(d->'input_schema'->'properties','{}'::jsonb) ? p) then raise exception 'notification parameter % not an input of %',p,v_resource using errcode='22023';end if;
  end loop;
  v_digest:=control.nexloop_definition_digest(d);
  select * into cur from control.nexloop_action_effect_categories c where c.tenant_id=p_tenant and c.world=p_world and c.resource_id=v_resource and c.definition_digest=v_digest;
  if found then
   if cur.category<>e->>'category' or cur.notification_parameters<>v_params then raise exception 'effect category of % is immutable',v_resource using errcode='22023';end if;
  else
   insert into control.nexloop_action_effect_categories(tenant_id,world,resource_id,definition_digest,category,notification_parameters,manifest_version)
    values(p_tenant,p_world,v_resource,v_digest,e->>'category',v_params,(p_manifest->>'manifest_version')::integer);
   n:=n+1;
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('configured',true,'written',n);
end $$;
alter function control.nexloop_configure_effect_categories(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_effect_categories(text,text,jsonb) from public;
grant usage on schema control to nexloop_configurator;
grant execute on function control.nexloop_configure_effect_categories(text,text,jsonb) to nexloop_configurator;

-- Declared category of an intent's frozen Action definition, or null (undeclared or digest changed).
create function control.nexloop_intent_effect_category(p_tenant text,p_world text,p_intent uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;c control.nexloop_action_effect_categories;v_result jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if found then
  select * into c from control.nexloop_action_effect_categories x where x.tenant_id=p_tenant and x.world=p_world
   and x.resource_id='eios:action:'||i.action_name||':'||i.action_version and x.definition_digest=control.nexloop_definition_digest(i.action_definition);
  if found then
   v_result:=jsonb_build_object('category',c.category,'notification_parameters',to_jsonb(c.notification_parameters),
    'outbound',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=p_intent),
    'notification',exists(select 1 from unnest(c.notification_parameters) p where coalesce(i.frozen_request->'parameters'->>p,'')<>''));
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_result;
end $$;
alter function control.nexloop_intent_effect_category(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_intent_effect_category(text,text,uuid) from public;

-- Dispatch: 0110 rules kept as a private alias; a declared non-contact service delivery without notification passes.
alter function control.nexloop_contact_assert_intent(text,text,uuid) rename to nexloop_contact_assert_intent_before_effect_category_v0110;
create function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_consumer text;v_category jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select consumer_id into v_consumer from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if v_consumer is not null and exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world=p_world and r.consumer_id=v_consumer and r.active) then
  v_category:=control.nexloop_intent_effect_category(p_tenant,p_world,p_intent);
  if v_category->>'category'='non_contact_service' and not (v_category->>'outbound')::boolean then
   if (v_category->>'notification')::boolean then
    raise exception 'contact restricted: attached_notification (send it as a reply bound to an inbound message)' using errcode='NXC05';end if;
   perform set_config('eios.tenant_id',coalesce(prior,''),true);
   return;
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 perform control.nexloop_contact_assert_intent_before_effect_category_v0110(p_tenant,p_world,p_intent);
end $$;
alter function control.nexloop_contact_assert_intent(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_contact_assert_intent(text,text,uuid) from public;

-- Commitment evidence (0111): an effect receipt only for a declared non-contact service delivery without notification.
create or replace function runtime.nexloop_commitment_effect_kind(p_tenant text,p_world text,p_intent uuid) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_category jsonb:=control.nexloop_intent_effect_category(p_tenant,p_world,p_intent);
begin
 if v_category->>'category'='non_contact_service' and not (v_category->>'outbound')::boolean and not (v_category->>'notification')::boolean then
  return 'effect_fulfilled';end if;
 return 'delivered_message';
end $$;
