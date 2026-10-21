-- NX-043 report leave-out aligned with X4 (L6 review of bb876cb5, dispatcher 2026-10-21): "the same comparison" also means the same
-- world, as X4's in_same_comparison does - a Consumer id met in an experiment of another world is never cross-allocated here. The
-- migration number lives only in the file name and the catalog. runtime.nexloop_experiment_leave_out is 0291's body with that one
-- condition (registered in tests/test_function_copy_guard.py).

create or replace function runtime.nexloop_experiment_leave_out(p_tenant text,p_experiment text,p_consumer text,p_arm text) returns text
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select case when p_consumer like 'pseudonym:%' then 'erased'
  when exists(select 1 from control.nexloop_experiments e join control.nexloop_experiments o on o.tenant_id=e.tenant_id and o.experiment_id<>e.experiment_id
     and o.registration->'candidate'=e.registration->'candidate' and o.registration->'control'=e.registration->'control'
     and o.registration->>'world'=e.registration->>'world'  -- the same comparison is in the same world (as X4's in_same_comparison)
    join control.nexloop_experiment_allocations a on a.tenant_id=o.tenant_id and a.experiment_id=o.experiment_id and a.consumer_id=p_consumer
     and a.arm is not null and a.arm<>p_arm
    where e.tenant_id=p_tenant and e.experiment_id=p_experiment) then 'cross_allocated' end
$$;
