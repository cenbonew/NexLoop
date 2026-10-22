"""AT-046's core on the acceptance's own path (stage judgement 2026-10-22, L6: "Twin calls a real message Action -> the server refuses,
even if the model forges mode" was never exercised on stage). at046_stage.py's intake now runs two refusal probes with the world's own
api credential, in the same run chain:
- N1 real_action_refused: a real-world session for the simulation credential (where any real message Action starts) - refused 42501;
- N2 forged_mode_refused: a Run command naming mode 'real' in the simulation world, the client's own check bypassed - refused 42501 by
  the server (0312: before it, only the Python client checked the mode; the server accepted and stored the forged command).
Real PostgreSQL with the simulation roles; and the merge: a probe let through fails the run, a missing probe never passes.
"""
import io
import json
import sys
import uuid
from pathlib import Path

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import at046_stage as A  # noqa: E402

from test_nx041_sim_pi_run_pg import sim_group  # noqa: E402,F401
from test_at046_stage_pg import FakeProbe, REFUSED, config_file, steps  # noqa: E402


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def test_both_probes_are_refused_by_the_server_and_leave_nothing(sim_group, admin, tmp_path):
    f = sim_group
    roles = {'sim_api': {'dsn_file': private(tmp_path / 'api-dsn', make_conninfo(f['pg'], user='nexloop_sim_api')),
                         'token_file': private(tmp_path / 'api-token', f['api_token']), 'signing_key_file': str(f['key']), 'signing_key_id': 'sim-run'}}
    real_rows = lambda: admin.execute("""select count(*) from runtime.jobs where world='real'""").fetchone()[0]  # noqa: E731
    sim_jobs = lambda: admin.execute('select count(*) from runtime.jobs where world=%s', (f['world'],)).fetchone()[0]  # noqa: E731
    before = (real_rows(), sim_jobs(), admin.execute("select count(*) from authz.nexloop_run_credentials where world='real'").fetchone()[0])
    # The twin port role with a twin credential of the world: its server entry asked for world real (the client refuses real first).
    import psycopg
    from nexloop_eios.twin_port import TwinPort
    admin.execute('alter role nexloop_twin_port login')
    twin = TwinPort(lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_twin_port'), autocommit=True), 'T' * 48, f['world'])
    out = A.negatives_from(roles)(f['world'], f['consumer'], 'eios:action:' + A_effect() + ':1', twin)
    assert out['n1']['real_session'] == {'refused': True, 'sqlstate': '42501', 'reason': 'service authentication denied'}, out
    assert out['n1']['twin_bind_real']['refused'] is True and out['n1']['twin_bind_real']['sqlstate'] == '42501', out
    assert out['n2'] == {'refused': True, 'sqlstate': '42501', 'reason': 'activation command denied'}, out
    after = (real_rows(), sim_jobs(), admin.execute("select count(*) from authz.nexloop_run_credentials where world='real'").fetchone()[0])
    assert after == before  # no task queued (the forged command was not accepted), nothing real
    # The client's own check is back in place after the probe (the bypass was scoped to that one call).
    from nexloop_eios import runtime_activation
    assert runtime_activation.world_kind(f['world']) == 'simulation'


def A_effect():
    from nexloop_eios.effect_contexts import EFFECT
    return EFFECT


def _intake(tmp_path, negatives, world):
    out = io.StringIO()
    before = tmp_path / 'before.json'
    if not before.exists():
        o = io.StringIO()
        A.main(['probe-before', '--config', str(config_file(tmp_path, world=world))], ports=A.Ports(probe=FakeProbe(world)), out=o)
        before.write_text(o.getvalue())
    code = A.main(['intake', '--config', str(config_file(tmp_path, world=world)), '--from', str(before)],
                  ports=A.Ports(intake=lambda w, c, e: ('task-1', 'run-1'), negatives=negatives), out=out)
    return code, json.loads(out.getvalue())


def test_a_probe_let_through_fails_the_run_and_a_missing_one_never_passes(tmp_path):
    world = 'sim:' + str(uuid.uuid4())
    code, doc = _intake(tmp_path, REFUSED, world)
    assert steps(doc)['real_action_refused']['passed'] is True and steps(doc)['forged_mode_refused']['passed'] is True

    def allowed_n2(*a):
        out = REFUSED(*a)
        out['n2'] = {'refused': False, 'sqlstate': None, 'reason': 'allowed'}
        return out

    def allowed_n1(*a):
        out = REFUSED(*a)
        out['n1']['twin_bind_real'] = {'refused': False, 'sqlstate': None, 'reason': 'allowed'}
        return out
    for fake, name in ((allowed_n2, 'forged_mode_refused'), (allowed_n1, 'real_action_refused')):
        code, doc = _intake(tmp_path, fake, world)
        assert code == 1 and doc['passed'] is False and steps(doc)[name]['passed'] is False, name
    # Without the probes (no sim_api role) the steps are blocked: the merge can then never be true.
    code, doc = _intake(tmp_path, None, world)
    assert steps(doc)['real_action_refused']['passed'] is None and doc['passed'] is not True
    assert {'real_action_refused', 'forged_mode_refused'} <= set(A.REQUIRED)
