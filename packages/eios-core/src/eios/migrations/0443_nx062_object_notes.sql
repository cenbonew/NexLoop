-- NX-062 M5c (docs/implementation/NX-062-M5-design.md §11, owner 2026-11-10 option A): the object note - one governed Action for people
-- and machine clients, an unverified statement about a Commitment or a CommercialRecord, never a property, a fact or evidence. Read by
-- the workbench path only (never by a runtime role, a machine read verb or an export, W1, W2); machine notes capped per client and per
-- object per day (W3); the audit names the note, its author and its object, never its text; erased with the Consumer and the member,
-- deleted with its object (V-3). The migration number is temporary; it lives only in the file name and the catalog.

create table runtime.nexloop_object_notes (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(runtime.nexloop_world_kind(world) is not null),
 note_id text not null check(note_id~'^[0-9a-f]{64}$'),
 type_name text not null check(type_name in ('Commitment','CommercialRecord')),
 object_id text not null check(object_id~'^[a-f0-9]{64}$'),
 author_kind text not null check(author_kind in ('human','machine')),
 author_principal_id text not null check(length(author_principal_id) between 1 and 200),
 author_subject_id text check(length(author_subject_id) between 1 and 200),
 client_id text,
 text text not null check(char_length(text) between 1 and 2000),
 erased boolean not null default false,
 request_ref text not null check(length(request_ref) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,note_id),
 check((author_kind='machine')=(client_id is not null))
);
create index nexloop_object_notes_object on runtime.nexloop_object_notes(tenant_id,world,type_name,object_id,created_at);
create index nexloop_object_notes_client_day on runtime.nexloop_object_notes(tenant_id,client_id,created_at) where client_id is not null;
alter table runtime.nexloop_object_notes owner to nexloop_owner;
alter table runtime.nexloop_object_notes enable row level security;
alter table runtime.nexloop_object_notes force row level security;
create policy tenant_boundary on runtime.nexloop_object_notes to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_object_notes from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- Append-only, except the erasure rewrite and the deletion with the object (the functions below set the transaction's mark).
create function runtime.nexloop_object_note_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if current_setting('nexloop.object_note_maintenance',true) is distinct from 'on' then
  raise exception 'object notes are append-only' using errcode='42501';end if;
 if tg_op='UPDATE' and (new.tenant_id,new.world,new.note_id,new.type_name,new.object_id,new.author_kind,new.client_id,new.request_ref,new.created_at)
   is distinct from (old.tenant_id,old.world,old.note_id,old.type_name,old.object_id,old.author_kind,old.client_id,old.request_ref,old.created_at) then
  raise exception 'object notes: only the erasure rewrite' using errcode='42501';end if;
 return case when tg_op='DELETE' then old else new end;
end $$;
alter function runtime.nexloop_object_note_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_object_note_guard() from public;
create trigger nx062_note_guard before update or delete on runtime.nexloop_object_notes for each row execute function runtime.nexloop_object_note_guard();
create trigger nx062_note_no_truncate before truncate on runtime.nexloop_object_notes for each statement execute function control.nexloop_nx022_append_only();

-- W3: the machine note caps (trusted configuration; no policy, no machine note).
create table control.nexloop_machine_note_limits (
 revision integer primary key check(revision>=1),
 notes_per_client_per_day integer not null check(notes_per_client_per_day between 1 and 100000),
 notes_per_object_per_day integer not null check(notes_per_object_per_day between 1 and 10000),
 manifest_version integer not null check(manifest_version>=1),
 digest text not null check(digest~'^[0-9a-f]{64}$'),
 recorded_at timestamptz not null default clock_timestamp()
);
alter table control.nexloop_machine_note_limits owner to nexloop_owner;
revoke all on control.nexloop_machine_note_limits from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger nx062_append_only before update or delete on control.nexloop_machine_note_limits for each row execute function control.nexloop_nx022_append_only();

create function control.nexloop_publish_machine_note_limits(p_policy jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare latest control.nexloop_machine_note_limits;v_digest text;v_revision integer;
begin
 if session_user is distinct from 'nexloop_configurator' then raise exception 'machine note limits require trusted configuration' using errcode='42501';end if;
 if jsonb_typeof(p_policy) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(p_policy) k) is distinct from array['manifest_version','notes_per_client_per_day','notes_per_object_per_day','schema_version']
  or p_policy->>'schema_version' is distinct from 'nexloop-machine-note-limits/1'
  or jsonb_typeof(p_policy->'manifest_version') is distinct from 'number' or jsonb_typeof(p_policy->'notes_per_client_per_day') is distinct from 'number'
  or jsonb_typeof(p_policy->'notes_per_object_per_day') is distinct from 'number' then
  raise exception 'machine note limits invalid' using errcode='22023';end if;
 v_digest:=encode(sha256(convert_to(p_policy::text,'UTF8')),'hex');
 select * into latest from control.nexloop_machine_note_limits order by revision desc limit 1;
 if found and latest.digest=v_digest then return jsonb_build_object('revision',latest.revision,'changed',false);end if;
 if found and (p_policy->>'manifest_version')::integer<=latest.manifest_version then raise exception 'machine note limits version must increase' using errcode='22023';end if;
 v_revision:=coalesce(latest.revision,0)+1;
 insert into control.nexloop_machine_note_limits(revision,notes_per_client_per_day,notes_per_object_per_day,manifest_version,digest)
  values(v_revision,(p_policy->>'notes_per_client_per_day')::integer,(p_policy->>'notes_per_object_per_day')::integer,(p_policy->>'manifest_version')::integer,v_digest);
 return jsonb_build_object('revision',v_revision,'changed',true);
end $$;
alter function control.nexloop_publish_machine_note_limits(jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_publish_machine_note_limits(jsonb) from public;
grant execute on function control.nexloop_publish_machine_note_limits(jsonb) to nexloop_configurator;

-- The note text as stored: control characters and bidirectional controls removed, trimmed.
create function runtime.nexloop_object_note_text(p jsonb) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when jsonb_typeof(p)='string' then btrim(regexp_replace(p#>>'{}',
  '[\u0001-\u0008\u000B\u000C\u000E-\u001F\u007F\u200E\u200F\u202A-\u202E\u2066-\u2069]','','g')) end
$$;

alter function runtime.nexloop_object_note_text(jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_object_note_text(jsonb) from public;

-- The handler: a person (owner or operator now) or a machine client (through the M5b machine core, which already checked the key, the
-- allowlist, the grant and the type) adds one note to a visible object.
create function runtime.nexloop_governed_object_note(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_text text;v_kind text;v_client text;v_subject_id text;v_id text;lim control.nexloop_machine_note_limits;n integer;v_role text;
 v_day timestamptz:=date_trunc('day',clock_timestamp() at time zone 'UTC') at time zone 'UTC';
begin
 if jsonb_typeof(body) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['object_id','operation','request_id','text','type_name']
  or body->>'type_name' not in ('Commitment','CommercialRecord') or coalesce(body->>'object_id','')!~'^[a-f0-9]{64}$' then
  raise exception 'object note: invalid' using errcode='22023';end if;
 v_text:=runtime.nexloop_object_note_text(body->'text');
 if v_text is null or char_length(v_text) not between 1 and 2000 then raise exception 'object note: invalid' using errcode='22023';end if;
 if p_subject='machine' then
  select c.client_id,c.subject_id into v_client,v_subject_id from authz.nexloop_machine_clients c where c.tenant_id=p_tenant and c.principal_id=p_principal and c.status='active';
  if v_client is null then raise exception 'object note: not_permitted' using errcode='42501';end if;
  v_kind:='machine';
 elsif p_subject='human' then
  v_role:=coalesce((control.nexloop_workbench_current(p_tenant)).members->>p_principal,'');
  if v_role not in ('owner','operator') then raise exception 'object note: not_permitted' using errcode='42501';end if;
  v_subject_id:=control.nexloop_approval_subject_of(p_tenant,p_principal);
  v_kind:='human';
 else
  raise exception 'object note: not_permitted' using errcode='42501';
 end if;
 -- The same visibility as the machine reads and the workbench: present, typed, nothing of an erasing or erased Consumer.
 if not authz.nexloop_machine_object_visible(p_tenant,p_world,body->>'type_name',body->>'object_id') then
  raise exception 'object note: not_found' using errcode='P0002';end if;
 v_id:=encode(sha256(convert_to(jsonb_build_array('nexloop-object-note-v1',p_tenant,p_world,p_intent)::text,'UTF8')),'hex');
 perform set_config('eios.tenant_id',p_tenant,true);
 if exists(select 1 from runtime.nexloop_object_notes where tenant_id=p_tenant and world=p_world and note_id=v_id) then
  return jsonb_build_object('note_id',v_id,'replayed',true);end if;
 if v_kind='machine' then
  -- W3: per client per UTC day, and per object per UTC day over all machine clients; no policy, no machine note.
  select * into lim from control.nexloop_machine_note_limits order by revision desc limit 1;
  if lim.revision is null then raise exception 'object note: note_limit_policy_missing' using errcode='NX429';end if;
  perform pg_advisory_xact_lock(hashtextextended('nexloop-object-note:'||p_tenant,0));
  select count(*) into n from runtime.nexloop_object_notes where tenant_id=p_tenant and client_id=v_client and created_at>=v_day;
  if n>=lim.notes_per_client_per_day then raise exception 'object note: note_limit' using errcode='NX429';end if;
  select count(*) into n from runtime.nexloop_object_notes where tenant_id=p_tenant and world=p_world and type_name=body->>'type_name'
   and object_id=body->>'object_id' and author_kind='machine' and created_at>=v_day;
  if n>=lim.notes_per_object_per_day then raise exception 'object note: note_limit' using errcode='NX429';end if;
 end if;
 insert into runtime.nexloop_object_notes(tenant_id,world,note_id,type_name,object_id,author_kind,author_principal_id,author_subject_id,client_id,text,request_ref)
  values(p_tenant,p_world,v_id,body->>'type_name',body->>'object_id',v_kind,p_principal,v_subject_id,v_client,v_text,p_intent);
 -- The audit: the note, its author and its object - never the text, its length or a digest of it.
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('object-note:'||p_tenant||':'||v_id,'object.note.add',p_tenant,p_principal,'object_note',p_intent,
   jsonb_build_object('note_id',v_id,'author_kind',v_kind,'author_principal_id',p_principal,'client_id',v_client,
    'type_name',body->>'type_name','object_id',body->>'object_id','world',p_world),clock_timestamp());
 return jsonb_build_object('note_id',v_id,'replayed',false);
end $$;
alter function runtime.nexloop_governed_object_note(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_object_note(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('object.add_note','object_note_add','agent_or_human','runtime.nexloop_governed_object_note(text,text,text,text,text,jsonb)','NX-062');
insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values
 ('object.add_note',1,'active','write','Add an unverified note to a visible Commitment or CommercialRecord (do not write names, phone numbers or other personal data).',
  'migration:0443_nx062_object_notes.sql','NX-062');
insert into control.nexloop_machine_write_capabilities(capability,object_types,param_keys,description,source_task) values
 ('object.add_note',array['Commitment','CommercialRecord'],array['object_id','text','type_name'],
  'One note of 1-2000 characters on a visible object; an unverified statement, never a property or evidence; do not write personal data.','NX-062');

-- The workbench resolves an Action by its capability only for human capabilities (0125). A registered machine write capability is also
-- one people use (agent_or_human): for those - and only those - the same lookup, the same human session check, one published
-- definition, the member's own grant fact. Every other capability goes to the unchanged function.
alter function authz.nexloop_workbench_action_for(text,text,text) rename to nexloop_workbench_action_for_before_notes;
create function authz.nexloop_workbench_action_for(p_digest text,p_world text,p_capability text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;n integer;v_result jsonb;
begin
 if not exists(select 1 from control.nexloop_machine_write_capabilities w join control.nexloop_governed_capabilities g on g.capability=w.capability
   where w.capability=p_capability and g.subject_rule='agent_or_human') then
  return authz.nexloop_workbench_action_for_before_notes(p_digest,p_world,p_capability);
 end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if ident->'binding'->>'subject_kind' is distinct from 'human' then raise exception 'workbench requires a human session' using errcode='42501';end if;
 select count(*),min(jsonb_build_object('stable_name',d.definition->>'stable_name','version',(d.definition->>'version')::integer)::text)::jsonb into n,v_result
  from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
   and d.definition->'capability_binding'->>'capability_name'=p_capability;
 if n=0 then return null;end if;
 if n>1 then raise exception 'workbench capability published more than once' using errcode='22023';end if;
 return v_result||jsonb_build_object('granted',exists(select 1 from authz.nexloop_authority_facts f where f.tenant_id=v_tenant and f.fact_kind='grants'
  and f.entity_key=array[ident->'binding'->>'subject_principal_id','eios:action:'||(v_result->>'stable_name')||':'||(v_result->>'version')]
  and jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0));
end $$;
alter function authz.nexloop_workbench_action_for(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_action_for(text,text,text) from public;
revoke all on function authz.nexloop_workbench_action_for_before_notes(text,text,text) from public,nexloop_api;
grant execute on function authz.nexloop_workbench_action_for(text,text,text) to nexloop_api;

-- The workbench read: the notes of one object, for a member who may read it now (owner, operator, reviewer), plain values only.
create function control.nexloop_workbench_object_notes(p_digest text,p_world text,p_type text,p_object text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;
begin
 ident:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 if coalesce((control.nexloop_workbench_current(v_tenant)).members->>v_principal,'') not in ('owner','operator','reviewer') then
  raise exception 'object note: not_permitted' using errcode='42501';end if;
 if p_type not in ('Commitment','CommercialRecord') or coalesce(p_object,'')!~'^[a-f0-9]{64}$'
  or not authz.nexloop_machine_object_visible(v_tenant,p_world,p_type,p_object) then
  raise exception 'object note: not_found' using errcode='P0002';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return coalesce((select jsonb_agg(jsonb_build_object('note_id',n.note_id,'author_kind',n.author_kind,'client_id',n.client_id,
   'author_principal_id',case when n.author_kind='human' then n.author_principal_id end,'text',n.text,'erased',n.erased,'created_at',to_jsonb(n.created_at))
   order by n.created_at desc,n.note_id) from (select * from runtime.nexloop_object_notes where tenant_id=v_tenant and world=p_world and type_name=p_type
   and object_id=p_object order by created_at desc,note_id limit 200) n),'[]'::jsonb);
end $$;
alter function control.nexloop_workbench_object_notes(text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_workbench_object_notes(text,text,text,text) from public;
grant execute on function control.nexloop_workbench_object_notes(text,text,text,text) to nexloop_api;

-- V-3: erasure and deletion ------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_object_note_consumer_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if new.state='pending_confirmation' or (tg_op='UPDATE' and old.state is distinct from 'pending_confirmation') then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 perform set_config('nexloop.object_note_maintenance','on',true);
 -- Notes on an object of the Consumer, and notes whose text names the Consumer's id: text replaced (W2: a name or a number in a note
 -- on an object not linked to the Consumer cannot be recognised; that limit is documented).
 update runtime.nexloop_object_notes n set text='[已擦除]',erased=true
  where n.tenant_id=new.tenant_id and n.world=new.world and not n.erased
   and (new.consumer_id=any(runtime.nexloop_object_consumers_read(n.tenant_id,n.world,n.type_name,n.object_id)) or strpos(n.text,new.consumer_id)>0);
 perform set_config('nexloop.object_note_maintenance','',true);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function runtime.nexloop_object_note_consumer_erasure() owner to nexloop_owner;
revoke all on function runtime.nexloop_object_note_consumer_erasure() from public;
create trigger object_note_consumer_erasure after insert or update of state on control.nexloop_consumer_erasures
 for each row execute function runtime.nexloop_object_note_consumer_erasure();

create function runtime.nexloop_object_note_member_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p text;v text;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 perform set_config('nexloop.object_note_maintenance','on',true);
 foreach p in array new.principals loop
  v:='pseudonym:'||encode(sha256(convert_to('object-note:'||p,'UTF8')),'hex');
  update runtime.nexloop_object_notes set author_principal_id=v,author_subject_id=v,text='[已擦除]',erased=true
   where tenant_id=new.tenant_id and author_kind='human' and author_principal_id=p;
 end loop;
 perform set_config('nexloop.object_note_maintenance','',true);
 return null;
end $$;
alter function runtime.nexloop_object_note_member_erasure() owner to nexloop_owner;
revoke all on function runtime.nexloop_object_note_member_erasure() from public;
create trigger object_note_member_erasure after insert on control.nexloop_workbench_member_erasures
 for each row execute function runtime.nexloop_object_note_member_erasure();

-- Deleted with the object, whatever deletes it (retention expiry, erasure purge); a legal hold keeps the object and so its notes.
create function runtime.nexloop_object_note_object_deleted() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if old.type_name not in ('Commitment','CommercialRecord') then return null;end if;
 perform set_config('eios.tenant_id',old.tenant_id,true);
 perform set_config('nexloop.object_note_maintenance','on',true);
 delete from runtime.nexloop_object_notes where tenant_id=old.tenant_id and world=old.world and type_name=old.type_name and object_id=old.object_id;
 perform set_config('nexloop.object_note_maintenance','',true);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_object_note_object_deleted() owner to nexloop_owner;
revoke all on function runtime.nexloop_object_note_object_deleted() from public;
create trigger object_note_object_deleted after delete on ontology.objects for each row execute function runtime.nexloop_object_note_object_deleted();
