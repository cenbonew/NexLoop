-- Erasing indirect read (L6 world-model-visibility review 1-1): an object that belongs to a Consumer only through a property - a
-- Commitment (made_to), a CommercialRecord (consumer_ref), a RelationshipAssessment (source_id / target_id / evidence Message), any
-- other object naming consumer_id (MessageAssignment, ConsumerOwnership, ConsumerRoleLink, ConsumerServiceOffering, ...) - was not
-- seen as the Consumer's by the erasing read refusal: the read guard (runtime.nexloop_assert_not_erasing, 0134) resolved only a
-- Consumer, Conversation or Message target, and object_fields (0314) the same. While the Consumer was being erased (before the purge
-- removes or unlinks them) such objects stayed readable. Now one function, runtime.nexloop_object_consumers, resolves the Consumers of
-- any object (the links the erasure itself follows), and both the guard and object_fields call it. The guard keeps 0134's fast path
-- (no erasing Consumer in the tenant and world: one index probe, nothing resolved). Temporary number; the dispatcher renumbers.

create function runtime.nexloop_object_consumers(p_tenant text,p_world text,p_type text,p_object text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
-- The Consumers an object belongs to, directly or indirectly: the one resolution of the erasing read refusal (the read guard
-- runtime.nexloop_assert_not_erasing and the workbench object_fields). The same links the erasure follows when it purges (0132,
-- 0227 references phase, 0307): the Consumer itself; a Conversation and its Messages; a Commitment's made_to; a CommercialRecord's
-- consumer_ref; a RelationshipAssessment's source_id, target_id and its evidence Message's Consumer; any other object's consumer_id.
-- Ids only (64 hex); an unknown object or type has none. The caller has set eios.tenant_id.
declare o ontology.objects;out text[]:='{}';
begin
 if p_type='Consumer' then return array[p_object];end if;
 if p_type='Conversation' then
  return coalesce((select array[cv.consumer_id] from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world and cv.conversation_id=p_object),'{}');
 end if;
 if p_type='Message' then
  return coalesce((select array[cv.consumer_id] from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
    on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
    where m.tenant_id=p_tenant and m.world=p_world and m.message_id=p_object limit 1),'{}');
 end if;
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=p_type and object_id=p_object;
 if not found then return out;end if;
 out:=array_remove(array[o.properties->>'consumer_id',case p_type when 'Commitment' then o.properties->>'made_to' when 'CommercialRecord' then o.properties->>'consumer_ref' end],null);
 if p_type='RelationshipAssessment' then
  out:=out||array_remove(array[o.properties->>'source_id',o.properties->>'target_id'],null);
  out:=out||coalesce((select array[cv.consumer_id] from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
    on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
    where m.tenant_id=p_tenant and m.world=p_world and m.message_id=o.properties->>'evidence_message_id' limit 1),'{}');
 end if;
 return array(select distinct x from unnest(out) x where x~'^[a-f0-9]{64}$');
end $$;
alter function runtime.nexloop_object_consumers(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_object_consumers(text,text,text,text) from public;

create or replace function runtime.nexloop_assert_not_erasing(p_world text,p_claims jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare prior text:=current_setting('eios.tenant_id',true);t text:=p_claims->>'tenant_id';
 r text:=coalesce(p_claims->>'resource_id',p_claims->>'target_resource','');v text;c text;hit boolean:=false;
begin
 if coalesce(t,'')='' then return;end if;
 if prior is distinct from t then perform set_config('eios.tenant_id',t,true);end if;
 -- (a) No erasing Consumer in this tenant and world: nothing to refuse.
 if exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.state<>'pending_confirmation') then
  -- Erasing indirect read: the one resolution shared with object_fields (runtime.nexloop_object_consumers).
  v:=substring(r from '^eios:(?:object|property):([A-Z][A-Za-z0-9_]{0,63})/');
  if v is null and p_claims->>'type_name' is not null then v:=p_claims->>'type_name';end if;
  c:=coalesce(substring(r from '^eios:(?:object|property):[A-Z][A-Za-z0-9_]{0,63}/([A-Za-z0-9][A-Za-z0-9._:-]{0,159})'),p_claims->>'object_id');
  if v is not null and c is not null then
   hit:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.state<>'pending_confirmation'
    and e.consumer_id=any(runtime.nexloop_object_consumers(t,p_world,v,c)));
  end if;
 end if;
 if prior is distinct from t then perform set_config('eios.tenant_id',coalesce(prior,''),true);end if;
 if hit then raise exception 'consumer is being erased' using errcode='42501';end if;
end $$;

create or replace function authz.nexloop_workbench_object_fields(p_tenant text,p_world text,p_role text,p_type text,p_object text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare o ontology.objects;def jsonb;restriction jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=p_type and object_id=p_object;
 if not found then return null;end if;
 if not authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,'eios:object:'||p_type||'/'||p_object)
  -- Erasing indirect read: the Consumers the object belongs to, the resolution the read guard uses (runtime.nexloop_object_consumers).
  or exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=p_world and e.state<>'pending_confirmation'
   and e.consumer_id=any(runtime.nexloop_object_consumers(p_tenant,p_world,p_type,p_object))) then
  return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',false,'fields','[]'::jsonb);end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=o.schema_version;
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[p_type];
 return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',true,'schema_version',o.schema_version,'revision',o.nexloop_revision,
  'fields',coalesce((select jsonb_agg(jsonb_build_object('name',x.name,'present',x.present,'restricted',x.restricted,'erasing',x.erasing,
      'readable',not x.restricted and not x.erasing and authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,
        'eios:property:'||p_type||'/'||p_object||'/'||x.name)) order by x.ord)
    from (select p.value->>'property_name' as name,p.ordinality ord,
       (o.properties ? (p.value->>'property_name') and o.properties->(p.value->>'property_name')<>'null'::jsonb) present,
       coalesce(restriction->'restricted_groups' ? ontology.nexloop_property_group_of(def,p.value->>'property_name'),false) restricted,
       -- NX-029: a value that names a Consumer being erased (an id, or an array of ids) is never shown.
       exists(select 1 from jsonb_array_elements(case jsonb_typeof(o.properties->(p.value->>'property_name')) when 'array' then o.properties->(p.value->>'property_name')
          when 'string' then jsonb_build_array(o.properties->(p.value->>'property_name')) else '[]'::jsonb end) e
         where jsonb_typeof(e)='string' and e#>>'{}'~'^[a-f0-9]{64}$' and runtime.nexloop_consumer_erasing(p_tenant,p_world,e#>>'{}')) erasing
      from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) with ordinality p
      where coalesce(p.value->>'property_name','')~'^[A-Za-z][A-Za-z0-9_]{0,63}$') x),'[]'::jsonb));
end $$;
