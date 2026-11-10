-- A completed Consumer erasure leaves no owner link (stage 2026-11-10, L6: customer-4's Consumer, erased 2026-10-19 10:58:53Z, still had
-- its control.nexloop_consumer_owners row, world real, idempotency key business-setup-<uuid>-ownership). Temporary number; the dispatcher
-- renumbers.
--
-- Cause: historical residue, not a rebinding. The erasure completed under release 22 (bootstrap revision 0221). The owner-link delete
-- came with 0306 (control.nexloop_consumer_reference_erasure, 2026-10-21), an AFTER UPDATE trigger that fires only on the transition to
-- 'completed' (0306:72, kept by 0388); an erasure completed before 0306 was never revisited. Rebinding afterwards is not possible through
-- the governed path: ConsumerOwnership.create (0138 commit_owner) requires the Consumer object, which the erasure deletes (0138:268).
--
-- 1. Guard: no owner link is inserted or moved onto a Consumer that has an erasure registered past confirmation (blocking, settling,
--    waiting_unknown_effects, purging, completed, completed_with_holds): 'consumer erased' (42501). pending_confirmation is left out:
--    the Consumer is still live until the request is confirmed.
-- 2. control.nexloop_erasure_owner_residue_settle(tenant): for every completed erasure (not under a retention hold) whose Consumer still
--    has owner rows: when the owner principals hold no non-empty WebChat grant set, the erasure's grants_cleared marker (0429) is set
--    if it was null (true when every owner principal had grants facts, all empty: the same rule as 0429), then the owner rows are
--    deleted (the entry-ticket pseudonymisation trigger, 0392, runs as for a normal erasure). With grants still held the rows stay, so
--    the precheck keeps tracing them (grants_held > 0, owner_link) and the owner revokes first. Returns
--    {settled, kept_grants_held}. Every settled or kept Consumer writes one runtime.audit_events row (action
--    nexloop.consumer_erasure.owner_residue; payload tenant, world, consumer_id, request_id, outcome settled|kept; never a principal).
--    Run once below for every tenant; each tenant's counts go to one more audit row (action ...owner_residue_run) and a NOTICE, the
--    deployment evidence. Owner-only, numbers only.

create function control.nexloop_consumer_owner_erased_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 v:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=new.tenant_id and e.world=new.world and e.consumer_id=new.consumer_id
  and e.state<>'pending_confirmation');
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if v then raise exception 'consumer erased' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_consumer_owner_erased_guard() owner to nexloop_owner;
revoke all on function control.nexloop_consumer_owner_erased_guard() from public;
create trigger nexloop_consumer_owner_erased_guard before insert or update on control.nexloop_consumer_owners
 for each row execute function control.nexloop_consumer_owner_erased_guard();

create function control.nexloop_erasure_owner_residue_audit(p_tenant text,p_world text,p_consumer text,p_request text,p_outcome text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('erasure-owner-residue:'||gen_random_uuid(),'nexloop.consumer_erasure.owner_residue',p_tenant,'migration:erasure_owner_residue',
   'erasure_owner_residue',p_request,jsonb_build_object('tenant_id',p_tenant,'world',p_world,'consumer_id',p_consumer,'request_id',p_request,
    'outcome',p_outcome),clock_timestamp()) $$;
alter function control.nexloop_erasure_owner_residue_audit(text,text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_erasure_owner_residue_audit(text,text,text,text,text) from public;

create function control.nexloop_erasure_owner_residue_settle(p_tenant text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e record;g jsonb;n_settled integer:=0;n_kept integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for e in select x.world,x.consumer_id,x.request_id from control.nexloop_consumer_erasures x
   where x.tenant_id=p_tenant and x.state='completed'
    and exists(select 1 from control.nexloop_consumer_owners o where o.tenant_id=x.tenant_id and o.world=x.world and o.consumer_id=x.consumer_id)
   order by x.consumer_id for update of x loop
  if runtime.nexloop_retention_held(p_tenant,e.world,'consumer',e.consumer_id) then continue;end if;
  g:=control.nexloop_consumer_webchat_grants(p_tenant,e.world,e.consumer_id,null);
  perform set_config('eios.tenant_id',p_tenant,true);
  if (g->>'held')::bigint>0 then
   n_kept:=n_kept+1;perform control.nexloop_erasure_owner_residue_audit(p_tenant,e.world,e.consumer_id,e.request_id,'kept');continue;end if;
  update control.nexloop_consumer_erasures set grants_cleared=((g->>'principals')::int>0 and (g->>'without_facts')::bigint=0)
   where tenant_id=p_tenant and world=e.world and request_id=e.request_id and grants_cleared is null;
  delete from control.nexloop_consumer_owners where tenant_id=p_tenant and world=e.world and consumer_id=e.consumer_id;
  n_settled:=n_settled+1;perform control.nexloop_erasure_owner_residue_audit(p_tenant,e.world,e.consumer_id,e.request_id,'settled');
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('settled',n_settled,'kept_grants_held',n_kept);
end $$;
alter function control.nexloop_erasure_owner_residue_settle(text) owner to nexloop_owner;
revoke all on function control.nexloop_erasure_owner_residue_settle(text) from public;

do $$ declare t text;r jsonb; begin
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  r:=control.nexloop_erasure_owner_residue_settle(t);
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('erasure-owner-residue-run:'||gen_random_uuid(),'nexloop.consumer_erasure.owner_residue_run',t,'migration:erasure_owner_residue',
    'erasure_owner_residue','migration-0447',r||jsonb_build_object('tenant_id',t),clock_timestamp());
  raise notice 'erasure owner residue: tenant % settled % kept %',t,r->>'settled',r->>'kept_grants_held';
 end loop;
end $$;
