-- NX-021 / NX-020 closing: change feeds written in the same transaction as the business write,
-- drained by leased service workers.
--  * recall-instance: every ontology.objects insert/update/delete (governed create/edit, Schema
--    re-binding, erasure) marks the instance for (re)indexing or removal from the recall index.
--  * claim-match: every recorded Claim marks its Conversation for matching (extraction run end).
-- One row per (tenant, world, feed, item); a newer change bumps change_seq, so a worker that
-- finishes an older snapshot never drops a later change. Leases are fenced by change_seq;
-- failures back off and dead-letter after max_attempts. No model call or index text here.
create table runtime.nexloop_work_feed (
 tenant_id text not null,world text not null,feed text not null check(feed in ('recall-instance','claim-match')),
 item_key text not null check(char_length(item_key) between 1 and 600),payload jsonb not null check(jsonb_typeof(payload)='object'),
 change_seq bigint not null default 1 check(change_seq>0),created_at timestamptz not null default clock_timestamp(),
 changed_at timestamptz not null default clock_timestamp(),available_at timestamptz not null default clock_timestamp(),
 lease_until timestamptz,lease_seq bigint,attempts integer not null default 0 check(attempts>=0),
 status text not null default 'pending' check(status in ('pending','dead_lettered')),last_code text check(last_code is null or last_code~'^[a-z0-9_]{1,64}$'),
 primary key(tenant_id,world,feed,item_key),check((lease_until is null)=(lease_seq is null))
);
create index nexloop_work_feed_due on runtime.nexloop_work_feed(tenant_id,world,feed,available_at) where status='pending';
alter table runtime.nexloop_work_feed owner to nexloop_owner;
alter table runtime.nexloop_work_feed enable row level security;
alter table runtime.nexloop_work_feed force row level security;
create policy work_feed_tenant on runtime.nexloop_work_feed to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_work_feed from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_work_feed_touch(p_tenant text,p_world text,p_feed text,p_key text,p_payload jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 -- The row belongs to the written row's tenant; the caller's tenant context is restored after.
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload) values(p_tenant,p_world,p_feed,p_key,p_payload)
 on conflict(tenant_id,world,feed,item_key) do update set payload=excluded.payload,change_seq=f.change_seq+1,changed_at=clock_timestamp(),
  available_at=clock_timestamp(),attempts=0,status='pending',last_code=null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;

create function authz.nexloop_work_feed_on_object() returns trigger
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare o record;
begin
 if tg_op='DELETE' then o:=old;else o:=new;end if;
 if tg_op='UPDATE' and (old.tenant_id,old.world,old.type_name,old.object_id) is distinct from (new.tenant_id,new.world,new.type_name,new.object_id) then
  perform authz.nexloop_work_feed_touch(old.tenant_id,old.world,'recall-instance',old.type_name||'/'||old.object_id,
   jsonb_build_object('type_name',old.type_name,'object_id',old.object_id,'op','delete'));
 end if;
 perform authz.nexloop_work_feed_touch(o.tenant_id,o.world,'recall-instance',o.type_name||'/'||o.object_id,
  jsonb_build_object('type_name',o.type_name,'object_id',o.object_id,'op',case when tg_op='DELETE' then 'delete' else 'upsert' end));
 return null;
end $$;
create trigger nexloop_work_feed_object after insert or update or delete on ontology.objects
 for each row execute function authz.nexloop_work_feed_on_object();

create function authz.nexloop_work_feed_on_claim() returns trigger
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 perform authz.nexloop_work_feed_touch(new.tenant_id,new.world,'claim-match',new.conversation_id,jsonb_build_object('conversation_id',new.conversation_id));
 return null;
end $$;
create trigger nexloop_work_feed_claim after insert on ontology.nexloop_claims
 for each row execute function authz.nexloop_work_feed_on_claim();

-- Existing rows become pending work (bootstrap runs before any RLS context).
insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload)
 select tenant_id,world,'recall-instance',type_name||'/'||object_id,jsonb_build_object('type_name',type_name,'object_id',object_id,'op','upsert')
 from ontology.objects on conflict do nothing;
insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload)
 select distinct tenant_id,world,'claim-match',conversation_id,jsonb_build_object('conversation_id',conversation_id)
 from ontology.nexloop_claims where resolution_state='unresolved' on conflict do nothing;

-- Signed service port; the feed's own technical Action authority (eios:action:NexLoop.feed.<feed>:1).
create function authz.nexloop_work_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_feed text:=c->>'feed';v_verb text:=c->>'verb';
 v_result jsonb;v_limit integer;v_lease integer;v_delay integer;v_max integer;r runtime.nexloop_work_feed%rowtype;ident jsonb;
begin
 if session_user<>'nexloop_domain_worker' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or v_feed is null or v_feed not in ('recall-instance','claim-match') or v_verb is null or v_verb not in ('claim','complete','retry','backlog')
  or a->>'protocol' is distinct from 'nexloop-work-feed-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:NexLoop.feed.'||v_feed||':1' or a->>'resource_id' is distinct from a->>'action_resource'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-work-feed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb then raise exception 'work feed unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='claim' then
  v_limit:=(c->>'limit')::integer;v_lease:=(c->>'lease_seconds')::integer;
  if v_limit is null or v_limit not between 1 and 50 or v_lease is null or v_lease not between 1 and 600 then raise exception 'work feed input invalid' using errcode='22023';end if;
  with due as (
   select f.item_key from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'
    and f.available_at<=clock_timestamp() and (f.lease_until is null or f.lease_until<=clock_timestamp())
   order by f.available_at,f.changed_at,f.item_key limit v_limit for update skip locked),
  leased as (
   update runtime.nexloop_work_feed f set lease_until=clock_timestamp()+make_interval(secs=>v_lease),lease_seq=f.change_seq,attempts=f.attempts+1
   from due where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.item_key=due.item_key
   returning f.item_key,f.payload,f.change_seq,f.attempts,f.available_at)
  select coalesce(jsonb_agg(jsonb_build_object('item_key',l.item_key,'payload',l.payload,'fence',l.change_seq,'attempts',l.attempts)
   order by l.available_at,l.item_key),'[]'::jsonb) into v_result from leased l;
 elsif v_verb in ('complete','retry') then
  select * into r from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=c->>'item_key' for update;
  -- A lost or replaced lease never completes or retries someone else's work.
  if not found or r.lease_seq is distinct from (c->>'fence')::bigint or r.lease_until<=clock_timestamp() then
   v_result:=jsonb_build_object('status','lease_lost');
  elsif v_verb='complete' then
   if r.change_seq=r.lease_seq then
    delete from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','completed');
   else
    -- A newer change arrived while working: keep it pending (it is processed again from scratch).
    update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,attempts=0
     where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','changed');
   end if;
  else
   v_delay:=(c->>'delay_seconds')::integer;v_max:=(c->>'max_attempts')::integer;
   if v_delay is null or v_delay not between 0 and 3600 or v_max is null or v_max not between 1 and 20 or coalesce(c->>'code','')!~'^[a-z0-9_]{1,64}$' then
    raise exception 'work feed input invalid' using errcode='22023';end if;
   update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,last_code=c->>'code',
    status=case when r.change_seq=r.lease_seq and r.attempts>=v_max then 'dead_lettered' else 'pending' end,
    available_at=case when r.change_seq=r.lease_seq then clock_timestamp()+make_interval(secs=>v_delay) else clock_timestamp() end,
    attempts=case when r.change_seq=r.lease_seq then r.attempts else 0 end
   where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key
   returning jsonb_build_object('status',status) into v_result;
  end if;
 else
  select jsonb_build_object(
   'pending',(select count(*) from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'oldest_pending_seconds',(select floor(extract(epoch from clock_timestamp()-min(f.changed_at)))::bigint from runtime.nexloop_work_feed f
     where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'dead_lettered',(select coalesce(jsonb_agg(jsonb_build_object('item_key',x.item_key,'attempts',x.attempts,'code',x.last_code) order by x.changed_at desc),'[]'::jsonb)
     from (select * from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='dead_lettered'
      order by f.changed_at desc limit 20) x)) into v_result;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_work_feed_touch(text,text,text,text,jsonb)','authz.nexloop_work_feed_on_object()',
  'authz.nexloop_work_feed_on_claim()','authz.nexloop_work_feed(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_work_feed(text,text,text,text,text) to nexloop_domain_worker;
