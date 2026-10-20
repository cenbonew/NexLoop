-- NX-038 E2 trajectory read (docs/implementation/NX-038-design.md §11.2 database route, §12.1, §12.5; owner decisions D1, D10-D12,
-- D14, D20). A read-only login role nexloop_evo_reader (D1) executes one function, authz.nexloop_evo_trace_runs, and nothing else:
--   * identity: the session role must be nexloop_evo_reader AND the caller presents an active service credential (no Run
--     context); the tenant comes from that credential, never from a parameter; world real only (§2.3);
--   * input: one window of issued Runs [since, until) of at most 31 days, at most 500 Runs a page (D20: a diagnosis reads at most
--     2000), ordered by issue time and Run id, cursor '<issued_at>|<run id>';
--   * output per Run: its kind (message / fallback / role / other), the Consumer id it served, that Consumer's persona (ADR-034;
--     D12: filtered by the caller's persona list, the persona always labelled), and the per-Run evidence the NX-037 evidence
--     function already returns (codes, counts, ids, times; never a body, a Claim value or a prompt);
--   * exclusions, counted not hidden: Runs of a Consumer being or already erased (NX-029, §2.4), and Runs outside the persona list.
-- Nothing is written. Functions pin search_path; the owner function runs under the tenant's row security.

create role nexloop_evo_reader login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema authz to nexloop_evo_reader;

create function runtime.nexloop_evo_trace_runs(p_tenant text,p_world text,p_since timestamptz,p_until timestamptz,p_after_at timestamptz,p_after_run uuid,
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
      'evidence',runtime.nexloop_nx037_run_evidence(p_tenant,p_world,p.run_id)) order by p.issued_at,p.run_id) from page p),'[]'::jsonb),
   'total',(select count(*) from kept),
   'excluded',jsonb_build_object('erasure',(select count(*) from labelled where erased),
     'persona',(select count(*) from labelled where not erased and persona is not null and not persona=any(p_personas))),
   'next_after',(select case when (select count(*) from page)=p_limit then q.c end from (select p.issued_at::text||'|'||p.run_id::text c from page p
     order by p.issued_at desc,p.run_id desc limit 1) q)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_evo_trace_runs(text,text,timestamptz,timestamptz,timestamptz,uuid,integer,text[]) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_trace_runs(text,text,timestamptz,timestamptz,timestamptz,uuid,integer,text[]) from public;

create function authz.nexloop_evo_trace_runs(p_digest text,p_since timestamptz,p_until timestamptz,p_after text,p_limit integer,p_personas text[]) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_after_at timestamptz;v_after_run uuid;
begin
 if session_user is distinct from 'nexloop_evo_reader' then raise exception 'trace read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,'real');  -- raises for an unknown, revoked or expired credential
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest) then
  raise exception 'trace read unavailable' using errcode='42501';end if;
 if p_since is null or p_until is null or p_until<=p_since or p_until-p_since>interval '31 days' or p_limit is null or p_limit not between 1 and 500
  or p_personas is null or cardinality(p_personas) not between 1 and 3 or not (p_personas<@array['real','simulated_agent','synthetic'])
  or (p_after is not null and p_after!~'^[0-9][0-9T:. +-]{9,39}\|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') then
  raise exception 'trace read invalid' using errcode='22023';end if;
 if p_after is not null then v_after_at:=split_part(p_after,'|',1)::timestamptz;v_after_run:=split_part(p_after,'|',2)::uuid;end if;
 return runtime.nexloop_evo_trace_runs(ident->'binding'->>'tenant_id','real',p_since,p_until,v_after_at,v_after_run,p_limit,p_personas);
end $$;
alter function authz.nexloop_evo_trace_runs(text,timestamptz,timestamptz,text,integer,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_trace_runs(text,timestamptz,timestamptz,text,integer,text[]) from public;
grant execute on function authz.nexloop_evo_trace_runs(text,timestamptz,timestamptz,text,integer,text[]) to nexloop_evo_reader;
