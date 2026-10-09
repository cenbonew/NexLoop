-- NX-019 background extraction: correction links, a per-Message extraction feed written in
-- the same transaction as the governed Message (persist before ACK, no model call on the
-- interactive path), and a service-only feed/backlog command. Jobs use the existing
-- durable queue (runtime.jobs, retry_wait, dead_lettered); no second task registry.
alter table ontology.nexloop_claims add column corrects_claim_id text,
 add constraint nexloop_claims_corrects check(corrects_claim_id is null or (corrects_claim_id~'^[0-9a-f]{64}$' and epistemic_kind='correction' and corrects_claim_id<>claim_id));

-- Previous recorder keeps every evidence check; it is no longer directly callable.
alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) rename to nexloop_record_claim_extraction_v0066;
revoke all on function authz.nexloop_record_claim_extraction_v0066(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_record_claim_extraction(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_payload::jsonb;cl jsonb;v_seen jsonb:='{}'::jsonb;v_result jsonb;v_target jsonb;
begin
 if jsonb_typeof(c->'claims') is distinct from 'array' then raise exception 'claim extraction unavailable' using errcode='42501';end if;
 -- A correction may only point at an explicit Claim recorded earlier in the same run,
 -- from the same or an earlier Message.
 for cl in select value from jsonb_array_elements(c->'claims') loop
  if jsonb_typeof(cl->'corrects_claim_id')='string' then
   v_target:=v_seen->(cl->>'corrects_claim_id');
   if cl->>'epistemic_kind' is distinct from 'correction' or v_target is null or v_target->>'kind'='hypothesis'
    or (v_target->>'sequence')::bigint>(cl->>'source_sequence')::bigint or cl->>'source_sequence' is null then
    raise exception 'claim correction target invalid' using errcode='42501';end if;
  elsif cl ? 'corrects_claim_id' and jsonb_typeof(cl->'corrects_claim_id') is distinct from 'null' then
   raise exception 'claim correction target invalid' using errcode='42501';end if;
  v_seen:=v_seen||jsonb_build_object(cl->>'claim_id',jsonb_build_object('kind',cl->>'epistemic_kind','sequence',cl->'source_sequence'));
 end loop;
 v_result:=authz.nexloop_record_claim_extraction_v0066(p_digest,p_world,p_text,p_signature,p_payload);
 if v_result->'replay'='true'::jsonb then return v_result;end if;
 -- Same transaction; RLS tenant context was set by the verified authority above.
 for cl in select value from jsonb_array_elements(c->'claims') where jsonb_typeof(value->'corrects_claim_id')='string' loop
  update ontology.nexloop_claims set corrects_claim_id=cl->>'corrects_claim_id'
   where tenant_id=current_setting('eios.tenant_id',true) and world=p_world and claim_id=cl->>'claim_id' and corrects_claim_id is null;
 end loop;
 return v_result;
end $$;
alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_record_claim_extraction(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_record_claim_extraction(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

create table runtime.nexloop_claim_extraction_feed (
 tenant_id text not null,world text not null,conversation_id text not null,message_id text not null,sequence bigint not null check(sequence>0),
 created_at timestamptz not null default clock_timestamp(),task_id text,enqueued_at timestamptz,
 primary key(tenant_id,world,message_id),check((task_id is null)=(enqueued_at is null)),
 foreign key(tenant_id,world,conversation_id) references runtime.nexloop_conversations
);
create index nexloop_claim_feed_pending on runtime.nexloop_claim_extraction_feed(tenant_id,world,conversation_id,sequence) where task_id is null;
alter table runtime.nexloop_claim_extraction_feed owner to nexloop_owner;
alter table runtime.nexloop_claim_extraction_feed enable row level security;
alter table runtime.nexloop_claim_extraction_feed force row level security;
create policy claim_feed_tenant on runtime.nexloop_claim_extraction_feed to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_claim_extraction_feed from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
-- Existing persisted Messages become pending extraction work (bootstrap runs before any RLS context).
insert into runtime.nexloop_claim_extraction_feed(tenant_id,world,conversation_id,message_id,sequence)
 select tenant_id,world,conversation_id,message_id,sequence from runtime.nexloop_conversation_messages on conflict do nothing;

create function authz.nexloop_claim_feed_on_message() returns trigger
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 insert into runtime.nexloop_claim_extraction_feed(tenant_id,world,conversation_id,message_id,sequence)
  values(new.tenant_id,new.world,new.conversation_id,new.message_id,new.sequence) on conflict do nothing;
 return new;
end $$;
alter function authz.nexloop_claim_feed_on_message() owner to nexloop_owner;
revoke all on function authz.nexloop_claim_feed_on_message() from public;
create trigger nexloop_claim_feed after insert on runtime.nexloop_conversation_messages
 for each row execute function authz.nexloop_claim_feed_on_message();

create function authz.nexloop_claim_extraction_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_verb text:=c->>'verb';
 v_result jsonb;v_quiet integer;v_limit integer;v_window integer;v_count integer;j runtime.jobs%rowtype;
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
  if v_quiet not between 0 and 3600 or v_limit not between 1 and 100 or v_window not between 1 and 200 then
   raise exception 'claim feed unavailable' using errcode='42501';end if;
  -- Debounce a burst: a conversation is due once its newest pending Message is quiet.
  select coalesce(jsonb_agg(jsonb_build_object('conversation_id',d.conversation_id,'through_sequence',d.through,
    'message_ids',(select jsonb_agg(w.message_id order by w.sequence) from (select m.message_id,m.sequence from runtime.nexloop_conversation_messages m
       where m.tenant_id=v_tenant and m.world=p_world and m.conversation_id=d.conversation_id and m.sequence<=d.through
       order by m.sequence desc limit v_window) w)) order by d.oldest,d.conversation_id),'[]'::jsonb) into v_result
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
alter function authz.nexloop_claim_extraction_feed(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_claim_extraction_feed(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_claim_extraction_feed(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
