-- A synthetic Consumer's erasure checks the principal's WebChat grants: revoke first, erase, then verify; fail closed (stage
-- 2026-11-09: customer-4's Consumer was erased on 10-30 but its principal still held nexloop.conversation.read and four other
-- non-empty grant sets; the 0220 erasure deletes the Consumer's data and its owner link, never the 0050 authority facts, which only
-- webchat_consumers.py `revoke: true` empties). Temporary number; the dispatcher renumbers.
--
-- Why at erasure time: after the erasure nothing ties the Consumer to its principal. The owner link is deleted (0306/0388) and the
-- contact keys (0358 D5) are HMACs of the Consumer's external_id_refs, which hold a browser-local reference only after the
-- browser identity link (0061/0308, link_authenticated_identity, Consumer edit authority) that the WebChat consumer role never
-- grants (tests/test_erase_synthetic_consumer_pg.py: test_after_a_real_erasure_a_webchat_principal_is_untraceable).
--
-- 1. control.nexloop_consumer_erasures.grants_cleared boolean: set once by the synthetic erasure below. true: at registration the
--    Consumer's owner principals were known and every one had `grants` facts, all empty (revoked); false: no owner principal, or
--    an owner without any `grants` fact (never configured or not traceable: not evidence of revocation); null: registered before
--    this migration or through another path (consumer.erase, the restore replay). No principal is stored.
-- 2. control.nexloop_consumer_webchat_grants(tenant, world, consumer, request): {principals, facts, held, via}: the principals of
--    the Consumer (owner link; else, for an erasure request, a principal whose browser identity references match a contact key of
--    that request by the tenant's salted HMAC; a revoked contact key still counts), how many `grants` facts they have and how many
--    are non-empty. Owner-only internal function; numbers only.
-- 3. control.nexloop_governed_erase_synthetic_consumer (0220) redefined in place (create or replace; the registry references it as
--    regprocedure and is append-only): a new request whose owner principals hold a
--    non-empty grant set is refused with 'consumer erasure: grants_held' (22023; workbench 409, details.reason grants_held) and
--    nothing is written; otherwise the 0220 body runs and the new request's grants_cleared is set. A replay is the 0220 body.
-- 4. The owner's erasure precheck (authz.nexloop_workbench_observe_read, renamed once and wrapped; every other verb unchanged) gains
--    held.grants_held and held.grants_trace:
--      principals traced and every one has grants facts -> the non-empty count; trace owner_link or contact_keys;
--      principals traced but some without any grants fact -> null; trace no_grant_facts;
--      none traced, erasure registered with grants_cleared true -> 0; trace cleared_at_erasure;
--      none traced, erasure registered otherwise -> null; trace untraceable;
--      none traced, no erasure -> 0; trace no_owner (a pre-erasure read, never a post-check).
--    The owner tool fails its post-check on null or above zero.

-- 1. The marker ---------------------------------------------------------------------------------------------------------------
alter table control.nexloop_consumer_erasures add column grants_cleared boolean;

-- 2. Grants of a Consumer's principals -------------------------------------------------------------------------------------------
create function control.nexloop_consumer_webchat_grants(p_tenant text,p_world text,p_consumer text,p_request text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);k bytea;v_via text;v_principals text[];v_facts bigint;v_held bigint;v_bare bigint;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(array_agg(o.principal_id order by o.principal_id),'{}') into v_principals from control.nexloop_consumer_owners o
  where o.tenant_id=p_tenant and o.world=p_world and o.consumer_id=p_consumer;
 v_via:='owner_link';
 if cardinality(v_principals)=0 and p_request is not null then
  select salt into k from control.nexloop_erasure_salts where tenant_id=p_tenant;
  if k is not null then
   select coalesce(array_agg(distinct m.principal_id),'{}') into v_principals from control.nexloop_browser_memberships m
    where m.tenant_id=p_tenant and exists(select 1 from unnest(control.nexloop_principal_identity_refs(p_tenant,p_world,m.principal_id)) ref
     join control.nexloop_erased_contact_keys x on x.tenant_id=p_tenant and x.world=p_world and x.request_id=p_request
      and x.key_digest=encode(extensions.hmac(convert_to(ref,'UTF8'),k,'sha256'),'hex'));
  end if;
  v_via:='contact_keys';
 end if;
 select count(*),count(*) filter (where jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0) into v_facts,v_held
  from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='grants' and f.entity_key[1]=any(v_principals);
 select count(*) into v_bare from unnest(v_principals) p where not exists(select 1 from authz.nexloop_authority_facts f
  where f.tenant_id=p_tenant and f.fact_kind='grants' and f.entity_key[1]=p);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('principals',cardinality(v_principals),'without_facts',v_bare,'facts',v_facts,'held',v_held,'via',v_via);
end $$;
alter function control.nexloop_consumer_webchat_grants(text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_consumer_webchat_grants(text,text,text,text) from public;

-- 3. The gate on the synthetic erasure (0220 body, redefined in place: the registry holds the handler as regprocedure and is
--    append-only, 0124) ---------------------------------------------------------------------------------------------------
create or replace function control.nexloop_governed_erase_synthetic_consumer(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_consumer text:=body->>'consumer_id';v_persona text;g jsonb;v_new boolean;r jsonb;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','reason'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'consumer erasure invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer)
  and not exists(select 1 from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer) then
  raise exception 'consumer unavailable' using errcode='42501';end if;
 v_persona:=runtime.nexloop_consumer_persona(p_tenant,p_world,v_consumer);
 if v_persona='real' then raise exception 'consumer erasure: persona_real' using errcode='22023';end if;
 -- 0429: a new request is refused while the owner principals hold a non-empty WebChat grant set; nothing is written.
 v_new:=not exists(select 1 from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer);
 if v_new then
  g:=control.nexloop_consumer_webchat_grants(p_tenant,p_world,v_consumer,null);
  if (g->>'held')::bigint>0 then raise exception 'consumer erasure: grants_held' using errcode='22023';end if;
 end if;
 r:=control.nexloop_governed_erase_consumer(p_tenant,p_world,p_principal,p_subject,p_intent,
  jsonb_build_object('operation','erase_consumer','request_id',body->>'request_id','consumer_id',v_consumer,'reason',body->>'reason'))
  ||jsonb_build_object('persona',v_persona);
 if v_new then
  update control.nexloop_consumer_erasures set grants_cleared=((g->>'principals')::int>0 and (g->>'without_facts')::bigint=0)
   where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer and grants_cleared is null;
 end if;
 return r;
end $$;

-- 4. The precheck --------------------------------------------------------------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_grants_held;
revoke all on function authz.nexloop_workbench_observe_read_before_grants_held(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb;v_tenant text;g jsonb;v_cleared boolean;v_count jsonb;v_trace text;
begin
 r:=authz.nexloop_workbench_observe_read_before_grants_held(p_digest,p_world,p_text,p_signature,p_payload);
 if p_payload::jsonb->>'verb' is distinct from 'erasure_precheck' then return r;end if;
 -- The wrapped body verified the owner, the signature and the payload, and set eios.tenant_id.
 v_tenant:=current_setting('eios.tenant_id',true);
 g:=control.nexloop_consumer_webchat_grants(v_tenant,p_world,r->>'consumer_id',r->'erasure'->>'request_id');
 select grants_cleared into v_cleared from control.nexloop_consumer_erasures where tenant_id=v_tenant and world=p_world and consumer_id=r->>'consumer_id';
 if (g->>'principals')::int>0 and (g->>'without_facts')::bigint=0 then v_count:=g->'held';v_trace:=g->>'via';
 elsif (g->>'principals')::int>0 then v_count:='null'::jsonb;v_trace:='no_grant_facts';
 elsif r->'erasure' is not null and r->'erasure'->>'request_id' is not null and v_cleared is true then v_count:='0'::jsonb;v_trace:='cleared_at_erasure';
 elsif r->'erasure' is not null and r->'erasure'->>'request_id' is not null then v_count:='null'::jsonb;v_trace:='untraceable';
 else v_count:='0'::jsonb;v_trace:='no_owner';end if;
 return jsonb_set(jsonb_set(r,'{held,grants_held}',v_count),'{held,grants_trace}',to_jsonb(v_trace));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
