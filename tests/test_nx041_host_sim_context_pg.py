"""NX-041 Host simulation context protocol (S7b; L6 nx041-s7b-review 3.4), the Python and database side.

- A simulation Run's input is S7b's pack from authz.nexloop_sim_context_command with that world's own credential; a real world
  is refused before any call, and nothing here ever reads or falls back to v6.
- End to end across the languages: the pack the database assembles is parsed and rendered by this checkout's built Agent Host
  module (Node 24): every line is marked simulated, goal, supply and relationship are explicitly empty, and a v6 pack or the pack
  of another world is refused by the same parser.
- The database side of the simulation event intake's role: applied only where S6c's authz.nexloop_app_role exists.
"""
import json
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest

from nexloop_eios.sim_context import SIM_CONTEXT_PROTOCOL, assemble_sim_context
from test_nx041_s7_pg import consumers, digest, port, s7, s7_raw  # noqa: F401
from nexloop_eios import consumer_port as P

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'apps/agent-host/dist/sim-context.js'


def node_render(pack_text, command):
    node = shutil.which('node')
    assert node and subprocess.run([node, '--version'], check=True, capture_output=True, text=True).stdout.startswith('v24.'), 'Node 24 required'
    assert DIST.exists(), 'build the Agent Host (pnpm -C apps/agent-host build) first'
    script = ("import {parseSimContextPack,renderSimContextPack} from %s;"
              "let input='';process.stdin.on('data',d=>input+=d).on('end',()=>{const [pack,command]=JSON.parse(input);"
              "try{process.stdout.write(JSON.stringify({ok:renderSimContextPack(parseSimContextPack(pack,command))}));}"
              "catch(error){process.stdout.write(JSON.stringify({error:error.code??String(error)}));}});") % json.dumps(DIST.as_uri())
    out = subprocess.run([node, '--input-type=module', '-e', script], input=json.dumps([pack_text, command]), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout)


def command(world, tenant, mode='simulation', consumer='c' * 64):
    return {'schema_version': '1.0', 'run_id': '11111111-1111-4111-8111-111111111111', 'tenant_id': tenant, 'world_id': world, 'mode': mode,
            'request_id': 'synthetic-sim-context-1', 'trigger_event_id': '22222222-2222-4222-8222-222222222222', 'role_ref': 'role:sim',
            'consumer_ref': 'consumer:' + consumer, 'goal_version_ref': 'goal:sim-v1', 'context_manifest_ref': 'artifact:sim', 'runtime_profile': 'deterministic-test',
            'credential_ref': 'run_credential:sim', 'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0',
                                                                'currency': 'USD'}, 'not_after': '2099-01-01T00:00:00Z', 'runtime_owner_epoch': 1}


def test_a_simulation_run_input_is_the_sim_pack_and_the_host_renders_it_marked_simulated(s7_raw):
    f = s7_raw
    c = consumers(f['admin'], f['a'])[0]
    twin = P.TwinStub(seed='host-ctx')
    for step in range(1, 4):
        port(f).act(twin.act(c, step))
    with f['api']() as db:
        text = assemble_sim_context(db, digest(f['token_a']), f['a'], c, limit=2)
    pack = json.loads(text)
    assert pack['schema_version'] == SIM_CONTEXT_PROTOCOL and pack['world'] == f['a'] and pack['data_mode'] == 'simulation'
    rendered = node_render(text, command(f['a'], pack['tenant_id'], consumer=c))['ok']
    lines = rendered.split('\n')
    assert lines[0].startswith('[模拟环境 SIMULATION]') and 'source: simulated' in lines[0]
    assert all(line.startswith('[模拟][source: simulated] ') for line in lines[1:-1]) and lines[-1].startswith('[模拟环境 SIMULATION 结束]')
    for section in ('goal', 'supply', 'relationship'):
        assert any(line.startswith(f'[模拟][source: simulated] {section}: 空') for line in lines), section
    assert sum(line.startswith('[模拟][source: simulated] simulated_conversation #') for line in lines) == 2
    assert any('older_events_omitted' in line for line in lines)
    # The same parser refuses the pack for another world, for a real-mode command, and any v6 pack (no fallback).
    assert node_render(text, command(f['b'], pack['tenant_id'], consumer=c))['error'] == 'runtime_context_invalid'
    assert node_render(text, command('real', pack['tenant_id'], 'real', consumer=c))['error'] == 'runtime_context_invalid'
    # L6 host-sim-context 4.1: the pack of this world given to a Run of another Consumer is refused.
    other = consumers(f['admin'], f['a'])[1]
    assert node_render(text, command(f['a'], pack['tenant_id'], consumer=other))['error'] == 'runtime_context_invalid'
    v6 = json.dumps({'schema_version': 'nexloop.context-pack.v6', 'bindings': {}, 'user_statement': {}})
    assert node_render(v6, command(f['a'], pack['tenant_id']))['error'] == 'runtime_context_invalid'


def test_the_assembler_refuses_real_and_test_before_any_call(s7_raw):
    f = s7_raw

    class Refuse:
        def execute(self, *args):
            raise AssertionError('no database call for a real or test world')
    for world in ('real', 'test', 'simulation'):
        with pytest.raises(ValueError, match='simulation world required'):
            assemble_sim_context(Refuse(), digest(f['token_real']), world, '0' * 64)
    with pytest.raises(ValueError, match='limit'):
        assemble_sim_context(Refuse(), digest(f['token_a']), f['a'], '0' * 64, limit=0)


def test_the_event_intake_entry_reads_app_role_and_the_migration_needs_s6c(s7_raw):
    """Integration (L6): the migration is unconditional - the entry's role check reads app_role and nexloop_sim_api may execute it;
    without S6c's interface the migration fails instead of leaving the same revision with another schema."""
    admin = s7_raw['admin']
    migration = next((ROOT / 'packages/eios-core/src/eios/migrations').glob('*_nx041_host_sim_context.sql')).read_text()
    assert 'to_regprocedure' in migration and 'is null then return' not in migration and 'requires S6c' in migration
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match='requires S6c'), admin.transaction(force_rollback=True):
        admin.execute('alter function authz.nexloop_app_role(text) rename to nexloop_app_role_hidden')
        admin.execute(migration)
    src = admin.execute("select prosrc from pg_proc where oid='authz.nexloop_sim_context_command(text,text,text,integer)'::regprocedure").fetchone()[0]
    # NX-042 T3 admits the port role (its owner-run observe entry) and nothing else besides app_role.
    assert 'authz.nexloop_app_role(p_world)' in src
    assert src.replace(" and session_user is distinct from 'nexloop_twin_port'", '', 1).count('session_user') == 0
    # Least privilege (0270, L6 sim-context-ref review): the simulation intake registers by reference only; it no longer executes
    # the assembler directly (authz.nexloop_sim_context_register calls it as its owner, the role check still reading app_role).
    assert not admin.execute("select has_function_privilege('nexloop_sim_api','authz.nexloop_sim_context_command(text,text,text,integer)','execute')").fetchone()[0]
    assert admin.execute("select has_function_privilege('nexloop_sim_api','authz.nexloop_sim_context_register(text,text,uuid,text,integer)','execute')").fetchone()[0]
    c = consumers(admin, s7_raw['a'])[0]
    with s7_raw['role']('nexloop_sim_api')() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_sim_context_command(%s,%s,%s,5)', (digest(s7_raw['token_a']), s7_raw['a'], c))
    with s7_raw['api']() as db:  # the real role still works exactly as before
        assert db.execute('select authz.nexloop_sim_context_command(%s,%s,%s,5)', (digest(s7_raw['token_a']), s7_raw['a'], c)).fetchone()[0]['data_mode'] == 'simulation'
