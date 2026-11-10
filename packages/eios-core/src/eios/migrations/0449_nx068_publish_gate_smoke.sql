-- NX-068 (P0 2026-11-10 Q8/Q9; docs/planning-drafts/nx068-publish-gate-and-smoke-20261111.md, L6 conditional PASS N1-N4, owner
-- 2026-11-11 "设计拍板表都按推荐"): a Schema publication is judged before it is written and smoke-checked after it. Temporary number;
-- the dispatcher renumbers. Nothing here grants anything or writes authority.
-- 1. authz.nexloop_effect_successor_admissible (N1): the single pure judgement of ADR-046 A' (an effect Action's successor proves the
--    Consumer scope of the executed :1), split out of authz.nexloop_effect_scope_successor_proof (0446) without a behaviour change:
--    the proof now asks it for each candidate and still alone records a proof. It reads and writes no table. The gate (G1) and the
--    post-publication smoke (S3, N4) call it too; option B (ADR-046 §3) turns it into the admission judgement.
-- 2. authz.nexloop_schema_publish_gate: the pre-publication judgement over the effect Actions (G1), the effect context registrar and
--    `<Type>.create` (G2/G3: every service holder must carry register's follow marker, which register writes only for the FOLLOWABLE
--    grants, N2 option 1), the other Actions bound to the type (info) and an empty affected set while other versions are bound (warn).
--    ontology.nexloop_schema_publication_gates (0254/0257) gets a wrapper of the same name: in the publish transaction, after the
--    permit check and before anything is written, a refuse item is a gate code, so the publication answers published:false
--    (source gates), writes nothing and leaves the permit unused (N3: no refusal audit until V3).
-- 3. authz.nexloop_read_schema_publish_readiness: the reviewer's read-only precheck (F3 check), the same gate on the proposal's next
--    Schema version; the successors are previewed in SQL as F3 builds them (the review read payload is bounded).
-- 4. runtime.nexloop_schema_publish_smokes: runtime.nexloop_schema_watch_start (0257, called once by each publication) gets a wrapper
--    that also queues two read-only smoke rounds (1 and 15 minutes, owner: 15; holder grants are checked in round 2, after register). runtime.nexloop_observability_maintain (run by the
--    alert evaluator once a minute) gets a wrapper that runs the due rounds; the metrics snapshot gains schema_publish_smoke for alert
--    rule schema_publish_smoke_failed (a failed round in 24 hours, or a round 30 minutes overdue).

-- 1. The single A' judgement ------------------------------------------------------------------------------------------------------
create function authz.nexloop_effect_successor_admissible(p_tenant text,p_type text,p_executed jsonb,p_successor jsonb,p_type_from jsonb,
  p_type_to jsonb,p_version integer,p_publication jsonb) returns jsonb
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
-- p_executed / p_successor: {resource_id, capability, definition[, active]}; p_type_from / p_type_to: the type's definitions at the
-- executed scope's version b and at p_version; p_publication: {published_by, permission_digest_before, permission_digest_after}
-- of the publication that names the successor, or null. Returns {admissible, code}; code null when admissible.
declare b integer;nb integer;tb jsonb:=p_type_from;tv jsonb:=p_type_to;
begin
 if p_type is distinct from 'Consumer' then return jsonb_build_object('admissible',false,'code','type_not_provable');end if;
 select min((r->>'version')::integer),count(*) into b,nb
  from jsonb_array_elements(coalesce(p_executed->'definition'->'governance'->'change_scope'->'object_types','[]'::jsonb)) r
  where r->>'tenant_id'=p_tenant and r->>'stable_name'=p_type;
 if nb<>1 or b is null then return jsonb_build_object('admissible',false,'code','executed_scope_not_single');end if;
 if p_version is null or p_version<=b then return jsonb_build_object('admissible',false,'code','version_not_newer');end if;
 -- D1/D2: the type at p_version extends b cumulatively.
 if tb is null or tv is null then return jsonb_build_object('admissible',false,'code','type_version_missing');end if;
 if (tb-'version'-'properties') is distinct from (tv-'version'-'properties')
  or jsonb_array_length(coalesce(tv->'properties','[]'::jsonb))<=jsonb_array_length(coalesce(tb->'properties','[]'::jsonb))
  or exists(select 1 from jsonb_array_elements(coalesce(tb->'properties','[]'::jsonb)) with ordinality o(e,n)
   where e is distinct from tv->'properties'->(n::integer-1))
  or exists(select 1 from jsonb_array_elements(tv->'properties') with ordinality a(e,n) where n>jsonb_array_length(coalesce(tb->'properties','[]'::jsonb))
   and (coalesce((e->>'required')::boolean,false) or coalesce((e->>'nullable')::boolean,false) is not true
    or coalesce(e->>'visibility','normal')='hidden' or coalesce(e->'default_value','null'::jsonb)<>'null'::jsonb))
 then return jsonb_build_object('admissible',false,'code','type_not_cumulative');end if;
 -- A'-1: an active successor of the same stable name, equal without versions, its scope holding the type at p_version.
 if p_successor is null then return jsonb_build_object('admissible',false,'code','successor_missing');end if;
 if (p_successor->>'active')::boolean is not true then return jsonb_build_object('admissible',false,'code','successor_inactive');end if;
 if p_successor->>'resource_id' is not distinct from p_executed->>'resource_id' then return jsonb_build_object('admissible',false,'code','successor_same_resource');end if;
 if p_successor->'definition'->>'stable_name' is distinct from p_executed->'definition'->>'stable_name' then
  return jsonb_build_object('admissible',false,'code','successor_other_stable_name');end if;
 if not exists(select 1 from jsonb_array_elements(coalesce(p_successor->'definition'->'governance'->'change_scope'->'object_types','[]'::jsonb)) r
   where r->>'tenant_id'=p_tenant and r->>'stable_name'=p_type and (r->>'version')::integer=p_version) then
  return jsonb_build_object('admissible',false,'code','successor_scope_wrong_version');end if;
 if p_successor->'capability' is distinct from p_executed->'capability' then return jsonb_build_object('admissible',false,'code','successor_capability_differs');end if;
 if authz.nexloop_effect_definition_unversioned(p_successor->'definition') is distinct from authz.nexloop_effect_definition_unversioned(p_executed->'definition') then
  return jsonb_build_object('admissible',false,'code','successor_not_equivalent');end if;
 -- A'-2: named by a human publication that left the permission digest unchanged.
 if p_publication is null then return jsonb_build_object('admissible',false,'code','no_publication_record');end if;
 if coalesce(p_publication->>'published_by','') not like 'human:%' then return jsonb_build_object('admissible',false,'code','publisher_not_human');end if;
 if p_publication->>'permission_digest_before' is null or p_publication->>'permission_digest_before' is distinct from p_publication->>'permission_digest_after' then
  return jsonb_build_object('admissible',false,'code','permission_digest_changed');end if;
 return jsonb_build_object('admissible',true,'code',null);
end $$;
alter function authz.nexloop_effect_successor_admissible(text,text,jsonb,jsonb,jsonb,jsonb,integer,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_successor_admissible(text,text,jsonb,jsonb,jsonb,jsonb,integer,jsonb) from public;

-- 0446's proof, the same signature and result: candidates in the old order (the highest version first); the highest one that is the
-- executed definition's equal decides, as the old single select did - a type or publication refusal ends the search.
create or replace function authz.nexloop_effect_scope_successor_proof(p_context uuid,p_tenant text,p_world text,p_executed control.nexloop_action_definitions,p_version integer)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare b integer;tb jsonb;tv jsonb;ex jsonb;d control.nexloop_action_definitions;p ontology.nexloop_schema_publications;r jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);  -- the intent's own tenant (ctx.tenant_id): the tables below are tenant-policied
 ex:=jsonb_build_object('resource_id',p_executed.resource_id,'capability',p_executed.capability,'definition',p_executed.definition);
 select min((x->>'version')::integer) into b from jsonb_array_elements(p_executed.definition->'governance'->'change_scope'->'object_types') x
  where x->>'tenant_id'=p_tenant and x->>'stable_name'='Consumer';
 select definition into tb from ontology.object_type_versions where tenant_id=p_tenant and type_name='Consumer' and version=b;
 select definition into tv from ontology.object_type_versions where tenant_id=p_tenant and type_name='Consumer' and version=p_version;
 for d in select x.* from control.nexloop_action_definitions x where x.tenant_id=p_tenant and x.world=p_world and x.active
   and x.resource_id<>p_executed.resource_id and x.definition->>'stable_name'=p_executed.definition->>'stable_name'
   order by (x.definition->>'version')::integer desc loop
  select q.* into p from ontology.nexloop_schema_publications q where q.tenant_id=p_tenant and q.world=p_world and q.type_name='Consumer'
   and q.version_after=p_version and d.resource_id=any(q.successor_actions)
   order by (q.published_by like 'human:%' and q.permission_digest_before=q.permission_digest_after) desc,q.published_at limit 1;
  r:=authz.nexloop_effect_successor_admissible(p_tenant,'Consumer',ex,
   jsonb_build_object('resource_id',d.resource_id,'capability',d.capability,'definition',d.definition,'active',d.active),tb,tv,p_version,
   case when p.publication_id is null then null else jsonb_build_object('published_by',p.published_by,
    'permission_digest_before',p.permission_digest_before,'permission_digest_after',p.permission_digest_after) end);
  if (r->>'admissible')::boolean then
   -- A'-4: the executed and the proving resource are both recorded.
   insert into control.nexloop_effect_scope_proofs(tenant_id,world,context_id,executed_resource,scope_proof_resource,publication_id,consumer_version)
    values(p_tenant,p_world,p_context,p_executed.resource_id,d.resource_id,p.publication_id,p_version) on conflict do nothing;
   return jsonb_build_object('executed_resource',p_executed.resource_id,'scope_proof_resource',d.resource_id,'publication_id',p.publication_id,'consumer_version',p_version);
  end if;
  if r->>'code' not in ('successor_scope_wrong_version','successor_capability_differs','successor_not_equivalent') then return null;end if;
 end loop;
 return null;
end $$;

-- 2. The pre-publication gate -----------------------------------------------------------------------------------------------------
-- The successor F3 builds for a definition (schema_publication.successors), as far as the gate compares it: the next version, each
-- reference to the type at the new version. The precheck's stand-in for the publication's own successors.
create function authz.nexloop_schema_successor_preview(p jsonb,p_type text,p_version integer) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_set(jsonb_set(jsonb_set(p,'{version}',to_jsonb((p->>'version')::integer+1)),'{object_types}',
   coalesce((select jsonb_agg(case when r->>'stable_name'=p_type then jsonb_set(r,'{version}',to_jsonb(p_version)) else r end order by n)
    from jsonb_array_elements(p->'object_types') with ordinality q(r,n)),'[]'::jsonb)),
  '{governance,change_scope,object_types}',
   coalesce((select jsonb_agg(case when r->>'stable_name'=p_type then jsonb_set(r,'{version}',to_jsonb(p_version)) else r end order by n)
    from jsonb_array_elements(p->'governance'->'change_scope'->'object_types') with ordinality q(r,n)),'[]'::jsonb))
$$;
alter function authz.nexloop_schema_successor_preview(jsonb,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_schema_successor_preview(jsonb,text,integer) from public;

-- p_pub: {schema, actions?}; without actions (the precheck) the successors are previewed from the latest active definitions.
-- Returns {verdict pass|warn|refuse, type_name, from_version, to_version, items:[{level, code, action}]}.
create function authz.nexloop_schema_publish_gate(p_tenant text,p_type text,p_pub jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare oldv integer;tv jsonb:=p_pub->'schema';v integer;items jsonb:='[]'::jsonb;e record;ex control.nexloop_action_definitions;
 a control.nexloop_action_definitions;succ jsonb;b integer;tb jsonb;r jsonb;holder text;n integer:=0;kind text;name text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select max(version) into oldv from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type;
 v:=(tv->>'version')::integer;
 if oldv is null or jsonb_typeof(tv) is distinct from 'object' or v is null then
  return jsonb_build_object('verdict','pass','type_name',p_type,'from_version',oldv,'to_version',v,'items','[]'::jsonb);end if;
 -- G1: each effect Action whose executed version is bound to the type; A' (until option B) must be able to prove its successor.
 for e in select stable_name,resource_id from control.nexloop_effect_actions order by stable_name loop
  select * into ex from control.nexloop_action_definitions where tenant_id=p_tenant and world='real' and resource_id=e.resource_id;
  continue when ex.resource_id is null or not exists(select 1 from jsonb_array_elements(ex.definition->'governance'->'change_scope'->'object_types') x
   where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type);
  succ:=null;
  if p_pub ? 'actions' then
   select jsonb_build_object('resource_id','eios:action:'||(x->'definition'->>'stable_name')||':'||(x->'definition'->>'version'),
     'capability',x->'capability','definition',x->'definition','active',true) into succ
    from jsonb_array_elements(coalesce(p_pub->'actions','[]'::jsonb)) x where x->'definition'->>'stable_name'=e.stable_name limit 1;
  else
   select jsonb_build_object('resource_id','eios:action:'||e.stable_name||':'||((d.definition->>'version')::integer+1),'capability',d.capability,
     'definition',authz.nexloop_schema_successor_preview(d.definition,p_type,v),'active',true) into succ
    from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.active and d.definition->>'stable_name'=e.stable_name
    order by (d.definition->>'version')::integer desc limit 1;
  end if;
  select min((x->>'version')::integer) into b from jsonb_array_elements(ex.definition->'governance'->'change_scope'->'object_types') x
   where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type;
  select definition into tb from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=b;
  r:=authz.nexloop_effect_successor_admissible(p_tenant,p_type,
   jsonb_build_object('resource_id',ex.resource_id,'capability',ex.capability,'definition',ex.definition),succ,tb,tv,v,
   jsonb_build_object('published_by','human:publish-gate','permission_digest_before','unchanged','permission_digest_after','unchanged'));
  if (r->>'admissible')::boolean is not true then
   items:=items||jsonb_build_object('level','refuse','code','effect_successor_unprovable:'||(r->>'code'),'action',e.resource_id);end if;
 end loop;
 -- The Actions bound to the type's current version (F3 publishes a successor of each).
 for a in select x.* from control.nexloop_action_definitions x where x.tenant_id=p_tenant and x.world='real' and x.active
   and exists(select 1 from jsonb_array_elements(x.definition->'governance'->'change_scope'->'object_types') y
    where y->>'tenant_id'=p_tenant and y->>'stable_name'=p_type and (y->>'version')::integer=oldv) order by x.resource_id loop
  n:=n+1;name:=a.definition->>'stable_name';
  kind:=case when name in ('nexloop.plan.bind_effect_context','nexloop.effect.configure_control') then 'registrar'
             when name=p_type||'.create' then 'create' end;
  if kind is null then
   if not exists(select 1 from control.nexloop_effect_actions where stable_name=name) then
    items:=items||jsonb_build_object('level','info','code','successor_coexists','action',a.resource_id);end if;
   continue;
  end if;
  -- G2/G3: the successor only re-binds the type (L6 Q4, create_scope_other_type) ...
  succ:=null;
  if p_pub ? 'actions' then
   select x into succ from jsonb_array_elements(coalesce(p_pub->'actions','[]'::jsonb)) x where x->'definition'->>'stable_name'=name limit 1;
  else
   succ:=jsonb_build_object('capability',a.capability,'definition',authz.nexloop_schema_successor_preview(a.definition,p_type,v));
  end if;
  if succ is null then
   items:=items||jsonb_build_object('level','refuse','code',kind||'_successor_missing','action',a.resource_id);
  elsif succ->'capability' is distinct from a.capability
   or authz.nexloop_effect_definition_unversioned(succ->'definition') is distinct from authz.nexloop_effect_definition_unversioned(a.definition) then
   items:=items||jsonb_build_object('level','refuse','code',kind||'_successor_not_rebinding','action',a.resource_id);
  elsif exists(select 1 from jsonb_array_elements(a.definition->'governance'->'change_scope'->'object_types') with ordinality o(x,i)
    where x->>'stable_name'<>p_type and x->'version' is distinct from succ->'definition'->'governance'->'change_scope'->'object_types'->(i::integer-1)->'version') then
   items:=items||jsonb_build_object('level','refuse','code',kind||'_successor_scope_other_type','action',a.resource_id);
  end if;
  -- ... and every service holder of the Action (any version) follows it: register's follow marker (FOLLOWABLE grants only, N2).
  for holder in select distinct f.entity_key[1] from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='grants'
    and f.payload->>'subject_kind'='service' and jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0
    and regexp_replace(f.entity_key[2],':[0-9]+$','')='eios:action:'||name order by 1 loop
   if not exists(select 1 from authz.nexloop_authority_facts g where g.tenant_id=p_tenant and g.fact_kind='grants' and g.entity_key[1]=holder
     and regexp_replace(g.entity_key[2],':[0-9]+$','')='eios:action:'||name
     and exists(select 1 from jsonb_array_elements(g.payload->'grants') y where y->>'grant_id' like '%:follow_latest_version')) then
    items:=items||jsonb_build_object('level','refuse','code',kind||'_holder_without_follow:'||holder,'action',a.resource_id);end if;
  end loop;
 end loop;
 -- Nothing bound to the current version while other versions are: say so, never a silent pass.
 if n=0 and exists(select 1 from control.nexloop_action_definitions x where x.tenant_id=p_tenant and x.world='real' and x.active
   and exists(select 1 from jsonb_array_elements(x.definition->'governance'->'change_scope'->'object_types') y
    where y->>'tenant_id'=p_tenant and y->>'stable_name'=p_type)) then
  items:=items||jsonb_build_object('level','warn','code','scope_lineage_gap','action',null);end if;
 return jsonb_build_object('verdict',case when exists(select 1 from jsonb_array_elements(items) i where i->>'level'='refuse') then 'refuse'
   when exists(select 1 from jsonb_array_elements(items) i where i->>'level'='warn') then 'warn' else 'pass' end,
  'type_name',p_type,'from_version',oldv,'to_version',v,'items',items);
end $$;
alter function authz.nexloop_schema_publish_gate(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_schema_publish_gate(text,text,jsonb) from public;

alter function ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text) rename to nexloop_schema_publication_gates_before_publish_gate;
revoke all on function ontology.nexloop_schema_publication_gates_before_publish_gate(text,ontology.nexloop_schema_proposals,text,jsonb,text) from public;
create function ontology.nexloop_schema_publication_gates(p_tenant text,r ontology.nexloop_schema_proposals,p_kind text,p_pub jsonb,p_hide text)
 returns text[] language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare g text[]:=ontology.nexloop_schema_publication_gates_before_publish_gate(p_tenant,r,p_kind,p_pub,p_hide);v jsonb;
begin
 if cardinality(g)>0 then return g;end if;
 v:=authz.nexloop_schema_publish_gate(p_tenant,r.type_name,p_pub);
 perform set_config('eios.tenant_id',p_tenant,true);
 return coalesce((select array_agg('publish_gate:'||(i->>'code') order by n) from jsonb_array_elements(v->'items') with ordinality q(i,n)
  where i->>'level'='refuse'),'{}'::text[]);
end $$;
alter function ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text) owner to nexloop_owner;
revoke all on function ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text) from public;
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='ontology.nexloop_schema_publication_gates_before_publish_gate(text,ontology.nexloop_schema_proposals,text,jsonb,text)'::regprocedure
    and a.privilege_type='EXECUTE' and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text) to %I',g);
  execute format('revoke execute on function ontology.nexloop_schema_publication_gates_before_publish_gate(text,ontology.nexloop_schema_proposals,text,jsonb,text) from %I',g);
 end loop;
end $$;

-- 3. The reviewer's precheck (read only, the review read guard of 0254/0322) ---------------------------------------------------------
create function authz.nexloop_read_schema_publish_readiness(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 r ontology.nexloop_schema_proposals;v jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if coalesce(c->>'proposal_id','')!~'^[0-9a-f-]{36}$' or jsonb_typeof(c->'schema') is distinct from 'object' then
  raise exception 'schema publish readiness invalid' using errcode='22023';end if;
 select * into r from ontology.nexloop_schema_proposals where tenant_id=t and world=p_world and proposal_id=(c->>'proposal_id')::uuid;
 if not found then return null;end if;
 v:=authz.nexloop_schema_publish_gate(t,r.type_name,jsonb_build_object('schema',c->'schema'));
 perform set_config('eios.tenant_id',t,true);
 return v||jsonb_build_object('proposal_id',r.proposal_id);
end $$;
alter function authz.nexloop_read_schema_publish_readiness(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_schema_publish_readiness(text,text,text,text,text) from public;
grant execute on function authz.nexloop_read_schema_publish_readiness(text,text,text,text,text) to nexloop_api;

-- 4. Post-publication smoke -------------------------------------------------------------------------------------------------------
create table runtime.nexloop_schema_publish_smokes (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),publication_id uuid not null,
 round smallint not null check(round in (1,2)),type_name text not null,due_at timestamptz not null,
 outcome text not null default 'pending' check(outcome in ('pending','passed','failed')),ran_at timestamptz,
 checks jsonb not null default '[]'::jsonb check(jsonb_typeof(checks)='array'),baseline numeric,
 primary key(tenant_id,world,publication_id,round),
 check((outcome='pending')=(ran_at is null))
);
alter table runtime.nexloop_schema_publish_smokes owner to nexloop_owner;
alter table runtime.nexloop_schema_publish_smokes enable row level security;
alter table runtime.nexloop_schema_publish_smokes force row level security;
create policy schema_publish_smokes_tenant on runtime.nexloop_schema_publish_smokes to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_schema_publish_smokes from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity,nexloop_metrics;
-- NX-041 outbound guard: rows are queued by the publication and settled by the alert evaluator's maintenance; type names, codes and
-- counts only, no external effect.
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('runtime.nexloop_schema_publish_smokes',false,'NX-068 post-publication smoke rounds (publication id, type name, check codes, a relay failure count); read-only checks, no external effect');

alter function runtime.nexloop_schema_watch_start(text,text,uuid,text) rename to nexloop_schema_watch_start_before_publish_smoke;
revoke all on function runtime.nexloop_schema_watch_start_before_publish_smoke(text,text,uuid,text) from public;
create function runtime.nexloop_schema_watch_start(p_tenant text,p_world text,p_publication uuid,p_type text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n timestamptz:=clock_timestamp();
begin
 perform runtime.nexloop_schema_watch_start_before_publish_smoke(p_tenant,p_world,p_publication,p_type);
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_schema_publish_smokes(tenant_id,world,publication_id,round,type_name,due_at)
  values(p_tenant,p_world,p_publication,1,p_type,n+interval '1 minute'),(p_tenant,p_world,p_publication,2,p_type,n+interval '15 minutes')
  on conflict do nothing;
end $$;
alter function runtime.nexloop_schema_watch_start(text,text,uuid,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_schema_watch_start(text,text,uuid,text) from public;
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='runtime.nexloop_schema_watch_start_before_publish_smoke(text,text,uuid,text)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function runtime.nexloop_schema_watch_start(text,text,uuid,text) to %I',g);
  execute format('revoke execute on function runtime.nexloop_schema_watch_start_before_publish_smoke(text,text,uuid,text) from %I',g);
 end loop;
end $$;

-- The checks (S1-S3) for one tenant and type: a list of failure codes, empty when everything holds. Read only. p_holders: also
-- require every service holder to hold the covering version - round 2 only, since register derives the followed grants after the
-- publication (run sheet: publish, register, smoke); round 1 checks coverage and the effect chain.
create function runtime.nexloop_schema_publish_smoke_checks(p_tenant text,p_world text,p_type text,p_holders boolean) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare fails jsonb:='[]'::jsonb;latest integer;tv jsonb;name text;cover text;holder text;e record;ex control.nexloop_action_definitions;
 succ control.nexloop_action_definitions;p ontology.nexloop_schema_publications;b integer;tb jsonb;r jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select max(version) into latest from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type;
 select definition into tv from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=latest;
 -- S1 (registrar) and S2 (create): a version covers the latest type version and every service holder holds it.
 foreach name in array array['nexloop.plan.bind_effect_context','nexloop.effect.configure_control',p_type||'.create'] loop
  continue when not exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=p_world and d.active
   and d.definition->>'stable_name'=name and exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') x
    where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type));
  cover:=null;
  select d.resource_id into cover from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=p_world and d.active
   and d.definition->>'stable_name'=name and exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') x
    where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type and (x->>'version')::integer=latest)
   order by (d.definition->>'version')::integer desc limit 1;
  if cover is null then fails:=fails||to_jsonb('uncovered:'||name);continue;end if;
  continue when not p_holders;
  for holder in select distinct f.entity_key[1] from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='grants'
    and f.payload->>'subject_kind'='service' and jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0
    and regexp_replace(f.entity_key[2],':[0-9]+$','')='eios:action:'||name order by 1 loop
   if not exists(select 1 from authz.nexloop_authority_facts g where g.tenant_id=p_tenant and g.fact_kind='grants' and g.entity_key[1]=holder
     and g.entity_key[2]=cover and jsonb_array_length(coalesce(g.payload->'grants','[]'::jsonb))>0) then
    fails:=fails||to_jsonb('holder_without_grant:'||holder||':'||cover);end if;
  end loop;
 end loop;
 -- S3 (N4): each effect Action bound to the type can be proven at the latest version, by the pure judgement (no proof written).
 for e in select stable_name,resource_id from control.nexloop_effect_actions order by stable_name loop
  select * into ex from control.nexloop_action_definitions where tenant_id=p_tenant and world=p_world and resource_id=e.resource_id;
  continue when ex.resource_id is null or not exists(select 1 from jsonb_array_elements(ex.definition->'governance'->'change_scope'->'object_types') x
   where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type);
  select min((x->>'version')::integer) into b from jsonb_array_elements(ex.definition->'governance'->'change_scope'->'object_types') x
   where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type;
  continue when b=latest;
  select definition into tb from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=b;
  select d.* into succ from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=p_world and d.active
   and d.definition->>'stable_name'=e.stable_name and d.resource_id<>ex.resource_id
   and exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') x
    where x->>'tenant_id'=p_tenant and x->>'stable_name'=p_type and (x->>'version')::integer=latest)
   order by (d.definition->>'version')::integer desc limit 1;
  select q.* into p from ontology.nexloop_schema_publications q where q.tenant_id=p_tenant and q.world=p_world and q.type_name=p_type
   and q.version_after=latest and succ.resource_id=any(q.successor_actions)
   order by (q.published_by like 'human:%' and q.permission_digest_before=q.permission_digest_after) desc,q.published_at limit 1;
  r:=authz.nexloop_effect_successor_admissible(p_tenant,p_type,
   jsonb_build_object('resource_id',ex.resource_id,'capability',ex.capability,'definition',ex.definition),
   case when succ.resource_id is null then null else jsonb_build_object('resource_id',succ.resource_id,'capability',succ.capability,
    'definition',succ.definition,'active',succ.active) end,tb,tv,latest,
   case when p.publication_id is null then null else jsonb_build_object('published_by',p.published_by,
    'permission_digest_before',p.permission_digest_before,'permission_digest_after',p.permission_digest_after) end);
  if (r->>'admissible')::boolean is not true then fails:=fails||to_jsonb('effect_unprovable:'||e.stable_name||':'||(r->>'code'));end if;
 end loop;
 return fails;
end $$;
alter function runtime.nexloop_schema_publish_smoke_checks(text,text,text,boolean) owner to nexloop_owner;
revoke all on function runtime.nexloop_schema_publish_smoke_checks(text,text,text,boolean) from public;

-- Runs the due rounds of every tenant (at most 20 each). Round 1: coverage, the effect chain and the relay baseline (S4); round 2:
-- the same with every holder's grant, and relay replies failing without success now while the baseline was 0.
create function runtime.nexloop_run_schema_publish_smokes() returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;s runtime.nexloop_schema_publish_smokes;fails jsonb;base numeric;now_v numeric;n integer:=0;
begin
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  for s in select * from runtime.nexloop_schema_publish_smokes where tenant_id=t and outcome='pending' and due_at<=clock_timestamp()
    order by due_at limit 20 for update skip locked loop
   now_v:=coalesce((runtime.nexloop_metrics_snapshot(t,s.world)->'relay_replies'->>'failing_without_success_15m')::numeric,0);
   fails:=runtime.nexloop_schema_publish_smoke_checks(t,s.world,s.type_name,s.round=2);
   if s.round=2 then
    perform set_config('eios.tenant_id',t,true);
    select baseline into base from runtime.nexloop_schema_publish_smokes where tenant_id=t and world=s.world and publication_id=s.publication_id and round=1;
    if now_v>0 and coalesce(base,0)=0 then fails:=fails||'["relay_replies_failing_since_publication"]'::jsonb;end if;
   end if;
   perform set_config('eios.tenant_id',t,true);
   update runtime.nexloop_schema_publish_smokes set outcome=case when jsonb_array_length(fails)=0 then 'passed' else 'failed' end,
     ran_at=clock_timestamp(),checks=fails,baseline=now_v
    where tenant_id=t and world=s.world and publication_id=s.publication_id and round=s.round;
   n:=n+1;
  end loop;
 end loop;
 perform set_config('eios.tenant_id','',true);
 return n;
end $$;
alter function runtime.nexloop_run_schema_publish_smokes() owner to nexloop_owner;
revoke all on function runtime.nexloop_run_schema_publish_smokes() from public;

-- The alert evaluator's maintenance runs the due rounds; a failing run leaves them pending (overdue after 30 minutes, alerted).
alter function runtime.nexloop_observability_maintain() rename to nexloop_observability_maintain_before_publish_smoke;
revoke all on function runtime.nexloop_observability_maintain_before_publish_smoke() from public;
create function runtime.nexloop_observability_maintain() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_observability_maintain_before_publish_smoke();k integer;
begin
 begin
  k:=runtime.nexloop_run_schema_publish_smokes();
 exception when others then
  k:=-1;
 end;
 return coalesce(v,'{}'::jsonb)||jsonb_build_object('publish_smokes',k);
end $$;
alter function runtime.nexloop_observability_maintain() owner to nexloop_owner;
revoke all on function runtime.nexloop_observability_maintain() from public;
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='runtime.nexloop_observability_maintain_before_publish_smoke()'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function runtime.nexloop_observability_maintain() to %I',g);
  execute format('revoke execute on function runtime.nexloop_observability_maintain_before_publish_smoke() from %I',g);
 end loop;
end $$;

-- The metrics snapshot gains schema_publish_smoke (stacked on 0446's relay_replies wrapper the same way).
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_publish_smoke;
revoke all on function runtime.nexloop_metrics_snapshot_before_publish_smoke(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_publish_smoke(p_tenant,p_world);failed bigint;overdue bigint;
begin
 if jsonb_typeof(v) is distinct from 'object' then return v;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select count(*) filter (where outcome='failed' and ran_at>clock_timestamp()-interval '24 hours'),
        count(*) filter (where outcome='pending' and due_at<clock_timestamp()-interval '30 minutes')
   into failed,overdue from runtime.nexloop_schema_publish_smokes where tenant_id=p_tenant and world=p_world;
 return v||jsonb_build_object('schema_publish_smoke',jsonb_build_object('failed_24h',failed,'overdue',overdue,'problems',failed+overdue));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='runtime.nexloop_metrics_snapshot_before_publish_smoke(text,text)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function runtime.nexloop_metrics_snapshot(text,text) to %I',g);
  execute format('revoke execute on function runtime.nexloop_metrics_snapshot_before_publish_smoke(text,text) from %I',g);
 end loop;
end $$;
