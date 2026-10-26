"""The DATA_HOST pg_hba scripts end to end on their normal path - the installed pg_hba lacks the script's line(s) and the rendered
template adds them (host_health_hba_data_host.sh, sim_hba_data_host.sh, evolution_hba_data_host.sh). Stage 2026-10-25: under
`set -euo pipefail` the `diff | sed` substitution failed whenever diff found a difference (exit 1), so the run stopped silently in
step 1 exactly on this path (`{"failed":"env exited 1"}`, pg_hba untouched). The scripts run for real here, with stand-ins on PATH
only for what needs root or the container: `id` (root), `stat` (the configuration counted as 0600 root), and `docker` (the
container sees the same pg_hba file; HUP and the reload log succeed); the release checkout's render.py fills the real template.
The test asserts the run reached the write: the lines are in pg_hba, the backup exists and the reload was asked for."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'deploy/stage/config/pg_hba.conf.template'
VALUES = {'__NEXLOOP_DB__': 'nexloop_stage', '__APP_HOST_ADDR__': '192.0.2.10', '__APP_PG_ROLES__': 'nexloop_api,nexloop_domain_worker'}
CASES = {
    'host_health_hba_data_host.sh': ('nexloop_host_recorder', 'pg_hba.conf.before-host-health'),
    'sim_hba_data_host.sh': ('nexloop_sim_worker', 'pg_hba.conf.before-sim'),
    'evolution_hba_data_host.sh': ('nexloop_evo_reader', None),
}

RENDER = '''import argparse,sys
p=argparse.ArgumentParser();p.add_argument('--env');p.add_argument('--template');p.add_argument('--out');a=p.parse_args()
text=open(a.template).read()
for k,v in %r.items():text=text.replace(k,v)
open(a.out,'w').write(text)
''' % VALUES
# stat: the configuration counted as 0600 root ('%a %u'), a directory as root's 0755 ('%u %a', the config/ check).
STAT = "#!/usr/bin/env bash\ncase \"$2\" in '%u %a') echo \"${FAKE_DIR_OWNER_MODE:-0 755}\" ;; *) echo '600 0' ;; esac\n"
DOCKER = '''#!/usr/bin/env bash
# docker compose -p PROJECT <sub> ...: the container's /etc/postgresql/pg_hba.conf is the installed file.
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case " $* " in
  *" exec "*" cat /etc/postgresql/pg_hba.conf"*) cat "$FAKE_HBA" ;;
  *" exec "*" grep -q "*) word=$(printf '%s\\n' "$*" | sed -n 's/.*grep -q \\([^ ]*\\) .*/\\1/p'); grep -q "$word" "$FAKE_HBA" ;;
  *" kill "*) : ;;
  *" logs "*) printf '%s\\n' 'LOG:  received SIGHUP, reloading configuration files' ;;
esac
'''


def rendered():
    text = TEMPLATE.read_text()
    for k, v in VALUES.items():
        text = text.replace(k, v)
    return text


def stand_ins(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in (('id', '#!/usr/bin/env bash\necho 0\n'), ('stat', STAT), ('docker', DOCKER), ('sleep', '#!/usr/bin/env bash\n:\n')):
        (bin_dir / name).write_text(body)
        os.chmod(bin_dir / name, 0o755)
    return bin_dir


@pytest.mark.parametrize('script', sorted(CASES))
def test_the_normal_path_adds_the_lines_and_reloads(tmp_path, script):
    marker, backup = CASES[script]
    root = tmp_path / 'nexloop'
    (root / 'config').mkdir(parents=True)
    hba = root / 'config/pg_hba.conf'
    full = rendered()
    hba.write_text('\n'.join(line for line in full.splitlines() if marker not in line or line.lstrip().startswith('#')) + '\n')
    checkout = tmp_path / 'release'
    (checkout / 'scripts/deploy').mkdir(parents=True)
    (checkout / 'deploy/stage/config').mkdir(parents=True)
    (checkout / 'scripts/deploy/render.py').write_text(RENDER)
    (checkout / 'deploy/stage/config/pg_hba.conf.template').write_text(TEMPLATE.read_text())
    env_file = tmp_path / 'stage.env'
    env_file.write_text('NEXLOOP_DB=nexloop_stage\n')
    config = tmp_path / 'config'
    config.write_text(f'NEXLOOP_ROOT={root}\nSTAGE_ENV={env_file}\nRELEASE_CHECKOUT={checkout}\n')
    os.chmod(config, 0o600)
    log = tmp_path / 'docker.log'
    env = {**os.environ, 'PATH': f'{stand_ins(tmp_path)}:{os.environ["PATH"]}', 'FAKE_HBA': str(hba), 'FAKE_DOCKER_LOG': str(log)}
    done = subprocess.run(['bash', str(ROOT / 'scripts/deploy' / script), str(config)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'step 2' in done.stdout and done.stdout.rstrip().splitlines()[-1].startswith('done:')
    now = hba.read_text()
    assert [line for line in now.splitlines() if marker in line and not line.lstrip().startswith('#')] == \
        [line for line in full.splitlines() if marker in line and not line.lstrip().startswith('#')]
    assert 'kill -s HUP postgres' in log.read_text()
    if backup:
        assert marker not in re.sub(r'(?m)^\s*#.*$', '', (root / 'config' / backup).read_text())
    # Rerun: nothing to add - the already-installed exit, no second reload.
    reloads = log.read_text().count('kill -s HUP')
    again = subprocess.run(['bash', str(ROOT / 'scripts/deploy' / script), str(config)], capture_output=True, text=True, env=env, timeout=60)
    assert again.returncode == 0 and ('already installed' in again.stdout or 'nothing to do' in again.stdout), again.stdout + again.stderr
    assert log.read_text().count('kill -s HUP') == reloads


def prepared(tmp_path):
    """The host health normal path's files (the installed pg_hba without its lines) and the environment of the stand-ins."""
    root = tmp_path / 'nexloop'
    (root / 'config').mkdir(parents=True)
    hba = root / 'config/pg_hba.conf'
    hba.write_text('\n'.join(l for l in rendered().splitlines() if 'nexloop_host_recorder' not in l or l.lstrip().startswith('#')) + '\n')
    checkout = tmp_path / 'release'
    (checkout / 'scripts/deploy').mkdir(parents=True)
    (checkout / 'deploy/stage/config').mkdir(parents=True)
    (checkout / 'scripts/deploy/render.py').write_text(RENDER)
    (checkout / 'deploy/stage/config/pg_hba.conf.template').write_text(TEMPLATE.read_text())
    (tmp_path / 'stage.env').write_text('NEXLOOP_DB=nexloop_stage\n')
    config = tmp_path / 'config'
    config.write_text(f'NEXLOOP_ROOT={root}\nSTAGE_ENV={tmp_path / "stage.env"}\nRELEASE_CHECKOUT={checkout}\n')
    os.chmod(config, 0o600)
    env = {**os.environ, 'PATH': f'{stand_ins(tmp_path)}:{os.environ["PATH"]}', 'FAKE_HBA': str(hba), 'FAKE_DOCKER_LOG': str(tmp_path / 'docker.log')}
    return hba, config, env


def test_host_health_hba_refuses_a_linked_backup_and_a_writable_config_directory(tmp_path):
    """L6 file-backup review I2: cp -p into config/ writes through a link put there; config/ must be root's alone."""
    script = str(ROOT / 'scripts/deploy/host_health_hba_data_host.sh')
    hba, config, env = prepared(tmp_path)
    before = hba.read_text()
    target = tmp_path / 'target'
    target.write_text('not pg_hba\n')
    os.symlink(target, hba.parent / 'pg_hba.conf.before-host-health')
    done = subprocess.run(['bash', script, str(config)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 1 and 'before-host-health is a symbolic link' in done.stderr
    assert target.read_text() == 'not pg_hba\n' and hba.read_text() == before
    os.unlink(hba.parent / 'pg_hba.conf.before-host-health')
    os.rename(hba, tmp_path / 'pg_hba.real')
    os.symlink(tmp_path / 'pg_hba.real', hba)
    done = subprocess.run(['bash', script, str(config)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 1 and 'pg_hba.conf is a symbolic link' in done.stderr
    os.unlink(hba)
    os.rename(tmp_path / 'pg_hba.real', hba)
    for owner_mode in ('0 777', '0 775', '1000 755'):
        done = subprocess.run(['bash', script, str(config)], capture_output=True, text=True, env={**env, 'FAKE_DIR_OWNER_MODE': owner_mode}, timeout=60)
        assert done.returncode == 1 and "config/ must be root's" in done.stderr and hba.read_text() == before, owner_mode
    real = hba.parent.parent / 'config-real'
    os.rename(hba.parent, real)
    os.symlink(real, hba.parent)
    done = subprocess.run(['bash', script, str(config)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 1 and 'config/: a directory, not a link' in done.stderr
