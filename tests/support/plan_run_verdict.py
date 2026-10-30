"""L6 I20: the Host's planRun (apps/agent-host/src/runtime-host.ts) judged on a real command and Context pack, through the built
Host (dist) itself, so a test feeds it what the server actually produced instead of a hand-written fixture."""
import json
import shutil
import subprocess
from pathlib import Path

DIST = Path(__file__).resolve().parents[2] / 'apps/agent-host/dist/runtime-host.js'
SCRIPT = ("import {planRun} from %s;let s='';process.stdin.on('data',c=>s+=c).on('end',()=>{const v=JSON.parse(s);"
          "process.stdout.write(JSON.stringify(planRun(v.command,v.input)));});")


def plan_run(command, text):
    run = subprocess.run([shutil.which('node'), '--input-type=module', '-e', SCRIPT % json.dumps(DIST.as_uri())],
                         input=json.dumps({'command': command, 'input': text}), capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)
