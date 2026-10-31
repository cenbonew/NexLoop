-- S77 (L6 conditional PASS 2026-10-31; temporary number 0389): the governed incentive catalog Actions (IncentiveOffering.create:1,
-- ConsumerIncentiveOffering.create:1, IncentiveOffering.edit:1, ConsumerIncentiveOffering.edit:1; system-actions.v1.json) write
-- through the generic object Actions, whose input does not restrict fields. This guard is the server-side rule (L6 C1/C5):
-- 1. Only world 'real' (owner Q2): test and simulation worlds hold no incentive catalog.
-- 2. Create: exactly the shape the 0145 admission accepts. An IncentiveOffering is active, fixed_amount, bound_consumer, with
--    amounts in minor units (per-Consumer period limit >= single-issue maximum) and a future UTC valid_until. A binding is active,
--    names an active IncentiveOffering of the same tenant and world at its current revision, a Consumer that exists and is not
--    being erased, and a non-empty source_principal.
-- 3. Edit: only a withdrawal - active true -> false, every other property unchanged (owner Q4/Q7: no re-activation, no new
--    valid_until, no amount, consumer_id, offering_id or offering_revision change). Identity is immutable.
-- Deletion is not guarded here: the consumer erasure (0138 references phase) deletes an erased Consumer's bindings, as before.
create function ontology.nexloop_incentive_catalog_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);p jsonb:=new.properties;o ontology.objects;
begin
 if new.type_name not in ('IncentiveOffering','ConsumerIncentiveOffering')
  and (tg_op='INSERT' or old.type_name not in ('IncentiveOffering','ConsumerIncentiveOffering')) then return new;end if;
 if tg_op='UPDATE' and (old.type_name<>new.type_name or old.object_id<>new.object_id or old.tenant_id<>new.tenant_id or old.world<>new.world) then
  raise exception 'incentive catalog identity is immutable' using errcode='42501';end if;
 if new.world<>'real' then raise exception 'incentive catalog: real world only' using errcode='42501';end if;
 if tg_op='UPDATE' then
  if old.properties->'active' is distinct from 'true'::jsonb or p is distinct from old.properties||'{"active":false}'::jsonb then
   raise exception 'incentive catalog: only a withdrawal (active true to false) may change it' using errcode='23514';end if;
  return new;
 end if;
 if new.type_name='IncentiveOffering' then
  if (select array_agg(k order by k) from jsonb_object_keys(p) k) is distinct from
    array['active','currency','eligibility','incentive_code','kind','max_amount_minor','per_consumer_period_limit_minor','title','valid_until']
   or p->'active' is distinct from 'true'::jsonb or p->>'kind' is distinct from 'fixed_amount' or p->>'eligibility' is distinct from 'bound_consumer'
   or jsonb_typeof(p->'incentive_code') is distinct from 'string' or p->>'incentive_code'!~'^[a-z0-9][a-z0-9._-]{0,63}$'
   or jsonb_typeof(p->'currency') is distinct from 'string' or p->>'currency'!~'^[A-Z]{3}$'
   or jsonb_typeof(p->'max_amount_minor') is distinct from 'string' or p->>'max_amount_minor'!~'^[1-9][0-9]{0,15}$'
   or jsonb_typeof(p->'per_consumer_period_limit_minor') is distinct from 'string' or p->>'per_consumer_period_limit_minor'!~'^[1-9][0-9]{0,15}$'
   or (p->>'per_consumer_period_limit_minor')::bigint<(p->>'max_amount_minor')::bigint
   or jsonb_typeof(p->'title') is distinct from 'string' or length(p->>'title') not between 1 and 256
   or jsonb_typeof(p->'valid_until') is distinct from 'string'
   or p->>'valid_until'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|\+00:00)$' then
   raise exception 'incentive catalog: offering shape' using errcode='23514';end if;
  if (p->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'incentive catalog: offering already expired' using errcode='23514';end if;
  return new;
 end if;
 if (select array_agg(k order by k) from jsonb_object_keys(p) k) is distinct from array['active','consumer_id','offering_id','offering_revision','source_principal']
  or p->'active' is distinct from 'true'::jsonb or jsonb_typeof(p->'offering_revision') is distinct from 'number'
  or jsonb_typeof(p->'consumer_id') is distinct from 'string' or p->>'consumer_id'!~'^[a-f0-9]{64}$'
  or jsonb_typeof(p->'offering_id') is distinct from 'string' or p->>'offering_id'!~'^[a-f0-9]{64}$'
  or jsonb_typeof(p->'source_principal') is distinct from 'string' or length(p->>'source_principal') not between 1 and 256 then
  raise exception 'incentive catalog: binding shape' using errcode='23514';end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into o from ontology.objects x where x.tenant_id=new.tenant_id and x.world=new.world and x.type_name='IncentiveOffering' and x.object_id=p->>'offering_id';
 if not found or o.properties->'active' is distinct from 'true'::jsonb or (p->>'offering_revision')::numeric is distinct from o.nexloop_revision::numeric then
  raise exception 'incentive catalog: binding names no active offering at its current revision' using errcode='23514';end if;
 if not exists(select 1 from ontology.objects x where x.tenant_id=new.tenant_id and x.world=new.world and x.type_name='Consumer' and x.object_id=p->>'consumer_id')
  or runtime.nexloop_consumer_erasing(new.tenant_id,new.world,p->>'consumer_id') then
  raise exception 'incentive catalog: binding names no current Consumer' using errcode='23514';end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function ontology.nexloop_incentive_catalog_guard() owner to nexloop_owner;
revoke all on function ontology.nexloop_incentive_catalog_guard() from public;
create trigger nexloop_incentive_catalog_guard before insert or update on ontology.objects
 for each row execute function ontology.nexloop_incentive_catalog_guard();
