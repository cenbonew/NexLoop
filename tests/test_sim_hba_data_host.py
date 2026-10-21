"""scripts/deploy/sim_hba_data_host.sh (simulation stage enablement): bash syntax; the template differs from the one before it by the
simulation hostssl line, which its pattern accepts (alone or with the evolution local line, one owner run); anything else added is
refused; the private configuration is read, never sourced; pg_hba is rewritten in place and reloaded. Nothing here runs as root."""
import os
import re
import subprocess
from pathlib import Path

from test_deploy_render import env  # noqa: F401
from nexloop_eios import stage_render as render  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy/sim_hba_data_host.sh'
TEMPLATE = ROOT / 'deploy/stage/config/pg_hba.conf.template'
SIM = 'hostssl nexloop_stage nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api,nexloop_stand_in_worker,nexloop_sim_files 192.0.2.10/32 scram-sha-256'
EVO = 'local nexloop_stage nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator scram-sha-256'


def lib(snippet, *args):
    return subprocess.run(['bash', '-c', f'set -euo pipefail; SIM_HBA_LIB=1 source "$0"; {snippet}', str(SCRIPT), *args], capture_output=True, text=True)


def rendered(text):
    return text.replace('__NEXLOOP_DB__', 'nexloop_stage').replace('__APP_HOST_ADDR__', '192.0.2.10')


def test_the_script_is_valid_bash_and_stops_without_root_or_config():
    assert subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True).returncode == 0
    text = SCRIPT.read_text()
    assert 'set -euo pipefail' in text and 'pg_hba.conf.before-sim' in text
    assert not re.search(r'(^|[;&|(]\s*)sudo\b', '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#')), re.M)
    assert 'PGPASSWORD' not in text and 'read -s' not in text
    assert subprocess.run(['bash', str(SCRIPT)], capture_output=True).returncode != 0
    if os.geteuid() != 0:
        done = subprocess.run(['bash', str(SCRIPT), '/nonexistent'], capture_output=True, text=True)
        assert done.returncode != 0 and 'run with sudo' in done.stderr


def added(tmp_path, before, now):
    (tmp_path / 'now').write_text(rendered(now))
    (tmp_path / 'before').write_text(rendered(before))
    done = lib('diff <(rules "$1") <(rules "$2") | sed -n "s/^> //p"', str(tmp_path / 'before'), str(tmp_path / 'now'))
    return [l for l in done.stdout.splitlines() if l]


def test_the_template_adds_the_simulation_line_alone_or_with_the_evolution_line(tmp_path):
    now = TEMPLATE.read_text()
    drop = lambda text, marker: '\n'.join(l for l in text.splitlines() if marker not in l or l.lstrip().startswith('#')) + '\n'  # noqa: E731
    assert added(tmp_path, drop(now, 'nexloop_sim_worker'), now) == [SIM]
    assert added(tmp_path, drop(drop(now, 'nexloop_sim_worker'), 'nexloop_evo_'), now) == [EVO, SIM]
    for line, ok in ((SIM, True), (SIM.replace('192.0.2.10/32', '0.0.0.0/0'), False), (SIM.replace('scram-sha-256', 'trust'), False),
                     (SIM.replace('hostssl', 'host'), False), (SIM.replace('', ',nexloop_api'), False)):
        assert (lib('printf "%s\\n" "$1" | grep -Eq "$SIM"', line).returncode == 0) is ok, line


def test_the_rendered_rules_admit_the_simulation_roles_from_app_host_only(env):  # noqa: F811
    values = render.load_env(env)
    rules, _ = render.render(TEMPLATE.read_text(), values)
    lines = [' '.join(l.split()) for l in rules.splitlines() if l.strip() and not l.lstrip().startswith('#')]
    sim = [l for l in lines if 'nexloop_sim_' in l]
    assert sim == [f"hostssl {values['NEXLOOP_DB']} nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api,nexloop_stand_in_worker,nexloop_sim_files "
                   f"{values['APP_HOST_ADDR']}/32 scram-sha-256"]
    assert not any(r.startswith(('nexloop_sim_', 'nexloop_stand_in', 'nexloop_twin')) for r in values['APP_PG_ROLES'].split(','))
    assert lines.index(sim[0]) < len(lines) - 4 and all('reject' in l for l in lines[-4:])


def test_the_added_rules_hold_the_simulation_line_exactly_once():
    """Review 1.2: two copies of the simulation line (or a stray rule beside it) are refused, not counted as the two allowed lines."""
    check = lambda *rules: lib('check_added "$1"', '\n'.join(rules)).returncode == 0  # noqa: E731
    assert check(SIM) and check(EVO, SIM) and check(SIM, EVO)
    assert not check(SIM, SIM) and not check(EVO, EVO) and not check(EVO) and not check(SIM, 'local all all trust') and not check(SIM, EVO, EVO)
