-- NX-023-A: read-only Context inputs. One signed definer, three verbs:
--   strategy     current/exact strategy version   (EXECUTE eios:action:nexloop.context.assemble:1)
--   definitions  semantic definition details       (current READ proof per gate: type and property)
--   open_work    unconfirmed execution for a Consumer (current READ on eios:object:Consumer/<id>)
-- Nothing is written; tenant and world come from the verified credential; definitions come
-- only from EIOS schema tables and the governed alias table (no second definition store).
create function authz.nexloop_context_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';ident jsonb;v_tenant text;
 v_result jsonb;v_row control.nexloop_context_strategies%rowtype;proof jsonb;ref text;gate text;v_type text;v_prop text;v_value text;
 v_def jsonb;v_version integer;v_items jsonb:='[]'::jsonb;v_props jsonb;v_consumer text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>4194304 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-context-read-v1' or v_verb not in ('strategy','definitions','open_work') or v_verb is null
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'world' is distinct from p_world then raise exception 'context read unavailable' using errcode='42501';end if;
 if v_verb='strategy' then
  if a->>'action_resource' is distinct from 'eios:action:nexloop.context.assemble:1' or c->>'strategy_id' !~ '^[a-z][a-z0-9_]{0,63}$'
   or (c ? 'version' and jsonb_typeof(c->'version') is distinct from 'number') then raise exception 'context read unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  select * into v_row from control.nexloop_context_strategies where tenant_id=v_tenant and world=p_world and strategy_id=c->>'strategy_id'
   and (not c ? 'version' or version=(c->>'version')::integer) order by version desc limit 1;
  if not found then v_result:=null;else
   v_result:=jsonb_build_object('strategy_ref','context-strategy:'||v_row.strategy_id||'@'||v_row.version,'definition',v_row.definition,
    'definition_digest',v_row.definition_digest,'published_at',v_row.published_at);end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return v_result;
 end if;
 if jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')>256 then raise exception 'context read unavailable' using errcode='42501';end if;
 -- Every proof must be the caller's own, current, and for exactly the resource it names.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if proof->>'tenant_id' is distinct from v_tenant or proof->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id'
   or proof->>'operation' is distinct from 'read' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then
   raise exception 'context read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='definitions' then
  if jsonb_typeof(c->'refs') is distinct from 'array' or jsonb_array_length(c->'refs') not between 1 and 64 then raise exception 'context read unavailable' using errcode='42501';end if;
  for ref in select value#>>'{}' from jsonb_array_elements(c->'refs') loop
   v_type:=null;v_prop:=null;v_value:=null;v_props:=null;
   if ref ~ '^eios:object_type:[A-Za-z][A-Za-z0-9_]{0,63}$' then v_type:=substring(ref from 18);
   elsif ref ~ '^eios:property:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}$' then
    v_type:=split_part(substring(ref from 15),'/',1);v_prop:=split_part(substring(ref from 15),'/',2);
   elsif ref ~ '^nexloop:vocabulary:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}/.+$' then
    v_type:=split_part(substring(ref from 20),'/',1);v_prop:=split_part(substring(ref from 20),'/',2);
    v_value:=substring(substring(ref from 20) from length(v_type)+length(v_prop)+3);
   else raise exception 'context definition ref invalid' using errcode='22023';end if;
   -- Gates: the type always; the property for property/vocabulary refs (same as NX-021 recall gates).
   foreach gate in array array_remove(array['eios:object_type:'||v_type,case when v_prop is null then null else 'eios:property:'||v_type||'/'||v_prop end],null) loop
    if not exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'=gate and q->>'resource_id'=gate) then
     raise exception 'context definition READ required' using errcode='42501';end if;
   end loop;
   select definition,version into v_def,v_version from ontology.object_type_versions where tenant_id=v_tenant and type_name=v_type order by version desc limit 1;
   if not found then raise exception 'context definition unavailable' using errcode='42501';end if;
   if v_prop is null then
    -- A type view lists only the properties the caller may READ.
    select coalesce(jsonb_agg(p->>'property_name' order by p->>'property_name'),'[]'::jsonb) into v_props from jsonb_array_elements(v_def->'properties') p
     where exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'='eios:property:'||v_type||'/'||(p->>'property_name'));
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind','object_type','schema_version',v_version,
     'display_name',v_def->>'display_name','description',coalesce(v_def->>'description',''),'readable_properties',v_props,
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   else
    select value into v_props from jsonb_array_elements(v_def->'properties') where value->>'property_name'=v_prop;
    if v_props is null then raise exception 'context definition unavailable' using errcode='42501';end if;
    if v_value is not null and not coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb) ? replace(replace(replace(replace(replace(replace(replace(v_value,'%20',' '),'%09',chr(9)),'%0A',chr(10)),'%0D',chr(13)),'%0C',chr(12)),'%0B',chr(11)),'%25','%') then
     raise exception 'context definition unavailable' using errcode='42501';end if;
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind',case when v_value is null then 'property' else 'vocabulary_value' end,
     'schema_version',v_version,'property_ref','eios:property:'||v_type||'/'||v_prop,'display_name',v_props->>'display_name','description',coalesce(v_props->>'description',''),
     'value_type',v_props->>'value_type','closed_vocabulary',coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb),
     'group',(select g->>'group_name' from jsonb_array_elements(coalesce(v_def->'property_groups','[]'::jsonb)) g where g->'property_names' ? v_prop limit 1),
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   end if;
  end loop;
  v_result:=jsonb_build_object('items',v_items);
 else
  v_consumer:=c->>'consumer_id';
  if v_consumer !~ '^[0-9a-f]{64}$' or v_consumer is null or not exists(select 1 from jsonb_array_elements(a->'read_proofs') q
    where q->>'target_resource'='eios:object:Consumer/'||v_consumer and q->>'resource_id'='eios:object:Consumer/'||v_consumer) then
   raise exception 'context open work requires Consumer READ' using errcode='42501';end if;
  if not exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
   raise exception 'context consumer unavailable' using errcode='42501';end if;
  v_result:=jsonb_build_object(
   'intents',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',i.intent_id::text,'action',i.action_name||':'||i.action_version,'state',i.state,
      'created_at',i.created_at) order by i.created_at,i.intent_id),'[]'::jsonb) from runtime.nexloop_effect_intents i
     where i.tenant_id=v_tenant and i.world=p_world and i.consumer_id=v_consumer and i.state in ('accepted','dispatching','unknown')),
   'outbound',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',o.intent_id::text,'conversation_id',o.conversation_id,'delivery_state',o.delivery_state,
      'persisted_at',o.persisted_at,'delivery_changed_at',o.delivery_changed_at,'message_id',o.message_id,'body_digest',o.body_digest)
      order by o.persisted_at,o.intent_id),'[]'::jsonb) from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations cv
      on cv.tenant_id=o.tenant_id and cv.world=o.world and cv.conversation_id=o.conversation_id
     where o.tenant_id=v_tenant and o.world=p_world and cv.consumer_id=v_consumer));
 end if;
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 return v_result;
end $$;
alter function authz.nexloop_context_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_read(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_context_read(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
