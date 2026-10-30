-- I26 (S6 fdd261cf, L6): semantic reads of a whole Chinese statement never met the English identifiers, and seven published types
-- were missing from the definition index at their current version. Temporary number; the dispatcher renumbers. Published
-- migrations are not edited.
--
-- 1. control.nexloop_recall_terms (nexloop_api, nexloop_domain_worker): the definition names, display names, aliases and vocabulary
--    values that occur in a text, within the caller's readable gates and the live scope of control.nexloop_recall_search. An ASCII
--    body matches as whole words (snake_case and CamelCase split into words); a body with CJK characters matches as a substring with
--    white space ignored. Only definition bodies are returned (at most 16, longest first), never the text.
-- 2. control.nexloop_recall_stale_definitions (nexloop_domain_worker): published types whose latest version has no definition entry
--    at that version, so the recall-index service reflows them on its tick (schema publications and tenant configuration publish
--    types without touching the index).
-- 3. runtime.nexloop_semantic_accesses.miss_reason: why an access returned nothing - no_readable_types (no definition type is
--    readable by the caller), denied (recall found definitions the caller may not read), no_match. Null when something was returned.
--    authz.nexloop_semantic_access_record (0313) is redefined to store it; everything else in the function is unchanged.

create function ontology.nexloop_recall_term_words(p text) returns text
 language sql immutable strict parallel safe set search_path=pg_catalog,pg_temp as $$
 select ' '||btrim(regexp_replace(lower(regexp_replace(regexp_replace(p,'([a-z0-9])([A-Z])','\1 \2','g'),'([A-Z]+)([A-Z][a-z])','\1 \2','g')),
  '[^a-z0-9]+',' ','g'))||' '
$$;
alter function ontology.nexloop_recall_term_words(text) owner to nexloop_owner;
revoke all on function ontology.nexloop_recall_term_words(text) from public;

create function control.nexloop_recall_terms(p_digest text,p_world text,p_request jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);q jsonb:=p_request;txt text;words text;flat text;types text[];props text[];res jsonb;
begin
 txt:=q->>'text';
 if txt is null or char_length(txt) not between 1 and 4000 or jsonb_typeof(q->'readable_types')<>'array' or jsonb_typeof(q->'readable_properties')<>'array' then
  raise exception 'recall terms invalid' using errcode='22023';end if;
 types:=array(select jsonb_array_elements_text(q->'readable_types'));props:=array(select jsonb_array_elements_text(q->'readable_properties'));
 words:=ontology.nexloop_recall_term_words(txt);flat:=regexp_replace(lower(txt),'\s+','','g');
 select coalesce(jsonb_agg(b order by char_length(b) desc,b),'[]'::jsonb) into res from (
  select distinct btrim(e.body) b from ontology.nexloop_recall_entries e
   join ontology.nexloop_recall_scope(t,p_world,'definition',types,props,null) s(entry_id) using(entry_id)
  where e.field in ('name','display_name','alias','vocabulary_value') and char_length(btrim(e.body)) between 2 and 64
   and case when e.body ~ '[㐀-䶿一-鿿豈-﫿]' then position(regexp_replace(lower(e.body),'\s+','','g') in flat)>0
            else ontology.nexloop_recall_term_words(e.body)<>'  ' and position(ontology.nexloop_recall_term_words(e.body) in words)>0 end
  order by 1 limit 200) x;
 return coalesce((select jsonb_agg(v) from (select v from jsonb_array_elements(res) v limit 16) y),'[]'::jsonb);
end $$;
alter function control.nexloop_recall_terms(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_recall_terms(text,text,jsonb) from public;
grant execute on function control.nexloop_recall_terms(text,text,jsonb) to nexloop_api,nexloop_domain_worker;

create function control.nexloop_recall_stale_definitions(p_digest text,p_world text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);
begin
 if session_user is distinct from 'nexloop_domain_worker' then raise exception 'recall reflow unavailable' using errcode='42501';end if;
 return coalesce((select jsonb_agg(l.type_name order by l.type_name) from (
   select v.type_name,max(v.version) latest from ontology.object_type_versions v where v.tenant_id=t group by v.type_name) l
  where not exists(select 1 from ontology.nexloop_recall_entries e where e.tenant_id=t and e.index_kind='definition'
   and e.source_key='type:'||l.type_name and e.source_revision=l.latest and e.tokenizer_version='nexloop-cjk-bigram-v1')),'[]'::jsonb);
end $$;
alter function control.nexloop_recall_stale_definitions(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_recall_stale_definitions(text,text) from public;
grant execute on function control.nexloop_recall_stale_definitions(text,text) to nexloop_domain_worker;

alter table runtime.nexloop_semantic_accesses add column miss_reason text
 check(miss_reason in ('no_readable_types','denied','no_match')),
 add constraint nexloop_semantic_accesses_miss_reason check(miss_reason is null or (kind='access' and result_count=0));

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
  insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,context_id,operation,query_kind,term_hashes,coverage,ambiguity,
    result_refs,result_count,unresolved_count,miss_reason)
   values(v_tenant,p_world,v_id,'access',v_run,nullif(c->>'context_id',''),c->>'operation',c->>'query_kind',v_terms,(c->>'coverage')::numeric,
    (c->>'ambiguity')::boolean,v_refs,jsonb_array_length(v_refs),coalesce((c->>'unresolved_count')::integer,0),
    case when jsonb_array_length(v_refs)=0 then nullif(c->>'miss_reason','') end) on conflict do nothing;
 end if;
 return jsonb_build_object('access_id',v_id,'kind',c->>'kind');
end $$;

