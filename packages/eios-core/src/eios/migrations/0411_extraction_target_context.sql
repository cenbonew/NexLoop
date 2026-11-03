-- S-DUP (docs/implementation/S-DUP-extraction-dedupe-design.md §2): extraction windows are split into targets and context. The migration
-- number lives only in the file name and the catalog; nothing below repeats it.
-- authz.nexloop_claim_extraction_feed (0069 body, changes marked "S-DUP", registered in tests/support/s_dup_edits.py): verb due returns
-- message_ids = the window's pending Messages only (the targets, at most `window`) and context_message_ids = at most `context` (default 10,
-- 0..50) earlier Messages of the Conversation, which the extractor shows as context and never extracts again. mark and backlog unchanged.
-- An older worker reads only message_ids and ignores context_message_ids (it extracts the targets without context); an older task without
-- context_message_ids is all targets to the new worker.

create or replace function authz.nexloop_claim_extraction_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_verb text:=c->>'verb';
 v_result jsonb;v_quiet integer;v_limit integer;v_window integer;v_count integer;j runtime.jobs%rowtype;v_context integer;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-claim-feed-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:nexloop.claim.extract:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') or v_verb not in ('due','mark','backlog') or v_verb is null then
  raise exception 'claim feed unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-claim-feed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'claim feed unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'claim feed unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if v_verb='due' then
  v_quiet:=(c->>'quiet_seconds')::integer;v_limit:=(c->>'limit')::integer;v_window:=(c->>'window')::integer;
  v_context:=coalesce((c->>'context')::integer,10);
  if v_quiet not between 0 and 3600 or v_limit not between 1 and 100 or v_window not between 1 and 200 or v_context not between 0 and 50 then
   raise exception 'claim feed unavailable' using errcode='42501';end if;
  -- Debounce a burst: a conversation is due once its newest pending Message is quiet.
  select coalesce(jsonb_agg(jsonb_build_object('conversation_id',d.conversation_id,'through_sequence',d.through,
    -- S-DUP: the targets are the window's pending Messages only; earlier Messages come as context, never extracted again.
    'message_ids',(select jsonb_agg(w.message_id order by w.sequence) from (select f2.message_id,f2.sequence from runtime.nexloop_claim_extraction_feed f2
       where f2.tenant_id=v_tenant and f2.world=p_world and f2.conversation_id=d.conversation_id and f2.task_id is null and f2.sequence<=d.through
       order by f2.sequence desc limit v_window) w),
    'context_message_ids',coalesce((select jsonb_agg(x.message_id order by x.sequence) from (select m.message_id,m.sequence from runtime.nexloop_conversation_messages m
       where m.tenant_id=v_tenant and m.world=p_world and m.conversation_id=d.conversation_id
         and m.sequence<(select min(w2.sequence) from (select f3.sequence from runtime.nexloop_claim_extraction_feed f3
           where f3.tenant_id=v_tenant and f3.world=p_world and f3.conversation_id=d.conversation_id and f3.task_id is null and f3.sequence<=d.through
           order by f3.sequence desc limit v_window) w2)
       order by m.sequence desc limit v_context) x),'[]'::jsonb)) order by d.oldest,d.conversation_id),'[]'::jsonb) into v_result
  from (select f.conversation_id,max(f.sequence) through,min(f.created_at) oldest from runtime.nexloop_claim_extraction_feed f
   where f.tenant_id=v_tenant and f.world=p_world and f.task_id is null group by f.conversation_id
   having max(f.created_at)<=clock_timestamp()-make_interval(secs=>v_quiet) order by min(f.created_at),f.conversation_id limit v_limit) d;
 elsif v_verb='mark' then
  select * into j from runtime.jobs where tenant_id=v_tenant and world=p_world and queue='claim-extraction' and job_id=c->>'task_id';
  if not found or j.normalized_input->>'conversation_id' is distinct from c->>'conversation_id'
   or j.normalized_input->'through_sequence' is distinct from c->'through_sequence' then
   raise exception 'claim feed task mismatch' using errcode='42501';end if;
  update runtime.nexloop_claim_extraction_feed set task_id=j.job_id,enqueued_at=clock_timestamp()
   where tenant_id=v_tenant and world=p_world and conversation_id=c->>'conversation_id' and task_id is null and sequence<=(c->>'through_sequence')::bigint;
  get diagnostics v_count=row_count;
  v_result:=jsonb_build_object('marked',v_count,'task_id',j.job_id);
 else
  select jsonb_build_object(
   'feed_pending',(select count(*) from runtime.nexloop_claim_extraction_feed f where f.tenant_id=v_tenant and f.world=p_world and f.task_id is null),
   'oldest_pending_seconds',(select floor(extract(epoch from clock_timestamp()-min(f.created_at)))::bigint from runtime.nexloop_claim_extraction_feed f
     where f.tenant_id=v_tenant and f.world=p_world and f.task_id is null),
   'jobs',(select coalesce(jsonb_object_agg(s.status,s.n),'{}'::jsonb) from (select status,count(*) n from runtime.jobs
     where tenant_id=v_tenant and world=p_world and queue='claim-extraction' group by status) s),
   'dead_lettered',(select coalesce(jsonb_agg(jsonb_build_object('task_id',x.job_id,'attempts',x.attempts,'code',x.result->>'code',
       'conversation_id',x.normalized_input->>'conversation_id') order by x.updated_at desc),'[]'::jsonb)
     from (select * from runtime.jobs where tenant_id=v_tenant and world=p_world and queue='claim-extraction' and status='dead_lettered' order by updated_at desc limit 20) x)
  ) into v_result;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;

