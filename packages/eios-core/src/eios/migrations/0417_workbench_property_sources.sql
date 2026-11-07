-- Claim → Consumer phase three, the profile read (design docs/implementation/claim-consumer-delivery-windows-design.md v2 §4.3, L6 C3):
-- where a structured Consumer property (a closed-vocabulary struct array, migration schema_structured_property) came from. The
-- migration number lives only in the file name and the catalog.
--
-- authz.nexloop_workbench_property_sources(p_digest,p_world,p_consumer): for an owner or operator, the same gate as
-- authz.nexloop_workbench_consumer_fields (0217). For every structured property of the Consumer's Schema version that the member may
-- read: the current value, and the latest applied mutation proposal for it - its Claim, the Claim's Conversation and source Message,
-- stated_at (the source Message's acceptance time, the order values replace each other by) and effective_at (when it was applied,
-- for display only). status: applied (a value), corrected (no value, an applied history: withdrawn through the correction chain) or
-- unknown (never obtained). A property the member may not read is not listed. Identifiers and times only: no Claim quote (the
-- evidence graph, 0406, decides who reads quotes). An erasing Consumer: null.
create function authz.nexloop_workbench_property_sources(p_digest text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w jsonb:=authz.nexloop_workbench_member(p_digest,p_world);o ontology.objects;def jsonb;
begin
 if w->>'role' not in ('owner','operator') or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then raise exception 'workbench member read forbidden' using errcode='42501';end if;
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 select * into o from ontology.objects where tenant_id=w->>'tenant_id' and world=p_world and type_name='Consumer' and object_id=p_consumer;
 if not found or runtime.nexloop_consumer_erasing(o.tenant_id,p_world,p_consumer) then return null;end if;
 select definition into def from ontology.object_type_versions where tenant_id=o.tenant_id and type_name='Consumer' and version=o.schema_version;
 return coalesce((select jsonb_agg(jsonb_build_object('property_name',x.name,'value',case when o.properties ? x.name then o.properties->x.name end,
    'status',case when o.properties ? x.name and jsonb_typeof(o.properties->x.name)<>'null' then 'applied' when a.claim_id is not null then 'corrected' else 'unknown' end,
    'source',case when a.claim_id is null or not (o.properties ? x.name) then null else jsonb_build_object('claim_id',a.claim_id,'conversation_id',c.conversation_id,
      'message_id',c.source_message_id) end,
    'stated_at',case when a.claim_id is not null and o.properties ? x.name then c.valid_time->>'anchor' end,
    'effective_at',case when a.claim_id is not null and o.properties ? x.name then to_char(a.updated_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') end)
   order by x.name)
  from (select p->>'property_name' as name from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) p
         where p->'type_descriptor'->>'kind'='array' and p->'type_descriptor'->'element'->>'kind'='struct'
          and authz.nexloop_workbench_member_may_read(o.tenant_id,p_world,w->>'role','eios:property:Consumer/'||p_consumer||'/'||(p->>'property_name'))) x
  left join lateral (select mp.claim_id,mp.updated_at from ontology.nexloop_mutation_proposals mp where mp.tenant_id=o.tenant_id and mp.world=p_world
     and mp.target_ref='eios:object:Consumer/'||p_consumer and mp.property_name=x.name and mp.status='applied' order by mp.updated_at desc limit 1) a on true
  left join ontology.nexloop_claims c on c.tenant_id=o.tenant_id and c.world=p_world and c.claim_id=a.claim_id),'[]'::jsonb);
end $$;
alter function authz.nexloop_workbench_property_sources(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_property_sources(text,text,text) from public;
grant execute on function authz.nexloop_workbench_property_sources(text,text,text) to nexloop_api;
