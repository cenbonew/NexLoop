-- I34 (a) (L6, S6 rerun dc7645a9 / R61): an undefined concept used to fall back to searching the whole statement; the fuzzy
-- recall always found something, so the access read coverage 1.0 with no miss_reason and the evolution counted the concept as
-- covered. Temporary number (0384 and 0385 belong to batch 71); the dispatcher renumbers. Depends on 0234 (the table), 0377
-- (miss_reason) and 0378 (gloss_hits, the body copied here). Published migrations are not edited.
--
-- runtime.nexloop_semantic_accesses.extraction: 'terms' (definition terms were extracted from the statement and searched) or
-- 'fallback' (none was found; the whole statement was the single search). Null on older rows and on an access whose caller passed
-- its query as the term itself (resolve, browse without extraction). A fallback access has coverage 0: coverage counts the
-- definition terms extracted, and a fallback extracted none. Its fuzzy results still reach the caller, marked as fallback hits;
-- the row keeps them in result_refs. Checked in the table and again in the record function. authz.nexloop_semantic_access_read
-- (0234 body) returns the column as extraction (null on rows without it).

alter table runtime.nexloop_semantic_accesses add column extraction text check(extraction in ('terms','fallback')),
 add constraint nexloop_semantic_accesses_fallback_coverage check(extraction is distinct from 'fallback' or coverage=0);

create or replace function authz.nexloop_semantic_access_record(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;v_tenant text;v_run uuid;v_id uuid;v_access runtime.nexloop_semantic_accesses;
 r jsonb;v_refs jsonb:='[]'::jsonb;v_terms text[];
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>65536 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-semantic-access-v1' or c->>'kind' not in ('access','use') or c->>'kind' is null
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'semantic access record unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-semantic-access-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'semantic access record unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if v_tenant is null or a->>'tenant_id' is distinct from v_tenant or a->>'world' is distinct from p_world then
  raise exception 'semantic access record unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if coalesce(c->>'access_id','')!~'^[0-9a-f-]{36}$' or (c ? 'run_id' and jsonb_typeof(c->'run_id')<>'null' and coalesce(c->>'run_id','')!~'^[0-9a-f-]{36}$') then
  raise exception 'semantic access record invalid' using errcode='22023';end if;
 v_id:=(c->>'access_id')::uuid;v_run:=nullif(c->>'run_id','')::uuid;
 if v_run is not null and not exists(select 1 from authz.nexloop_runtime_run_bindings b where b.tenant_id=v_tenant and b.world=p_world and b.run_id=v_run)
  and not exists(select 1 from authz.nexloop_run_credentials rc where rc.run_id=v_run and rc.world=p_world and rc.source_digest=p_digest and rc.status='active') then
  raise exception 'semantic access run unknown' using errcode='42501';end if;
 if jsonb_typeof(c->'refs') is distinct from 'array' or jsonb_array_length(c->'refs')>320 then raise exception 'semantic access record invalid' using errcode='22023';end if;
 for r in select value from jsonb_array_elements(c->'refs') loop
  if jsonb_typeof(r)<>'object' or (select array_agg(key order by key) from jsonb_object_keys(r) key) is distinct from array['ref','revision']
   or coalesce(r->>'ref','')!~'^(eios:object_type:|eios:property:|nexloop:vocabulary:)[^[:cntrl:]]+$' or char_length(r->>'ref')>512 or coalesce(r->>'revision','')!~'^[A-Za-z0-9._:@-]{1,128}$' then
   raise exception 'semantic access record invalid' using errcode='22023';end if;
  v_refs:=v_refs||jsonb_build_array(r);
 end loop;
 if c->>'kind'='use' then
  if coalesce(c->>'accessed_id','')!~'^[0-9a-f-]{36}$' then raise exception 'semantic access record invalid' using errcode='22023';end if;
  select * into v_access from runtime.nexloop_semantic_accesses where tenant_id=v_tenant and world=p_world and access_id=(c->>'accessed_id')::uuid and kind='access';
  -- A use names an access of the same Run, and only refs that access returned.
  if not found or v_access.run_id is distinct from v_run
   or exists(select 1 from jsonb_array_elements(v_refs) u where not v_access.result_refs @> jsonb_build_array(u.value)) then
   raise exception 'semantic access use invalid' using errcode='42501';end if;
  insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,context_id,accessed_id,result_refs,result_count)
   values(v_tenant,p_world,v_id,'use',v_run,v_access.context_id,v_access.access_id,v_refs,jsonb_array_length(v_refs)) on conflict do nothing;
 else
  if jsonb_typeof(c->'term_hashes') is distinct from 'array' then raise exception 'semantic access record invalid' using errcode='22023';end if;
  v_terms:=array(select value#>>'{}' from jsonb_array_elements(c->'term_hashes'));
  -- I34 (0386): extraction terms | fallback (the whole statement searched); a fallback access covers no definition term.
  if c ? 'extraction' and (c->>'extraction' is null or c->>'extraction' not in ('terms','fallback')
   or (c->>'extraction'='fallback' and (c->>'coverage')::numeric is distinct from 0)) then
   raise exception 'semantic access record invalid' using errcode='22023';end if;
  insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,context_id,operation,query_kind,term_hashes,coverage,ambiguity,
    result_refs,result_count,unresolved_count,miss_reason,gloss_hits,extraction)
   values(v_tenant,p_world,v_id,'access',v_run,nullif(c->>'context_id',''),c->>'operation',c->>'query_kind',v_terms,(c->>'coverage')::numeric,
    (c->>'ambiguity')::boolean,v_refs,jsonb_array_length(v_refs),coalesce((c->>'unresolved_count')::integer,0),
    case when jsonb_array_length(v_refs)=0 then nullif(c->>'miss_reason','') end,
    case when jsonb_typeof(c->'gloss_hits')='object' then c->'gloss_hits' else '{}'::jsonb end,c->>'extraction') on conflict do nothing;
 end if;
 return jsonb_build_object('access_id',v_id,'kind',c->>'kind');
end $$;

create or replace function authz.nexloop_semantic_access_read(p_digest text,p_world text,p_since timestamptz,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user is distinct from 'nexloop_evo_reader' or p_world is distinct from 'real' or p_limit not between 1 and 1000 or p_since is null then
  raise exception 'semantic access read unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 if v_tenant is null then raise exception 'semantic access read unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return coalesce((select jsonb_agg(jsonb_build_object('access_id',s.access_id,'kind',s.kind,'run_id',s.run_id,'context_id',s.context_id,'accessed_id',s.accessed_id,
   'operation',s.operation,'query_kind',s.query_kind,'term_hashes',to_jsonb(s.term_hashes),'coverage',s.coverage,'ambiguity',s.ambiguity,
   'result_refs',s.result_refs,'result_count',s.result_count,'unresolved_count',s.unresolved_count,'recorded_at',s.recorded_at,'extraction',s.extraction) order by s.recorded_at,s.access_id)
  from (select * from runtime.nexloop_semantic_accesses where tenant_id=v_tenant and world='real' and recorded_at>=p_since
   order by recorded_at,access_id limit p_limit) s),'[]'::jsonb);
end $$;
