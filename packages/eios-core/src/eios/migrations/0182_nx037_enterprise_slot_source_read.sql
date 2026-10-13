-- ADR-030 (NX-037 gap B slice 2): the relay Source reads the enterprise slot's offering and binding as it reads the reply ones.
-- An enterprise intent of a Message Run builds its catalog envelope from the Source's signed READ of the enrolled enterprise
-- ServiceOffering and ConsumerServiceOffering (service_offerings._catalog_envelope). The 0142 relay-source-v1 derivation
-- covers only the reply offering/binding columns of the enrollment, so the enterprise intent was refused before admission.
-- The derivation is wrapped: the 0142 body is unchanged (renamed); when it derives nothing, the same rule is applied to the
-- current active enrollment's enterprise columns (0180): same relay rule, same fixed field lists, same erasure and
-- owner-restricted-group exclusions, binding naming this Source and this consumer and this offering. The result has the same
-- shape. Validity of the offering itself stays with authz.nexloop_assert_service_offering at submission and at dispatch.
alter function authz.nexloop_relay_source_derivation(text,text,text,text,text) rename to nexloop_relay_source_derivation_before_enterprise_v0404;
revoke all on function authz.nexloop_relay_source_derivation_before_enterprise_v0404(text,text,text,text,text) from public;

create function authz.nexloop_relay_source_derivation(p_tenant text,p_principal text,p_world text,p_resource text,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare base jsonb;m text[];t text;oid text;prop text;rule jsonb;restriction jsonb;e control.nexloop_relay_enrollments;v_binding ontology.objects;
 v_object ontology.objects;def jsonb;grp text;
begin
 base:=authz.nexloop_relay_source_derivation_before_enterprise_v0404(p_tenant,p_principal,p_world,p_resource,p_operation);
 if base is not null then return base;end if;
 if p_world is distinct from 'real' or p_operation is distinct from 'read' then return null;end if;
 m:=regexp_match(p_resource,'^eios:object:(ServiceOffering|ConsumerServiceOffering)/([a-f0-9]{64})$');
 if m is not null then t:=m[1];oid:=m[2];
 else
  m:=regexp_match(p_resource,'^eios:property:(ServiceOffering|ConsumerServiceOffering)/([a-f0-9]{64})/([a-z_]{1,64})$');
  if m is null then return null;end if;
  t:=m[1];oid:=m[2];prop:=m[3];
  if (t='ServiceOffering' and prop not in ('active','allowed_discounts','allowed_guarantees','content_kind','currency','delivery_action','eligibility',
       'evidence_kind','price_amount','service_code','title','valid_until'))
   or (t='ConsumerServiceOffering' and prop not in ('active','consumer_id','offering_id','offering_revision','source_principal')) then return null;end if;
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select payload into rule from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='relay_source_read_rule' and entity_key=array[p_principal] for share;
 if not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp() then return null;end if;
 -- The current enrollment of each consumer that ever named this object in its enterprise slot; it must still name it.
 for e in select x.* from (select distinct on (y.consumer_id) y.* from control.nexloop_relay_enrollments y where y.tenant_id=p_tenant and y.world=p_world
     and y.consumer_id in (select z.consumer_id from control.nexloop_relay_enrollments z where z.tenant_id=p_tenant and z.world=p_world
       and ((t='ServiceOffering' and z.enterprise_offering_id=oid) or (t='ConsumerServiceOffering' and z.enterprise_binding_id=oid)))
     order by y.consumer_id,y.enrollment_revision desc) x
   where x.state='active' and x.relay_principal=rule->>'relay_principal'
     and ((t='ServiceOffering' and x.enterprise_offering_id=oid) or (t='ConsumerServiceOffering' and x.enterprise_binding_id=oid))
   order by x.consumer_id
 loop
  if runtime.nexloop_consumer_erasing(p_tenant,p_world,e.consumer_id) then continue;end if;
  perform set_config('eios.tenant_id',p_tenant,true);
  select * into v_binding from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.enterprise_binding_id and type_name='ConsumerServiceOffering' for share;
  if v_binding.object_id is null or v_binding.properties->>'source_principal' is distinct from p_principal or v_binding.properties->>'consumer_id' is distinct from e.consumer_id
   or v_binding.properties->>'offering_id' is distinct from e.enterprise_offering_id then continue;end if;
  select * into v_object from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=oid and type_name=t for share;
  if v_object.object_id is null then return null;end if;
  grp:=null;restriction:=null;
  if prop is not null then
   select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=t and version=v_object.schema_version for share;
   if def is null or not exists(select 1 from jsonb_array_elements(def->'properties') d where d->>'property_name'=prop) then return null;end if;
   grp:=ontology.nexloop_property_group_of(def,prop);
   select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[t] for share;
   if grp is not null and restriction->'restricted_groups' ? grp then return null;end if;
  end if;
  return jsonb_build_object('mode','derived','derivation','relay-source-v1','type_name',t,'consumer_id',e.consumer_id,'enrollment_revision',e.enrollment_revision,
   'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until',
   'restriction_hash',case when restriction is null then null else encode(sha256(convert_to(restriction::text,'UTF8')),'hex') end);
 end loop;
 return null;
end $$;
alter function authz.nexloop_relay_source_derivation(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relay_source_derivation(text,text,text,text,text) from public;
