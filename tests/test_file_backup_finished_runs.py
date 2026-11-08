"""2026-11-08 (stage files_runtime, L4 docs/implementation/NX-035-file-backup-runtime-wal.md): the file backup copies a Run's
runtime.sqlite only when no non-empty WAL is beside it, and the Agent Host kept every finished Run's storage open until purge or
exit. End to end with the real Pi adapter in a live Node process (apps/agent-host/test/finished-run-process.ts) and the real
file_backup.stage_runs: while the process holds the settled Run open the backup skips it as not checkpointed; after the inspect
that sees it finished - the process still running, as a Host does - the backup stages it with an intact database. Needs Node 24
and the built agent-host dist (npm run build)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from nexloop_eios.file_backup import stage_runs

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'apps/agent-host'


def node():
    found = shutil.which('node')
    assert found and subprocess.run([found, '--version'], capture_output=True, text=True).stdout.startswith('v24.'), 'Node 24 required'
    assert (HOST / 'dist/pi-runtime-adapter.js').is_file(), 'built agent-host dist required (npm run build)'
    return found


def command():
    """The deterministic test command of apps/agent-host/test/pi-runtime-support.ts, built here."""
    out = subprocess.run([node(), '--experimental-strip-types', '-e',
                          "import('./test/pi-runtime-support.ts').then(m=>process.stdout.write(JSON.stringify(m.command())))"],
                         cwd=HOST, capture_output=True, text=True, timeout=60, check=True).stdout
    return json.loads(out)


def read_line(process):
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return line.strip()


def test_a_finished_run_is_backed_up_while_the_host_keeps_running(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    cmd = command()
    process = subprocess.Popen([node(), '--experimental-strip-types', str(HOST / 'test/finished-run-process.ts')], cwd=HOST,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={'PATH': os.environ.get('PATH', '')})
    try:
        process.stdin.write(json.dumps({'root': str(runtime), 'command': cmd}) + '\n')
        process.stdin.flush()
        assert read_line(process) == 'settled'
        entries = [{'ref': cmd['run_id']}]
        # Settled but still open in the Host: its WAL is not empty, the backup skips it (the stage failure).
        rows, skipped, absent = stage_runs(entries, runtime, tmp_path / 'before')
        assert (rows, skipped['not_checkpointed'], absent) == ([], 1, 0)
        process.stdin.write('inspect\n')
        process.stdin.flush()
        assert read_line(process) == 'inspected succeeded'
        assert process.poll() is None  # the Host process keeps running
        rows, skipped, absent = stage_runs(entries, runtime, tmp_path / 'after')
        assert [r[0] for r in rows] == [f"runs/{cmd['run_id']}/runtime.sqlite"] and skipped == {'missing': 0, 'not_checkpointed': 0, 'integrity': 0}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
