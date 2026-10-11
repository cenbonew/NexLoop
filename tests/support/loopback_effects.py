"""Test harness for 0220: NexLoop's own loopback channels are not external effects in tests.

Production declares this through trusted configuration (deploy/configuration/action-effect-categories.v2.json:
external_effect false for nexloop.service.request and nexloop.incentive.issue), applied per tenant by the configurator.
Test fixtures publish those Action definitions directly, in many places and with their own definitions, and dispatch them to
loopback providers. Instead of repeating the declaration in every fixture, the disposable test cluster records the same
declaration for every definition of these two Actions as it is published (world 'real'). It never opens a real dispatch
switch; tests of the switch remove the declaration to make the effect external again (`make_external`).
Only installed by the test PG fixture; never part of a migration.
"""
import psycopg

INTERNAL = ('eios:action:nexloop.service.request:1', 'eios:action:nexloop.incentive.issue:1')

SQL = """
create schema if not exists nexloop_test_harness;
create or replace function nexloop_test_harness.declare_loopback() returns trigger language plpgsql security definer
 set search_path=pg_catalog,pg_temp as $$
begin
 if new.world='real' and new.resource_id = any(array['%s','%s']) and to_regclass('control.nexloop_action_effect_externality') is not null then
  insert into control.nexloop_action_effect_externality(tenant_id,world,resource_id,definition_digest,external_effect,manifest_version)
   values(new.tenant_id,new.world,new.resource_id,control.nexloop_definition_digest(new.definition),false,2) on conflict do nothing;
 end if;
 return new;
end $$;
create or replace function nexloop_test_harness.on_ddl() returns event_trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if to_regclass('control.nexloop_action_definitions') is not null
  and not exists(select 1 from pg_trigger where tgname='nexloop_test_loopback_effects') then
  create trigger nexloop_test_loopback_effects after insert on control.nexloop_action_definitions
   for each row execute function nexloop_test_harness.declare_loopback();
 end if;
end $$;
create event trigger nexloop_test_loopback_effects on ddl_command_end when tag in ('CREATE TABLE') execute function nexloop_test_harness.on_ddl();
""" % INTERNAL


def install(conninfo):
    with psycopg.connect(conninfo, autocommit=True) as db:
        db.execute(SQL)


def make_external(admin, tenant, resource_id, world='real'):
    """For tests of the switch: remove the loopback declaration so the Action is external (as when nothing is declared)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('delete from control.nexloop_action_effect_externality where tenant_id=%s and world=%s and resource_id=%s', (tenant, world, resource_id))
