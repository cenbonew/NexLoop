-- NX-040 F2, migration A: Schema proposals (docs/implementation/NX-040-design.md §3, §9; D21-D27, owner 2026-10-20). The migration
-- number lives only in the file name and the catalog; nothing below repeats it.
-- 1. ontology.nexloop_schema_proposals: one frozen input of the EIOS Schema review per proposal (contract schema-proposal 1.0). The
--    input is fixed at freeze and never changes (a changed change is a new proposal): the ontology_schema candidate it comes from,
--    the §4.1 whitelist operation and its structured change, the parent Object Type version and the digest of its definition (both
--    read by the server, never chosen by the caller), source ids (the candidate's diagnosis snapshot digest, candidate definition ids,
--    Claim ids) and the compatibility report and permission proof digests. Status is frozen | published | withdrawn only (NX-039
--    §12): evaluation state stays on the candidate. A proposal changes no Schema, publishes nothing, grants nothing and bypasses no
--    EIOS step: publication is F3's, through the existing review decision (authz.nexloop_review_decide) with an NX-039 permit.
-- 2. ontology.nexloop_schema_proposal_events: append-only (frozen, published, withdrawn).
-- 3. Writes only through security definer functions run by the evolution writer (nexloop_evo_writer, its service credential gives
--    the tenant): freeze, withdraw and read. runtime.nexloop_schema_proposal_mark_published is internal (no grant) for F3's
--    publication transaction. Row security per tenant; owner nexloop_owner.

create table ontology.nexloop_schema_proposals (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),proposal_id uuid not null,
 candidate_id uuid not null,operation text not null check(operation in ('add_optional_property','add_vocabulary_value','add_object_type')),
 type_name text not null check(type_name~'^[A-Za-z][A-Za-z0-9_]{0,127}$'),
 target_ref text not null check(target_ref~'^eios:(object_type|property):[A-Za-z][A-Za-z0-9_.]{0,200}$'),
 change jsonb not null check(jsonb_typeof(change)='object' and octet_length(change::text)<=16384),change_digest text not null check(change_digest~'^[0-9a-f]{64}$'),
 parent_schema_version integer not null check(parent_schema_version>=0),parent_schema_digest text not null check(parent_schema_digest~'^[0-9a-f]{64}$'),
 input_snapshot_digest text not null check(input_snapshot_digest~'^[0-9a-f]{64}$'),
 candidate_definition_ids text[] not null default '{}' check(cardinality(candidate_definition_ids)<=64),
 claim_ids text[] not null default '{}' check(cardinality(claim_ids)<=256),
 compatibility_report_digest text not null check(compatibility_report_digest~'^[0-9a-f]{64}$'),
 permission_proof_digest text not null check(permission_proof_digest~'^[0-9a-f]{64}$'),
 status text not null default 'frozen' check(status in ('frozen','published','withdrawn')),
 permit_ref text check(permit_ref~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),withdraw_reason text check(withdraw_reason~'^[a-z_]{1,64}$'),
 frozen_by text not null,frozen_at timestamptz not null default clock_timestamp(),finished_at timestamptz,
 primary key(tenant_id,world,proposal_id),
 foreign key(tenant_id,world,candidate_id) references runtime.nexloop_evo_candidates(tenant_id,world,candidate_id),
 check((status='published')=(permit_ref is not null)),check((status='withdrawn')=(withdraw_reason is not null)),
 check((status='frozen')=(finished_at is null)),
 check((operation='add_object_type')=(parent_schema_version=0)),
 check(array_to_string(claim_ids,',')~'^([0-9a-f]{64}(,[0-9a-f]{64})*)?$'),
 check(array_to_string(candidate_definition_ids,',')~'^([0-9a-f-]{36}(,[0-9a-f-]{36})*)?$')
);
create index nexloop_schema_proposals_candidate on ontology.nexloop_schema_proposals(tenant_id,world,candidate_id);
create table ontology.nexloop_schema_proposal_events (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,event_id bigint generated always as identity,proposal_id uuid not null,
 from_status text check(from_status in ('frozen','published','withdrawn')),to_status text not null check(to_status in ('frozen','published','withdrawn')),
 reason text check(reason~'^[a-z_]{1,64}$'),recorded_by text not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,event_id),
 foreign key(tenant_id,world,proposal_id) references ontology.nexloop_schema_proposals(tenant_id,world,proposal_id)
);
do $t$
declare t text;
begin
 foreach t in array array['ontology.nexloop_schema_proposals','ontology.nexloop_schema_proposal_events'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';
  execute 'revoke all on '||t||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_evo_writer';
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
 end loop;
end $t$;

-- Frozen means frozen: only the status moves, once, from frozen to published (with its permit) or withdrawn (with a reason).
create function ontology.nexloop_schema_proposal_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_moving text[]:=array['status','permit_ref','withdraw_reason','finished_at'];
begin
 if tg_op='DELETE' then raise exception 'schema proposals are never deleted' using errcode='42501';end if;
 if (to_jsonb(new)-v_moving) is distinct from (to_jsonb(old)-v_moving) then raise exception 'schema proposal is frozen' using errcode='42501';end if;
 if old.status<>'frozen' or new.status not in ('published','withdrawn') then raise exception 'schema proposal % cannot become %',old.status,new.status using errcode='42501';end if;
 return new;
end $$;
alter function ontology.nexloop_schema_proposal_guard() owner to nexloop_owner;
create trigger schema_proposal_guard before update or delete on ontology.nexloop_schema_proposals for each row execute function ontology.nexloop_schema_proposal_guard();
create function ontology.nexloop_schema_proposal_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'schema proposal events are append-only' using errcode='42501';end $$;
alter function ontology.nexloop_schema_proposal_events_append_only() owner to nexloop_owner;
create trigger schema_proposal_events_append_only before update or delete on ontology.nexloop_schema_proposal_events for each row
 execute function ontology.nexloop_schema_proposal_events_append_only();

-- The contract value (ids, digests, codes; never the structured change itself).
create function runtime.nexloop_schema_proposal_wire(x ontology.nexloop_schema_proposals) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('schema_version','1.0','proposal_id',x.proposal_id,'tenant_id',x.tenant_id,'candidate_id',x.candidate_id,'operation',x.operation,
  'target_ref',x.target_ref,'parent_schema_version',x.parent_schema_version,'parent_schema_digest',x.parent_schema_digest,'change_digest',x.change_digest,
  'source',jsonb_build_object('input_snapshot_digest',x.input_snapshot_digest,'candidate_definition_ids',to_jsonb(x.candidate_definition_ids),'claim_ids',to_jsonb(x.claim_ids)),
  'compatibility_report_digest',x.compatibility_report_digest,'permission_proof_digest',x.permission_proof_digest,'status',x.status,'permit_ref',x.permit_ref,
  'frozen_at',to_char(x.frozen_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'))
$$;
alter function runtime.nexloop_schema_proposal_wire(ontology.nexloop_schema_proposals) owner to nexloop_owner;
revoke all on function runtime.nexloop_schema_proposal_wire(ontology.nexloop_schema_proposals) from public;

-- The §4.1 structured change, exactly: a property (optional, nullable) to add, a vocabulary value to add to an existing property,
-- or a new Object Type with its primary key and properties. Names and EIOS value types only.
create function runtime.nexloop_schema_change_valid(p_operation text,p_type text,p_change jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(case p_operation
  when 'add_optional_property' then (select array_agg(k order by k) from jsonb_object_keys(p_change) k)=array['property_name','value_type']
   and p_change->>'property_name'~'^[a-z][a-z0-9_]{0,63}$' and p_change->>'value_type' in ('string','integer','number','boolean','datetime','json')
  when 'add_vocabulary_value' then (select array_agg(k order by k) from jsonb_object_keys(p_change) k)=array['property_name','value']
   and p_change->>'property_name'~'^[a-z][a-z0-9_]{0,63}$' and jsonb_typeof(p_change->'value')='string' and char_length(p_change->>'value') between 1 and 200
  when 'add_object_type' then (select array_agg(k order by k) from jsonb_object_keys(p_change) k)=array['primary_key','properties']
   and jsonb_typeof(p_change->'properties')='array' and jsonb_array_length(p_change->'properties') between 1 and 64
   and not exists(select 1 from jsonb_array_elements(p_change->'properties') e where jsonb_typeof(e)<>'object'
     or (select array_agg(k order by k) from jsonb_object_keys(e) k) is distinct from array['property_name','required','value_type']
     or e->>'property_name'!~'^[a-z][a-z0-9_]{0,63}$' or e->>'value_type' not in ('string','integer','number','boolean','datetime','json')
     or jsonb_typeof(e->'required')<>'boolean')
   and jsonb_typeof(p_change->'primary_key')='array' and jsonb_array_length(p_change->'primary_key') between 1 and 8
   and not exists(select 1 from jsonb_array_elements_text(p_change->'primary_key') k where not exists(
     select 1 from jsonb_array_elements(p_change->'properties') e where e->>'property_name'=k and (e->>'required')::boolean))
  else false end,false)
$$;
alter function runtime.nexloop_schema_change_valid(text,text,jsonb) owner to nexloop_owner;

-- 3. Freeze: the server fixes the parent version and digest and the candidate's snapshot; the caller gives the change and the ids.
create function authz.nexloop_schema_proposal_freeze(p_digest text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);b jsonb:=p_body;x runtime.nexloop_evo_candidates;old ontology.nexloop_schema_proposals;
 v_type text:=b->>'type_name';v_op text:=b->>'operation';v_target text;v_version integer;v_definition jsonb;v_parent text;v_change_digest text;
 v_claims text[];v_definitions text[];r ontology.nexloop_schema_proposals;
begin
 if jsonb_typeof(b) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(b) k) is distinct from
   array['candidate_definition_ids','candidate_id','change','claim_ids','compatibility_report_digest','operation','permission_proof_digest','proposal_id','type_name']
  or coalesce(b->>'proposal_id','')!~'^[0-9a-f-]{36}$' or coalesce(b->>'candidate_id','')!~'^[0-9a-f-]{36}$'
  or coalesce(v_type,'')!~'^[A-Za-z][A-Za-z0-9_]{0,127}$' or v_op is null or v_op not in ('add_optional_property','add_vocabulary_value','add_object_type')
  or coalesce(b->>'compatibility_report_digest','')!~'^[0-9a-f]{64}$' or coalesce(b->>'permission_proof_digest','')!~'^[0-9a-f]{64}$'
  or jsonb_typeof(b->'claim_ids') is distinct from 'array' or jsonb_typeof(b->'candidate_definition_ids') is distinct from 'array'
  or jsonb_typeof(b->'change') is distinct from 'object' or not runtime.nexloop_schema_change_valid(v_op,v_type,b->'change') then
  raise exception 'schema proposal invalid' using errcode='22023';end if;
 v_claims:=array(select e#>>'{}' from jsonb_array_elements(b->'claim_ids') e);
 v_definitions:=array(select e#>>'{}' from jsonb_array_elements(b->'candidate_definition_ids') e);
 select * into old from ontology.nexloop_schema_proposals where tenant_id=v_tenant and world='real' and proposal_id=(b->>'proposal_id')::uuid;
 if found then  -- a replay of the same freeze returns it; anything else is a new proposal, never an edit
  if old.candidate_id<>(b->>'candidate_id')::uuid or old.operation<>v_op or old.type_name<>v_type or old.change<>b->'change'
   or old.claim_ids<>v_claims or old.candidate_definition_ids<>v_definitions or old.compatibility_report_digest<>b->>'compatibility_report_digest'
   or old.permission_proof_digest<>b->>'permission_proof_digest' then
   raise exception 'schema proposal is frozen' using errcode='42501';end if;
  return jsonb_build_object('frozen',true,'replayed',true,'proposal',runtime.nexloop_schema_proposal_wire(old));
 end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=(b->>'candidate_id')::uuid for share;
 if not found then raise exception 'schema proposal candidate unknown' using errcode='22023';end if;
 if x.dimension<>'ontology_schema' then raise exception 'schema proposal needs an ontology_schema candidate' using errcode='22023';end if;
 if x.status not in ('proposed','evaluating','accepted_for_release') then raise exception 'schema proposal candidate is %',x.status using errcode='22023';end if;
 v_target:=case when v_op='add_object_type' then 'eios:object_type:'||v_type else 'eios:property:'||v_type||'.'||(b->'change'->>'property_name') end;
 if not (v_target=any(x.allowed_change_scope) or ('eios:object_type:'||v_type)=any(x.allowed_change_scope)) then
  raise exception 'schema proposal outside the candidate scope' using errcode='42501';end if;  -- AT-024
 -- The parent: the latest published version of the type and the digest of its definition, read now and frozen.
 select version,definition into v_version,v_definition from ontology.object_type_versions where tenant_id=v_tenant and type_name=v_type order by version desc limit 1;
 if v_op='add_object_type' then
  if v_version is not null then raise exception 'schema proposal: object type exists' using errcode='22023';end if;
  v_version:=0;v_parent:=encode(sha256(convert_to('nexloop:schema-parent:none:'||v_type,'UTF8')),'hex');
 else
  if v_version is null then raise exception 'schema proposal: object type unknown' using errcode='22023';end if;
  v_parent:=encode(sha256(convert_to(runtime.nexloop_canonical_json(v_definition),'UTF8')),'hex');
 end if;
 v_change_digest:=encode(sha256(convert_to(runtime.nexloop_canonical_json(jsonb_build_object('operation',v_op,'type_name',v_type,'change',b->'change')),'UTF8')),'hex');
 insert into ontology.nexloop_schema_proposals(tenant_id,world,proposal_id,candidate_id,operation,type_name,target_ref,change,change_digest,parent_schema_version,
   parent_schema_digest,input_snapshot_digest,candidate_definition_ids,claim_ids,compatibility_report_digest,permission_proof_digest,frozen_by)
  values(v_tenant,'real',(b->>'proposal_id')::uuid,x.candidate_id,v_op,v_type,v_target,b->'change',v_change_digest,v_version,v_parent,x.input_snapshot_digest,
   v_definitions,v_claims,b->>'compatibility_report_digest',b->>'permission_proof_digest','service:evolution') returning * into r;
 insert into ontology.nexloop_schema_proposal_events(tenant_id,world,proposal_id,from_status,to_status,recorded_by) values(v_tenant,'real',r.proposal_id,null,'frozen','service:evolution');
 return jsonb_build_object('frozen',true,'replayed',false,'proposal',runtime.nexloop_schema_proposal_wire(r));
end $$;

create function authz.nexloop_schema_proposal_withdraw(p_digest text,p_proposal uuid,p_reason text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);r ontology.nexloop_schema_proposals;
begin
 if p_reason is null or p_reason not in ('parent_moved','missing_data','candidate_rejected','superseded','user_interrupted') then
  raise exception 'schema proposal withdraw reason invalid' using errcode='22023';end if;
 select * into r from ontology.nexloop_schema_proposals where tenant_id=v_tenant and world='real' and proposal_id=p_proposal for update;
 if not found then raise exception 'schema proposal unknown' using errcode='22023';end if;
 if r.status='withdrawn' and r.withdraw_reason=p_reason then return jsonb_build_object('status','withdrawn','replayed',true);end if;
 if r.status<>'frozen' then raise exception 'schema proposal % cannot be withdrawn',r.status using errcode='42501';end if;
 update ontology.nexloop_schema_proposals set status='withdrawn',withdraw_reason=p_reason,finished_at=clock_timestamp()
  where tenant_id=v_tenant and world='real' and proposal_id=p_proposal;
 insert into ontology.nexloop_schema_proposal_events(tenant_id,world,proposal_id,from_status,to_status,reason,recorded_by)
  values(v_tenant,'real',p_proposal,'frozen','withdrawn',p_reason,'service:evolution');
 return jsonb_build_object('status','withdrawn','replayed',false);
end $$;

create function authz.nexloop_schema_proposal_read(p_digest text,p_proposal uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);r ontology.nexloop_schema_proposals;
begin
 select * into r from ontology.nexloop_schema_proposals where tenant_id=v_tenant and world='real' and proposal_id=p_proposal;
 if not found then return null;end if;
 return jsonb_build_object('proposal',runtime.nexloop_schema_proposal_wire(r),'withdraw_reason',r.withdraw_reason,'finished_at',r.finished_at);
end $$;

-- For F3's publication transaction only (the EIOS review decision with a used NX-039 permit); no role holds it.
create function runtime.nexloop_schema_proposal_mark_published(p_tenant text,p_proposal uuid,p_permit text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r ontology.nexloop_schema_proposals;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from ontology.nexloop_schema_proposals where tenant_id=p_tenant and world='real' and proposal_id=p_proposal for update;
 if not found then raise exception 'schema proposal unknown' using errcode='22023';end if;
 if r.status='published' and r.permit_ref=p_permit then return jsonb_build_object('status','published','replayed',true);end if;
 if r.status<>'frozen' then raise exception 'schema proposal % cannot be published',r.status using errcode='42501';end if;
 update ontology.nexloop_schema_proposals set status='published',permit_ref=p_permit,finished_at=clock_timestamp() where tenant_id=p_tenant and world='real' and proposal_id=p_proposal;
 insert into ontology.nexloop_schema_proposal_events(tenant_id,world,proposal_id,from_status,to_status,recorded_by) values(p_tenant,'real',p_proposal,'frozen','published',p_by);
 return jsonb_build_object('status','published','replayed',false);
end $$;

do $f$
declare f text;
begin
 foreach f in array array['authz.nexloop_schema_proposal_freeze(text,jsonb)','authz.nexloop_schema_proposal_withdraw(text,uuid,text)',
   'authz.nexloop_schema_proposal_read(text,uuid)','runtime.nexloop_schema_proposal_mark_published(text,uuid,text,text)','runtime.nexloop_schema_change_valid(text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
 foreach f in array array['authz.nexloop_schema_proposal_freeze(text,jsonb)','authz.nexloop_schema_proposal_withdraw(text,uuid,text)','authz.nexloop_schema_proposal_read(text,uuid)'] loop
  execute 'grant execute on function '||f||' to nexloop_evo_writer';
 end loop;
end $f$;
