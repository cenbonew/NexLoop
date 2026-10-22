-- EIOS gap A1 (NX-061): the published Definitions a Schema proposal's type may affect, for the reviewer's pre-publication impact
-- list (nexloop_eios.schema_impact over the vendored eios.ontology.impact graph). Read-only: the same review read guard as the
-- publication basis (0254: ontology.schema.review EXECUTE of a current Human session, the caller's own tenant), nothing written.
-- Rows are a coarse prefilter (the definition mentions the type name); which of them depend on the type or on a changed property
-- is decided by the dependency graph. The latest version of each Object Set and Object View, the active Functions and Actions,
-- at most 500 of each kind (truncated says so).
create function authz.nexloop_read_schema_dependents(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 r ontology.nexloop_schema_proposals;needle text;cap constant integer:=500;sets jsonb;views jsonb;fns jsonb;acts jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if coalesce(c->>'proposal_id','')!~'^[0-9a-f-]{36}$' then raise exception 'schema dependents invalid' using errcode='22023';end if;
 select * into r from ontology.nexloop_schema_proposals where tenant_id=t and world=p_world and proposal_id=(c->>'proposal_id')::uuid;
 if not found then return null;end if;
 needle:='"'||r.type_name||'"';
 select coalesce(jsonb_agg(d.definition order by d.stable_name),'[]'::jsonb) into sets from (
  select l.stable_name,l.definition from (
   select distinct on (s.stable_name) s.stable_name,s.definition from control.nexloop_object_set_definitions s
    where s.tenant_id=t and s.world='real' order by s.stable_name,s.version desc) l
  where strpos(l.definition::text,needle)>0 order by l.stable_name limit cap+1) d;
 select coalesce(jsonb_agg(d.definition order by d.stable_name),'[]'::jsonb) into views from (
  select l.stable_name,l.definition from (
   select distinct on (v.stable_name) v.stable_name,v.definition from control.nexloop_object_view_definitions v
    where v.tenant_id=t and v.world='real' order by v.stable_name,v.version desc) l
  where strpos(l.definition::text,needle)>0 order by l.stable_name limit cap+1) d;
 select coalesce(jsonb_agg(d.definition order by d.resource_id),'[]'::jsonb) into fns from (
  select f.resource_id,f.definition from control.nexloop_function_definitions f
   where f.tenant_id=t and f.world='real' and f.active and strpos(f.definition::text,needle)>0 order by f.resource_id limit cap+1) d;
 select coalesce(jsonb_agg(d.definition order by d.resource_id),'[]'::jsonb) into acts from (
  select a.resource_id,a.definition from control.nexloop_action_definitions a
   where a.tenant_id=t and a.world='real' and a.active and strpos(a.definition::text,needle)>0 order by a.resource_id limit cap+1) d;
 return jsonb_build_object('proposal_id',r.proposal_id,'type_name',r.type_name,'operation',r.operation,'change',r.change,
  'truncated',jsonb_array_length(sets)>cap or jsonb_array_length(views)>cap or jsonb_array_length(fns)>cap or jsonb_array_length(acts)>cap,
  'object_sets',sets,'object_views',views,'functions',fns,'actions',acts);
end $$;
alter function authz.nexloop_read_schema_dependents(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_schema_dependents(text,text,text,text,text) from public;
grant execute on function authz.nexloop_read_schema_dependents(text,text,text,text,text) to nexloop_api;
