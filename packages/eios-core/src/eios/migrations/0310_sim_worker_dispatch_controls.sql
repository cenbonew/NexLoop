-- NX-041 stage finding 2026-10-22 (AT-046 first stage run): the simulation runtime worker failed every Run at its control stage
-- (runtime_control_unavailable, InsufficientPrivilege). Before a queued Run is activated the worker re-reads the owner controls
-- (nexloop_eios.goal_controls.ControlPlane.prepare_run_dispatch: goal binding, the task's control snapshot, the model budget and its
-- reservation) - entries the real workers hold (nexloop_domain_worker) and S6c never granted to nexloop_sim_worker. The local D10 test
-- called claim and activation directly and so never ran that stage (tests/test_nx041_sim_pi_run_pg.py now runs the worker's own path).
-- The migration number is temporary; it lives only in the file name and the catalog.
-- Least privilege: execute on these four entries only, to the simulation worker only (not its guard, not the intake); no role
-- membership. Each entry authenticates the caller's service credential for the world it names (the world's own worker credential:
-- D10) and works on that world's rows only.
grant execute on function authz.nexloop_assert_task_dispatch_controls(text,text,text) to nexloop_sim_worker;
grant execute on function authz.nexloop_bind_run_goal(text,text,uuid,text,integer) to nexloop_sim_worker;
grant execute on function authz.nexloop_budget_status(text,text,text) to nexloop_sim_worker;
grant execute on function authz.nexloop_reserve_budget(text,text,text,text,text,text,text) to nexloop_sim_worker;
