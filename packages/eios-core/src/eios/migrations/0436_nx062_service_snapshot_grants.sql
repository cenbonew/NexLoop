-- NX-062 M2a fix (found in M3, 2026-11-09): 0434 renamed authz.nexloop_service_identity_snapshot to ..._before_machine and created the
-- machine-excluding wrapper with grants for nexloop_api, nexloop_domain_worker, nexloop_action_worker and nexloop_scheduler only. Every
-- other role that held EXECUTE on the old function - the NX-041 simulation roles nexloop_sim_api, nexloop_sim_worker and
-- nexloop_sim_guard (0268, granted function by function) - kept it on the renamed inner function and lost it on the name every entry
-- calls, so a simulation world's service authentication was refused. This migration gives the wrapper exactly the EXECUTE grantees of the
-- inner function (its owner aside) and takes the inner function from them: afterwards nobody but the owner calls the inner function, and
-- every role that called the snapshot before 0434 calls it again. The migration number is temporary; it lives only in the file name and
-- the catalog.
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='authz.nexloop_service_identity_snapshot_before_machine(text,text)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function authz.nexloop_service_identity_snapshot(text,text) to %I',g);
  execute format('revoke execute on function authz.nexloop_service_identity_snapshot_before_machine(text,text) from %I',g);
 end loop;
end $$;
