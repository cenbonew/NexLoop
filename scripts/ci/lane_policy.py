#!/usr/bin/env python3
"""NX-034 release CI on the existing CI lane: derive the lane's policy instance from the trusted `.ci-policy`.

The controller (nexloop_ci.py run-once) judges a candidate with a trusted policy directory. On the existing CI lane, without
the rootless Podman runner of NX-033 §3, the stage list of `.ci-policy/stages.v<N>.json` cannot be used as it is:
  * its `isolation-probe` stage runs `/trusted/...`, a path that exists only inside the runner; the lane's current suite
    (scripts/ci/check) does not run it either;
  * the controller passes a stage only PATH, HOME, LANG, LC_ALL and TMPDIR, so the lane's non-secret settings (for example
    NEXLOOP_TEST_PG_BIN) must be given to each stage explicitly.
This tool writes a new policy directory: the stage file of the selected version without the named stages and with the given
environment added to every stage, and the required-suites file that version uses, unchanged. The judgement (required suites,
counts, deadline gate, policy_version) is the trusted one; the derivation is recorded in the stage file's `decision` and in
`lane.json`, and its digest changes the result's policy_digest. Environment names that look like secrets are refused.

    lane_policy.py --source <TRUSTED_CI_DIR>/.ci-policy --stages-version 4 --without isolation-probe \
                   --stage-env <PRIVATE_LANE_ENV_FILE> --out <LANE_POLICY_DIR>

Prints one JSON object; exit 0 written, 2 refused (input or environment error). Standard library only.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nexloop_ci import SECRET_ENV  # noqa: E402

_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,63}')


class LaneError(Exception):
    pass


def read_env(path):
    """KEY=VALUE lines (blank lines and # comments ignored); names upper-case; no secret-looking name, no duplicate."""
    env = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not _NAME.fullmatch(key) or key in env:
            raise LaneError(f'env line {number}: KEY=VALUE with an upper-case unique name')
        if SECRET_ENV.search(key):
            raise LaneError(f'env line {number}: {key} looks like a secret; the lane policy carries no secret')
        if key in ('PATH', 'HOME', 'GIT_ALLOW_PROTOCOL', 'GIT_TERMINAL_PROMPT'):
            raise LaneError(f'env line {number}: {key} is set by the controller')
        env[key] = value
    return env


def derive(source, stages_version, without, env, out):
    source, out = Path(source), Path(out)
    if out.exists():
        raise LaneError('output directory exists (a policy instance is written once)')
    stages_file = source / f'stages.v{stages_version}.json'
    suites_name = 'required-suites.v2.json' if stages_version >= 3 else 'required-suites.v1.json'
    stages = json.loads(stages_file.read_text())
    if stages.get('schema') != 'nexloop-ci-stages/1' or stages.get('version') != stages_version:
        raise LaneError('source stages file invalid')
    names = [s['name'] for s in stages['stages']]
    missing = sorted(set(without) - set(names))
    if missing:
        raise LaneError('unknown stage: ' + ','.join(missing))
    kept = []
    for stage in stages['stages']:
        if stage['name'] in without:
            continue
        stage = dict(stage)
        if env:
            stage['env'] = {**(stage.get('env') or {}), **env}
        kept.append(stage)
    source_digest = hashlib.sha256(stages_file.read_bytes()).hexdigest()
    lane = {'schema': 'nexloop-ci-lane-policy/1', 'source_stages': stages_file.name, 'source_stages_sha256': source_digest,
            'without': sorted(without), 'env_names': sorted(env)}
    stages = {**stages, 'stages': kept,
              'decision': stages.get('decision', '') + f' Lane instance (NX-034 release CI on the existing lane): derived from {stages_file.name} '
                          f'sha256 {source_digest}, without {",".join(sorted(without)) or "-"}, stage env {",".join(sorted(env)) or "-"}.'}
    out.mkdir(parents=True)
    (out / stages_file.name).write_text(json.dumps(stages, indent=1, sort_keys=True) + '\n')
    (out / suites_name).write_bytes((source / suites_name).read_bytes())
    (out / 'lane.json').write_text(json.dumps(lane, indent=1, sort_keys=True) + '\n')
    return {'written': True, 'out': str(out), 'stages': [s['name'] for s in kept], **lane}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--stages-version', type=int, choices=(1, 2, 3, 4), required=True)
    parser.add_argument('--without', action='append', default=[])
    parser.add_argument('--stage-env', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        out = derive(args.source, args.stages_version, set(args.without), read_env(args.stage_env) if args.stage_env else {}, args.out)
    except (LaneError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'written': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, LaneError) else None}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
