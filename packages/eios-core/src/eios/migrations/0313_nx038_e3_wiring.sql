-- NX-038 E3 wiring (dispatcher 2026-10-22, G-S6-1; option A): the Context assembly of a message Run reads semantics through
-- SemanticAdapter with a SemanticAccessRecorder (nexloop_eios.context_artifacts). The migration number lives only in the file name and
-- the catalog. Functions re-created from their latest bodies with only these changes (tests/test_function_copy_guard.py):
-- 1. authz.nexloop_semantic_access_record (0234): the Context is assembled before the Run is activated, so the Run is not yet in
--    authz.nexloop_runtime_run_bindings. A Run is also known when its active Run credential was issued by this very Source (the
--    calling service credential) in this world - still bound to the server-side tenant and world, never a payload value.
-- 2. Failure basis (owner 2026-10-22, option A): a Run whose semantic read found nothing and whose reply was not completed counts as
--    failed (the existing attribution._failed), no separate miss signal. "Reply not completed" for a message Run = its inbound message
--    later got the fallback reply Run (authz.nexloop_message_fallback_issuances, 0110: issued only when the pending reply is still
--    unsettled at its fallback time - the model failed, timed out or gave no reply; at most one per message).
--    runtime.nexloop_evo_trace_runs (0233) adds reply_fallback to each Run; nexloop_eios.evolution_traces.sample_from_run maps it to
--    the sample's existing outcome field (reply_fallback), so the trajectory-sample contract is unchanged.

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
    result_refs,result_count,unresolved_count)
   values(v_tenant,p_world,v_id,'access',v_run,nullif(c->>'context_id',''),c->>'operation',c->>'query_kind',v_terms,(c->>'coverage')::numeric,
    (c->>'ambiguity')::boolean,v_refs,jsonb_array_length(v_refs),coalesce((c->>'unresolved_count')::integer,0)) on conflict do nothing;
 end if;
 return jsonb_build_object('access_id',v_id,'kind',c->>'kind');
end $$;

create or replace function runtime.nexloop_evo_trace_runs(p_tenant text,p_world text,p_since timestamptz,p_until timestamptz,p_after_at timestamptz,p_after_run uuid,
  p_limit integer,p_personas text[]) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with runs as (
   select r.run_id,r.issued_at,
    case when mr.run_id is not null then 'message' when fb.run_id is not null then 'fallback' when rb.run_id is not null then 'role' else 'other' end kind,
    coalesce(mc.consumer_id,fc.consumer_id,rb.consumer_id) consumer_id
   from authz.nexloop_run_credentials r
   join authz.nexloop_service_credentials sc on sc.token_digest=r.source_digest and sc.tenant_id=p_tenant
   left join runtime.nexloop_message_routes mr on mr.tenant_id=p_tenant and mr.world=r.world and mr.run_id=r.run_id
   left join lateral (select cv.consumer_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
     on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
     where m.tenant_id=p_tenant and m.world=r.world and m.message_id=mr.message_id limit 1) mc on true
   left join runtime.nexloop_fallback_context_bindings fb on fb.tenant_id=p_tenant and fb.world=r.world and fb.run_id=r.run_id
   left join lateral (select cv.consumer_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
     on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
     where m.tenant_id=p_tenant and m.world=r.world and m.message_id=fb.message_id limit 1) fc on true
   left join authz.nexloop_role_run_bindings rb on rb.tenant_id=p_tenant and rb.world=r.world and rb.run_id=r.run_id
   where r.world=p_world and r.issued_at>=p_since and r.issued_at<p_until),
  labelled as (
   select x.*,case when x.consumer_id is null then null else runtime.nexloop_consumer_persona(p_tenant,p_world,x.consumer_id) end persona,
    x.consumer_id is not null and runtime.nexloop_consumer_erasing(p_tenant,p_world,x.consumer_id) erased
   from runs x),
  kept as (
   select l.* from labelled l where not l.erased and (l.persona is null or l.persona=any(p_personas))),
  page as (
   select k.* from kept k where p_after_at is null or (k.issued_at,k.run_id)>(p_after_at,p_after_run)
   order by k.issued_at,k.run_id limit p_limit)
 select jsonb_build_object('world',p_world,'since',p_since,'until',p_until,
   'runs',coalesce((select jsonb_agg(jsonb_build_object('run_id',p.run_id,'issued_at',p.issued_at,'kind',p.kind,'consumer_id',p.consumer_id,'persona',p.persona,
      'evidence',runtime.nexloop_nx037_run_evidence(p_tenant,p_world,p.run_id),
      'reply_fallback',p.kind='message' and exists(select 1 from authz.nexloop_message_fallback_issuances fi join runtime.nexloop_message_routes m2
        on m2.tenant_id=fi.tenant_id and m2.world=fi.world and m2.message_id=fi.message_id where m2.tenant_id=p_tenant and m2.world=p_world and m2.run_id=p.run_id)) order by p.issued_at,p.run_id) from page p),'[]'::jsonb),
   'total',(select count(*) from kept),
   'excluded',jsonb_build_object('erasure',(select count(*) from labelled where erased),
     'persona',(select count(*) from labelled where not erased and persona is not null and not persona=any(p_personas))),
   'next_after',(select case when (select count(*) from page)=p_limit then q.c end from (select p.issued_at::text||'|'||p.run_id::text c from page p
     order by p.issued_at desc,p.run_id desc limit 1) q)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
