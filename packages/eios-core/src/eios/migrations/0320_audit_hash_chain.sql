-- NX-059 audit tamper evidence (docs/design/audit-hash-chain.md; owner Q6 2026-10-22, option A: tamper evidence and export verification,
-- no external security platform, the sealing key in private configuration, published audit tables unchanged). The migration number is
-- temporary; it lives only in the file name and the catalog. Semantics after the upstream 0059_authorization_audit.sql (NEX-EIOS 1e363db,
-- sha256 741b3f76bd58ea9a71579602de4c7ea0ed3976719b6704c4403605f3c7d58501): chain head, per-record digest, predecessor link, verify
-- function, append-only triggers; no code copied, no SIEM outbox, no pseudonymization, no retention table (0129/0132/0140 own those).
--
-- runtime.audit_events and runtime.nexloop_effect_recovery_audits keep their columns, grants and policies; each gains an AFTER INSERT
-- trigger (the row's content digest into a pending queue, no lock) and an AFTER DELETE trigger (a row leaves only inside a governed
-- retention or erasure transaction, and its chain entry is marked pruned). runtime.nexloop_audit_chain_append moves committed pending
-- rows into one SHA-256 chain per tenant; the HMAC seal of a chain head is computed outside the database (scripts/ops/audit_chain.py,
-- key from private configuration) and stored after SQL checked its link.
set local timezone = 'UTC';

create table runtime.nexloop_audit_chain_pending (
 pending_id bigint generated always as identity primary key,
 tenant_id text not null check(tenant_id<>''),
 source text not null check(source in ('audit_events','effect_recovery_audits')),
 event_key text not null check(char_length(event_key) between 1 and 200),
 record_digest text not null check(record_digest~'^[0-9a-f]{64}$'),
 captured_at timestamptz not null default clock_timestamp(),
 pruned_at timestamptz, pruned_ref text,
 unique(source,event_key), check((pruned_at is null)=(pruned_ref is null))
);
create table runtime.nexloop_audit_chain (
 tenant_id text not null, seq bigint not null check(seq>=1),
 source text not null check(source in ('audit_events','effect_recovery_audits')),
 event_key text not null, record_digest text not null check(record_digest~'^[0-9a-f]{64}$'),
 prev_link text not null check(prev_link~'^[0-9a-f]{64}$'), link text not null check(link~'^[0-9a-f]{64}$'),
 captured_at timestamptz not null, appended_at timestamptz not null,
 pruned_at timestamptz, pruned_ref text,
 primary key(tenant_id,seq), unique(source,event_key), check((pruned_at is null)=(pruned_ref is null))
);
create table runtime.nexloop_audit_chain_heads (
 tenant_id text primary key, seq bigint not null check(seq>=1), link text not null check(link~'^[0-9a-f]{64}$'), updated_at timestamptz not null
);
create table runtime.nexloop_audit_chain_seals (
 tenant_id text not null, seq bigint not null check(seq>=1), link text not null check(link~'^[0-9a-f]{64}$'),
 key_id text not null check(key_id~'^[0-9a-f]{16}$'), sealed_at timestamptz not null, mac text not null check(mac~'^[0-9a-f]{64}$'),
 recorded_at timestamptz not null default clock_timestamp(), primary key(tenant_id,seq,key_id)
);
do $$ declare t text; begin
 foreach t in array array['runtime.nexloop_audit_chain_pending','runtime.nexloop_audit_chain','runtime.nexloop_audit_chain_heads','runtime.nexloop_audit_chain_seals'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('revoke all on %s from public',t);
  -- No application role has any grant. The owner's own functions see one tenant (eios.tenant_id, as everywhere), except the append,
  -- which walks every tenant's queue under its transaction-local flag.
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true) or current_setting(''nexloop.audit_chain_append'',true)=''on'') with check(tenant_id=current_setting(''eios.tenant_id'',true) or current_setting(''nexloop.audit_chain_append'',true)=''on'')',t);
 end loop;
end $$;

create function runtime.nexloop_audit_chain_genesis(p_tenant text) returns text language sql immutable
 set search_path=pg_catalog,pg_temp as $$ select encode(sha256(convert_to('nexloop-audit-chain/1|genesis|'||p_tenant,'UTF8')),'hex') $$;
create function runtime.nexloop_audit_chain_link(p_tenant text,p_seq bigint,p_source text,p_key text,p_digest text,p_prev text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to('nexloop-audit-chain/1|'||p_tenant||'|'||p_seq||'|'||p_source||'|'||p_key||'|'||p_digest||'|'||p_prev,'UTF8')),'hex') $$;
-- The content digest of a source row: its whole jsonb text (timestamps in UTC; the functions below fix the time zone).
create function runtime.nexloop_audit_chain_digest(p_row jsonb) returns text language sql immutable
 set search_path=pg_catalog,pg_temp as $$ select encode(sha256(convert_to(p_row::text,'UTF8')),'hex') $$;

-- AFTER INSERT on a source table: TG_ARGV[0] the source, TG_ARGV[1] the key column. No lock, no head read.
create function runtime.nexloop_audit_chain_capture() returns trigger language plpgsql security definer
 set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
declare r jsonb:=to_jsonb(new);prior text:=current_setting('eios.tenant_id',true);
begin
 -- The queue row is written under the audit row's own tenant, whatever the writer's session carries (a superuser, a batch over
 -- several tenants, no tenant at all): the audit write never fails because of its chain. The caller's setting is restored.
 perform set_config('eios.tenant_id',r->>'tenant_id',true);
 insert into runtime.nexloop_audit_chain_pending(tenant_id,source,event_key,record_digest)
  values(r->>'tenant_id',tg_argv[0],r->>tg_argv[1],runtime.nexloop_audit_chain_digest(r));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
-- AFTER DELETE on a source table: only a governed retention or erasure transaction (control.nexloop_erasure_scope, 0129) removes an
-- audit row; its chain entry (or pending row) keeps the digest and is marked pruned with that request.
create function runtime.nexloop_audit_chain_prune() returns trigger language plpgsql security definer
 set search_path=pg_catalog,pg_temp as $$
declare ref text;k text:=to_jsonb(old)->>tg_argv[1];prior text:=current_setting('eios.tenant_id',true);
begin
 select request_ref into ref from control.nexloop_erasure_scope where txid=txid_current() order by table_name limit 1;
 if not found then raise exception 'audit rows leave only through governed retention or erasure' using errcode='42501';end if;
 perform set_config('eios.tenant_id',to_jsonb(old)->>'tenant_id',true);  -- the entry is marked under the row's own tenant
 perform set_config('nexloop.audit_chain_prune','on',true);
 update runtime.nexloop_audit_chain set pruned_at=clock_timestamp(),pruned_ref=ref where source=tg_argv[0] and event_key=k and pruned_at is null;
 if not found then
  update runtime.nexloop_audit_chain_pending set pruned_at=clock_timestamp(),pruned_ref=ref where source=tg_argv[0] and event_key=k and pruned_at is null;
 end if;
 perform set_config('nexloop.audit_chain_prune','',true);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
-- The chain tables are append only: the one UPDATE is marking an entry pruned (inside the prune trigger); a pending row leaves only
-- when the append moves it into the chain.
create function runtime.nexloop_audit_chain_guard() returns trigger language plpgsql
 set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and current_setting('nexloop.audit_chain_prune',true)='on' and old.pruned_at is null and new.pruned_at is not null
  and (to_jsonb(new)-'pruned_at'-'pruned_ref')=(to_jsonb(old)-'pruned_at'-'pruned_ref') then return new;end if;
 if tg_op='DELETE' and tg_table_name='nexloop_audit_chain_pending' and current_setting('nexloop.audit_chain_append',true)='on' then return old;end if;
 raise exception 'the audit chain is append only' using errcode='42501';
end $$;
do $$ declare t text; begin
 foreach t in array array['nexloop_audit_chain_pending','nexloop_audit_chain','nexloop_audit_chain_heads','nexloop_audit_chain_seals'] loop
  execute format('create trigger nexloop_audit_chain_guard before update or delete on runtime.%I for each row execute function runtime.nexloop_audit_chain_guard()',t);
  execute format('create trigger nexloop_audit_chain_no_truncate before truncate on runtime.%I for each statement execute function runtime.nexloop_audit_chain_guard()',t);
 end loop;
end $$;
-- The head moves only inside the append (its own guard exception below).
create or replace function runtime.nexloop_audit_chain_head_guard() returns trigger language plpgsql
 set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and current_setting('nexloop.audit_chain_append',true)='on' and new.seq>old.seq then return new;end if;
 raise exception 'the audit chain is append only' using errcode='42501';
end $$;
drop trigger nexloop_audit_chain_guard on runtime.nexloop_audit_chain_heads;
create trigger nexloop_audit_chain_guard before update or delete on runtime.nexloop_audit_chain_heads for each row execute function runtime.nexloop_audit_chain_head_guard();

create trigger nexloop_audit_chain_capture after insert on runtime.audit_events
 for each row execute function runtime.nexloop_audit_chain_capture('audit_events','event_id');
create trigger nexloop_audit_chain_prune after delete on runtime.audit_events
 for each row execute function runtime.nexloop_audit_chain_prune('audit_events','event_id');
create trigger nexloop_audit_chain_capture after insert on runtime.nexloop_effect_recovery_audits
 for each row execute function runtime.nexloop_audit_chain_capture('effect_recovery_audits','recovery_id');
create trigger nexloop_audit_chain_prune after delete on runtime.nexloop_effect_recovery_audits
 for each row execute function runtime.nexloop_audit_chain_prune('effect_recovery_audits','recovery_id');

-- Existing rows enter the queue once, oldest first (the first append chains them).
insert into runtime.nexloop_audit_chain_pending(tenant_id,source,event_key,record_digest,captured_at)
 select tenant_id,'audit_events',event_id,runtime.nexloop_audit_chain_digest(to_jsonb(a)),clock_timestamp() from runtime.audit_events a order by created_at,event_id;
insert into runtime.nexloop_audit_chain_pending(tenant_id,source,event_key,record_digest,captured_at)
 select tenant_id,'effect_recovery_audits',recovery_id::text,runtime.nexloop_audit_chain_digest(to_jsonb(r)),clock_timestamp()
 from runtime.nexloop_effect_recovery_audits r order by recorded_at,recovery_id;

-- Committed pending rows into each tenant's chain, in pending order (at most p_limit per tenant per call).
create function runtime.nexloop_audit_chain_append(p_limit integer default 1000) returns jsonb language plpgsql security definer
 set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
declare t text;h runtime.nexloop_audit_chain_heads%rowtype;p runtime.nexloop_audit_chain_pending%rowtype;seq bigint;prev text;nxt text;n integer;out jsonb:='{}';
begin
 if p_limit is null or p_limit not between 1 and 100000 then raise exception 'audit chain: invalid limit' using errcode='22023';end if;
 perform set_config('nexloop.audit_chain_append','on',true);
 for t in select distinct tenant_id from runtime.nexloop_audit_chain_pending order by 1 loop
  perform pg_advisory_xact_lock(hashtextextended('nexloop-audit-chain:'||t,0));
  select * into h from runtime.nexloop_audit_chain_heads where tenant_id=t for update;
  if found then seq:=h.seq;prev:=h.link;else seq:=0;prev:=runtime.nexloop_audit_chain_genesis(t);end if;
  n:=0;
  for p in select * from runtime.nexloop_audit_chain_pending where tenant_id=t order by pending_id limit p_limit for update loop
   seq:=seq+1;nxt:=runtime.nexloop_audit_chain_link(t,seq,p.source,p.event_key,p.record_digest,prev);
   insert into runtime.nexloop_audit_chain values(t,seq,p.source,p.event_key,p.record_digest,prev,nxt,p.captured_at,clock_timestamp(),p.pruned_at,p.pruned_ref);
   delete from runtime.nexloop_audit_chain_pending where pending_id=p.pending_id;
   prev:=nxt;n:=n+1;
  end loop;
  if n>0 then
   insert into runtime.nexloop_audit_chain_heads values(t,seq,prev,clock_timestamp())
    on conflict(tenant_id) do update set seq=excluded.seq,link=excluded.link,updated_at=excluded.updated_at;
   out:=out||jsonb_build_object(t,n);
  end if;
 end loop;
 perform set_config('nexloop.audit_chain_append','',true);
 return jsonb_build_object('appended',out);
end $$;

-- A seal computed outside (HMAC with a key the database never sees) is stored only for the link the chain holds at that position.
create function runtime.nexloop_audit_chain_seal(p_tenant text,p_seq bigint,p_link text,p_key_id text,p_sealed_at timestamptz,p_mac text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from runtime.nexloop_audit_chain where tenant_id=p_tenant and seq=p_seq and link=p_link) then
  raise exception 'audit chain: the seal names no link of the chain' using errcode='22023';end if;
 insert into runtime.nexloop_audit_chain_seals(tenant_id,seq,link,key_id,sealed_at,mac) values(p_tenant,p_seq,p_link,p_key_id,p_sealed_at,p_mac)
  on conflict do nothing;
 return jsonb_build_object('tenant_id',p_tenant,'seq',p_seq,'link',p_link);
end $$;

-- The source row of a chain entry as jsonb (or null when it is gone), read under the tenant's own boundary.
create function runtime.nexloop_audit_chain_source(p_tenant text,p_source text,p_key text) returns jsonb language plpgsql security definer
 set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
declare r jsonb;prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_source='audit_events' then select to_jsonb(a) into r from runtime.audit_events a where a.tenant_id=p_tenant and a.event_id=p_key;
 else select to_jsonb(x) into r from runtime.nexloop_effect_recovery_audits x where x.tenant_id=p_tenant and x.recovery_id::text=p_key;end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);  -- the caller's tenant setting is restored
 return r;
end $$;

-- Verification without the key: links, sequence, head, source digests, deletions and insertions, seal positions. Codes and keys only.
create function runtime.nexloop_audit_chain_verify(p_tenant text) returns jsonb language plpgsql security definer
 set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
declare c runtime.nexloop_audit_chain%rowtype;h runtime.nexloop_audit_chain_heads%rowtype;s runtime.nexloop_audit_chain_seals%rowtype;
 prev text:=runtime.nexloop_audit_chain_genesis(p_tenant);expect bigint:=1;r jsonb;problems jsonb:='[]';n bigint:=0;pruned bigint:=0;k text;src text;
 prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for c in select * from runtime.nexloop_audit_chain where tenant_id=p_tenant order by seq loop
  n:=n+1;
  if c.seq<>expect then problems:=problems||jsonb_build_object('code','sequence_gap','seq',c.seq,'expected',expect);end if;
  if c.prev_link<>prev or c.link<>runtime.nexloop_audit_chain_link(p_tenant,c.seq,c.source,c.event_key,c.record_digest,c.prev_link) then
   problems:=problems||jsonb_build_object('code','link_broken','seq',c.seq);end if;
  r:=runtime.nexloop_audit_chain_source(p_tenant,c.source,c.event_key);
  if r is null and c.pruned_at is null then problems:=problems||jsonb_build_object('code','record_deleted','seq',c.seq,'source',c.source,'event_key',c.event_key);
  elsif r is null then pruned:=pruned+1;
  elsif runtime.nexloop_audit_chain_digest(r)<>c.record_digest then
   problems:=problems||jsonb_build_object('code','record_altered','seq',c.seq,'source',c.source,'event_key',c.event_key);end if;
  prev:=c.link;expect:=c.seq+1;
 end loop;
 select * into h from runtime.nexloop_audit_chain_heads where tenant_id=p_tenant;
 if (found and (h.seq<>n or h.link<>prev)) or (not found and n>0) then problems:=problems||jsonb_build_object('code','head_mismatch');end if;
 -- Every source row is chained or queued (an inserted row bypassing the capture is neither).
 for src,k in select 'audit_events',a.event_id from runtime.audit_events a where a.tenant_id=p_tenant
   union all select 'effect_recovery_audits',x.recovery_id::text from runtime.nexloop_effect_recovery_audits x where x.tenant_id=p_tenant loop
  if not exists(select 1 from runtime.nexloop_audit_chain where source=src and event_key=k)
   and not exists(select 1 from runtime.nexloop_audit_chain_pending where source=src and event_key=k) then
   problems:=problems||jsonb_build_object('code','record_unchained','source',src,'event_key',k);end if;
 end loop;
 for s in select * from runtime.nexloop_audit_chain_seals where tenant_id=p_tenant order by seq loop
  if not exists(select 1 from runtime.nexloop_audit_chain where tenant_id=p_tenant and seq=s.seq and link=s.link) then
   problems:=problems||jsonb_build_object('code','seal_mismatch','seq',s.seq,'key_id',s.key_id);end if;
 end loop;
 r:=jsonb_build_object('tenant_id',p_tenant,'valid',jsonb_array_length(problems)=0,'entries',n,'pruned',pruned,
  'pending',(select count(*) from runtime.nexloop_audit_chain_pending where tenant_id=p_tenant),'head_seq',coalesce(h.seq,0),'problems',problems);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);  -- the caller's tenant setting is restored
 return r;
end $$;

-- Export: the chain entries after p_after (at most p_limit), each with its source row's text when the row still exists.
create function runtime.nexloop_audit_chain_export(p_tenant text,p_after bigint,p_limit integer) returns setof jsonb language plpgsql security definer
 set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if p_limit is null or p_limit not between 1 and 10000 or p_after is null or p_after<0 then raise exception 'audit chain: invalid range' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return query select jsonb_build_object('seq',c.seq,'source',c.source,'event_key',c.event_key,'record_digest',c.record_digest,'prev_link',c.prev_link,
   'link',c.link,'pruned_at',c.pruned_at,'pruned_ref',c.pruned_ref,'record_text',runtime.nexloop_audit_chain_source(p_tenant,c.source,c.event_key)::text)
  from runtime.nexloop_audit_chain c where c.tenant_id=p_tenant and c.seq>p_after order by c.seq limit p_limit;
end $$;

do $$ declare f text; begin
 foreach f in array array['runtime.nexloop_audit_chain_genesis(text)','runtime.nexloop_audit_chain_link(text,bigint,text,text,text,text)',
   'runtime.nexloop_audit_chain_digest(jsonb)','runtime.nexloop_audit_chain_capture()','runtime.nexloop_audit_chain_prune()',
   'runtime.nexloop_audit_chain_guard()','runtime.nexloop_audit_chain_head_guard()','runtime.nexloop_audit_chain_append(integer)',
   'runtime.nexloop_audit_chain_seal(text,bigint,text,text,timestamptz,text)','runtime.nexloop_audit_chain_source(text,text,text)',
   'runtime.nexloop_audit_chain_verify(text)','runtime.nexloop_audit_chain_export(text,bigint,integer)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
