"""scripts/deploy/evolution_hba_data_host.sh (evolution stage enrollment): bash syntax; the rule it relies on - the template differs
from the one before it by exactly the evolution local line, which its pattern accepts; the private configuration is read, never
sourced (unknown keys, relative paths, shell characters and a readable file are refused); pg_hba is rewritten in place and reloaded,
never replaced. Nothing here runs as root or touches a host."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy/evolution_hba_data_host.sh'
TEMPLATE = ROOT / 'deploy/stage/config/pg_hba.conf.template'


def lib(snippet, *args):
    """Run a snippet with the script's functions loaded (EVOLUTION_HBA_LIB=1 stops it before any step)."""
    return subprocess.run(['bash', '-c', f'set -euo pipefail; EVOLUTION_HBA_LIB=1 source "$0"; {snippet}', str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_the_script_is_valid_bash_and_stops_without_root_or_config():
    assert subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True).returncode == 0
    text = SCRIPT.read_text()
    assert 'set -euo pipefail' in text and 'pg_hba.conf.before-evolution' in text
    # The script itself never calls sudo (the owner runs it with sudo once) and never reads a password.
    assert not re.search(r'(^|[;&|(]\s*)sudo\b', '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#')), re.M)
    assert 'PGPASSWORD' not in text and 'read -s' not in text
    assert subprocess.run(['bash', str(SCRIPT)], capture_output=True).returncode != 0  # no config
    if os.geteuid() != 0:
        done = subprocess.run(['bash', str(SCRIPT), '/nonexistent'], capture_output=True, text=True)
        assert done.returncode != 0 and 'run with sudo' in done.stderr


def test_the_template_adds_exactly_the_evolution_line_and_the_pattern_accepts_only_it(tmp_path):
    now = TEMPLATE.read_text()
    before = '\n'.join(l for l in now.splitlines() if 'nexloop_evo_' not in l or l.lstrip().startswith('#')) + '\n'
    (tmp_path / 'now').write_text(now.replace('__NEXLOOP_DB__', 'nexloop_stage'))
    (tmp_path / 'before').write_text(before.replace('__NEXLOOP_DB__', 'nexloop_stage'))
    done = lib('diff <(rules "$1") <(rules "$2") | sed -n "s/^> //p"', str(tmp_path / 'before'), str(tmp_path / 'now'))
    added = [l for l in done.stdout.splitlines() if l]
    assert added == ['local nexloop_stage nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator scram-sha-256']
    for line, ok in ((added[0], True), ('local all nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator scram-sha-256', True),
                     ('hostssl nexloop_stage nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator 0.0.0.0/0 scram-sha-256', False),
                     ('local nexloop_stage nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator trust', False),
                     ('local nexloop_stage all scram-sha-256', False)):
        assert (lib('printf "%s\\n" "$1" | grep -Eq "$EVOLUTION"', line).returncode == 0) is ok, line


def test_the_configuration_is_read_never_sourced(tmp_path):
    good = tmp_path / 'good.conf'
    good.write_text('NEXLOOP_ROOT=/srv/nexloop\nSTAGE_ENV=/srv/private/stage.env\n# comment\nRELEASE_CHECKOUT=/srv/release/checkout\n')
    good.chmod(0o600)
    done = lib('read_config "$1"; printf "%s|%s|%s" "$NEXLOOP_ROOT" "$STAGE_ENV" "$RELEASE_CHECKOUT"', str(good))
    assert done.returncode == 0 and done.stdout == '/srv/nexloop|/srv/private/stage.env|/srv/release/checkout', done.stderr
    marker = tmp_path / 'executed'
    for body in (f'NEXLOOP_ROOT=$(touch {marker})\nSTAGE_ENV=/a\nRELEASE_CHECKOUT=/b\n', 'NEXLOOP_ROOT=relative\nSTAGE_ENV=/a\nRELEASE_CHECKOUT=/b\n',
                 'NEXLOOP_ROOT=/a\nSTAGE_ENV=/a\nRELEASE_CHECKOUT=/b\nPATH=/tmp\n', 'NEXLOOP_ROOT=/a/../etc\nSTAGE_ENV=/a\nRELEASE_CHECKOUT=/b\n',
                 'NEXLOOP_ROOT=/a\nSTAGE_ENV=/a\n'):
        bad = tmp_path / 'bad.conf'
        bad.write_text(body)
        bad.chmod(0o600)
        assert lib('read_config "$1"', str(bad)).returncode != 0, body
    assert not marker.exists()
    good.chmod(0o644)
    assert lib('read_config "$1"', str(good)).returncode != 0


def test_step_2_rewrites_in_place_checks_the_container_view_and_never_replaces_the_file():
    text = SCRIPT.read_text()
    step2 = text[text.index('step 2 '):]
    assert 'install -o' not in step2 and 'mv ' not in step2 and 'restart postgres' in step2
    assert 'write_in_place "$WORK/pg_hba.conf" "$HBA"' in step2 and 'write_in_place "$HBA.before-evolution" "$HBA"' in step2
    assert 'exec -T postgres cat /etc/postgresql/pg_hba.conf' in step2 and 'kill -s HUP postgres' in step2
