-- Proactive outreach, slice 3: the purpose list (docs/implementation/outreach-impl-plan.md; design §2, owner decisions P2/P5/P8;
-- contract packages/contracts/outreach-purposes.schema.json). The migration number lives only in the file name and the catalog
-- (renumbered at integration); nothing below repeats it. Still no outreach Action and no real dispatch.
-- 1. control.nexloop_outreach_purpose_lists: versioned, append-only trusted configuration without a tenant dimension (the same kind
--    as control.nexloop_contact_refusal_rules); v1 = deploy/configuration/outreach-purposes.v1.json, byte-equal canonical JSON
--    (tests). A new purpose, a plan-defined one included, is a new list version.
-- 2. control.nexloop_outreach_purpose_states (per tenant and purpose: enabled, revision) and append-only events; no row means the
--    purpose's default_enabled.
-- 3. runtime.nexloop_outreach_purpose(tenant, purpose_id): {purpose_id, listed, enabled, list_version, definition}.
-- 4. Governed owner Action outreach_purposes.set / set_outreach_purpose (purpose_id, enabled, reason): only listed purposes
--    ('outreach purposes: unknown_purpose' otherwise); the owner never adds one.
-- 5. The dry run's purpose gate (outreach_gates body copied from its latest definition, change marked "Outreach slice 3"): refuse
--    an unlisted (purpose_not_allowed) or disabled (purpose_disabled) purpose; a purpose needing approval stays not wired (slice 6).
-- 6. Workbench observe verb outreach_purposes (owner only): the current list with each purpose's state for the tenant.

create table control.nexloop_outreach_purpose_lists (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(definition->>'schema'='nexloop-outreach-purposes/1' and jsonb_typeof(definition->'purposes')='array')
);
alter table control.nexloop_outreach_purpose_lists owner to nexloop_owner;
create trigger outreach_purpose_lists_append_only before update or delete on control.nexloop_outreach_purpose_lists for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_outreach_purpose_lists from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_outreach_purpose_lists(version,definition,definition_digest,published_by)
 values(1,'{"schema":"nexloop-outreach-purposes/1","version":1,"decision":"Outreach design §2 and owner decisions P2/P5/P8 (2026-10-19), outreach slice 3: the purposes proactive outreach may use. Trusted configuration: a new purpose (including a plan-defined one) is a new version of this file; versions are only extended, never edited. The owner enables or disables a listed purpose for the tenant (nexloop.outreach_purposes.set:1); default_enabled applies until then. No purpose needs approval by default (P5; approval is slice 6). Marketing purposes follow the tenant consent mode unless requires_consent (opt-in for that purpose); they carry the server-appended unsubscribe notice and count against the marketing allowance.","purposes":[{"purpose_id":"commitment_followup","kind":"commitment_followup","trigger":"commitment_due","topic":"commitment_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"service_followup","kind":"service_followup","trigger":"service_receipt","topic":"service_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"renewal_reminder","kind":"plan_defined","trigger":"plan_reassess","topic":"renewal_reminder","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"marketing_general","kind":"marketing","trigger":"owner_request","topic":"marketing","frequency_class":"marketing","requires_consent":false,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":true}}]}'::jsonb,encode(sha256(convert_to(runtime.nexloop_canonical_json('{"schema":"nexloop-outreach-purposes/1","version":1,"decision":"Outreach design §2 and owner decisions P2/P5/P8 (2026-10-19), outreach slice 3: the purposes proactive outreach may use. Trusted configuration: a new purpose (including a plan-defined one) is a new version of this file; versions are only extended, never edited. The owner enables or disables a listed purpose for the tenant (nexloop.outreach_purposes.set:1); default_enabled applies until then. No purpose needs approval by default (P5; approval is slice 6). Marketing purposes follow the tenant consent mode unless requires_consent (opt-in for that purpose); they carry the server-appended unsubscribe notice and count against the marketing allowance.","purposes":[{"purpose_id":"commitment_followup","kind":"commitment_followup","trigger":"commitment_due","topic":"commitment_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"service_followup","kind":"service_followup","trigger":"service_receipt","topic":"service_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"renewal_reminder","kind":"plan_defined","trigger":"plan_reassess","topic":"renewal_reminder","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"marketing_general","kind":"marketing","trigger":"owner_request","topic":"marketing","frequency_class":"marketing","requires_consent":false,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":true}}]}'::jsonb),'UTF8')),'hex'),'migration:outreach-purposes.v1');

create table control.nexloop_outreach_purpose_states (
 tenant_id text not null,purpose_id text not null check(purpose_id~'^[a-z][a-z0-9_.]{0,63}$'),enabled boolean not null,revision bigint not null check(revision>0),
 updated_by text not null,request_intent text not null,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,purpose_id),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_outreach_purpose_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,purpose_id text not null,enabled boolean not null,revision bigint not null,
 list_version integer not null,set_by text not null,request_intent text not null,reason text not null check(char_length(reason) between 1 and 500),
 recorded_at timestamptz not null default clock_timestamp(),unique(tenant_id,purpose_id,revision),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_outreach_purpose_states','control.nexloop_outreach_purpose_events'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';execute 'revoke all on '||t||' from public';
  execute format('create policy %I on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',
   'tenant_'||replace(t,'control.',''),t);
 end loop;
end $tables$;
create function control.nexloop_outreach_purpose_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'outreach purpose events are append-only' using errcode='42501';end $$;
alter function control.nexloop_outreach_purpose_events_append_only() owner to nexloop_owner;
create trigger outreach_purpose_events_append_only before update or delete on control.nexloop_outreach_purpose_events
 for each row execute function control.nexloop_outreach_purpose_events_append_only();

create function runtime.nexloop_outreach_purpose(p_tenant text,p_purpose text) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare l control.nexloop_outreach_purpose_lists;d jsonb;s control.nexloop_outreach_purpose_states;
begin
 select * into l from control.nexloop_outreach_purpose_lists order by version desc limit 1;
 select value into d from jsonb_array_elements(coalesce(l.definition->'purposes','[]'::jsonb)) where value->>'purpose_id'=p_purpose;
 if d is null then return jsonb_build_object('purpose_id',p_purpose,'listed',false,'enabled',false,'list_version',l.version,'definition',null);end if;
 select * into s from control.nexloop_outreach_purpose_states where tenant_id=p_tenant and purpose_id=p_purpose;
 return jsonb_build_object('purpose_id',p_purpose,'listed',true,'enabled',coalesce(s.enabled,(d->>'default_enabled')::boolean),'list_version',l.version,'definition',d);
end $$;

create function control.nexloop_governed_set_outreach_purpose(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_id text:=body->>'purpose_id';v_purpose jsonb;cur control.nexloop_outreach_purpose_states;v_revision bigint;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','purpose_id','enabled','reason'))
  or coalesce(v_id,'')!~'^[a-z][a-z0-9_.]{0,63}$' or jsonb_typeof(body->'enabled') is distinct from 'boolean'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'outreach purposes: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,v_id);
 if not (v_purpose->>'listed')::boolean then raise exception 'outreach purposes: unknown_purpose' using errcode='22023';end if;
 select * into cur from control.nexloop_outreach_purpose_states where tenant_id=p_tenant and purpose_id=v_id for update;
 if (v_purpose->>'enabled')::boolean=(body->>'enabled')::boolean then
  return jsonb_build_object('purpose_id',v_id,'enabled',(body->>'enabled')::boolean,'revision',coalesce(cur.revision,0),'changed',false);end if;
 v_revision:=coalesce(cur.revision,0)+1;
 insert into control.nexloop_outreach_purpose_states(tenant_id,purpose_id,enabled,revision,updated_by,request_intent)
  values(p_tenant,v_id,(body->>'enabled')::boolean,v_revision,p_principal,p_intent)
  on conflict(tenant_id,purpose_id) do update set enabled=excluded.enabled,revision=excluded.revision,updated_by=excluded.updated_by,
   request_intent=excluded.request_intent,updated_at=clock_timestamp();
 insert into control.nexloop_outreach_purpose_events(tenant_id,purpose_id,enabled,revision,list_version,set_by,request_intent,reason)
  values(p_tenant,v_id,(body->>'enabled')::boolean,v_revision,(v_purpose->>'list_version')::integer,p_principal,p_intent,body->>'reason');
 return jsonb_build_object('purpose_id',v_id,'enabled',(body->>'enabled')::boolean,'revision',v_revision,'changed',true);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_outreach_purpose(text,text)','control.nexloop_governed_set_outreach_purpose(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('outreach_purposes.set','set_outreach_purpose','human','control.nexloop_governed_set_outreach_purpose(text,text,text,text,text,jsonb)','NX-022');

-- 5. The dry run's purpose gate (outreach_gates, latest body copied; the change is marked "Outreach slice 3") -------------------
create or replace function runtime.nexloop_outreach_gates(p_tenant text,p_world text,p_consumer text,p_purpose text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d jsonb:=runtime.nexloop_outreach_settings(p_tenant);g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;v_purpose jsonb;
begin
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 -- Outreach slice 3: the purpose must be in the current purpose list and enabled for the tenant; approval is slice 6.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 g:=g||jsonb_build_object('gate','purpose','result',case when not (v_purpose->>'listed')::boolean or not (v_purpose->>'enabled')::boolean then 'refuse'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'not_wired' else 'pass' end,
  'detail',case when not (v_purpose->>'listed')::boolean then 'purpose_not_allowed' when not (v_purpose->>'enabled')::boolean then 'purpose_disabled'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'approval: slice 6' else 'list v'||(v_purpose->>'list_version') end);
 g:=g||jsonb_build_object('gate','persona_excluded','result',case when runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)<>'real' then 'refuse' else 'pass' end,
   'detail',runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer));
 g:=g||jsonb_build_object('gate','quiet_hours','result',case when (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to))
   then 'refuse' else 'pass' end,'detail',d->>'quiet_hours');
 v_n24:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '24 hours');
 v_n7:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '7 days');
 g:=g||jsonb_build_object('gate','frequency','result',case when v_n24>=(d->>'per_24h')::integer or v_n7>=(d->>'per_7d')::integer then 'refuse' else 'pass' end,
   'detail',jsonb_build_object('last_24h',v_n24,'last_7d',v_n7));
 v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,p_consumer,'native.webchat',p_purpose,gen_random_uuid(),p_at);
 g:=g||jsonb_build_object('gate','consumer_preference','result',case when v_code is null then 'pass' else 'refuse' end,'detail',v_code);
 v_layer:=control.nexloop_real_dispatch_closed_layer(p_tenant,p_world,'eios:action:nexloop.outreach.send:1');
 g:=g||jsonb_build_object('gate','real_dispatch','result',case when v_layer is null then 'pass' else 'refuse' end,'detail',coalesce(v_layer,'open'));
 g:=g||jsonb_build_object('gate','budget','result','not_wired','detail','outreach Action and budget: slice 4');
 return g;
end $$;

-- 6. The owner's view of the purposes (workbench observe; every other verb unchanged) ----------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_outreach_purposes;
revoke all on function authz.nexloop_workbench_observe_read_before_outreach_purposes(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;
begin
 if c->>'verb' is distinct from 'outreach_purposes' then
  return authz.nexloop_workbench_observe_read_before_outreach_purposes(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as the earlier verbs).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return jsonb_build_object('list_version',(select max(version) from control.nexloop_outreach_purpose_lists),
  'purposes',coalesce((select jsonb_agg(runtime.nexloop_outreach_purpose(v_tenant,p->>'purpose_id')||jsonb_build_object('revision',
     coalesce((select s.revision from control.nexloop_outreach_purpose_states s where s.tenant_id=v_tenant and s.purpose_id=p->>'purpose_id'),0))
    order by ord) from jsonb_array_elements((select definition->'purposes' from control.nexloop_outreach_purpose_lists order by version desc limit 1)) with ordinality x(p,ord)),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
