"""A stand-in for the pgbackrest command line (tests only): `version`, `info --output=json`, `backup --type=T` and `verify` for one
stanza, with the state in a JSON file named by the first argument. The cluster facts (system identifier, LSNs, timeline) come
from the state the test wrote from the real cluster; knobs in the state make backup, verify or the backup's error flag fail."""
import json
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
args = sys.argv[2:]
state = json.loads(state_path.read_text())
stanza = next(a.split('=', 1)[1] for a in args if a.startswith('--stanza='))
repo = int(next((a.split('=', 1)[1] for a in args if a.startswith('--repo=')), '1'))
config = next((a.split('=', 1)[1] for a in args if a.startswith('--config=')), None)
state.setdefault('calls', []).append([a for a in args if not a.startswith('--config=')] + (['--config=<set>'] if config else []))
command = [a for a in args if not a.startswith('--')]
if command == ['version']:
    print(f"pgBackRest {state['version']}")
elif command == ['info']:
    print(json.dumps([{'name': stanza, 'status': {'code': state.get('status_code', 0), 'message': 'ok' if not state.get('status_code') else 'error'},
                       'db': [{'id': 1, 'repo-key': repo, 'system-id': int(state['system_id']), 'version': '18'}],
                       'backup': [b for b in state.get('backups', []) if b['database']['repo-key'] == repo]}]))
elif command == ['backup']:
    if state.get('fail_backup'):
        print('ERROR: [056]: synthetic backup failure', file=sys.stderr)
        state_path.write_text(json.dumps(state))
        sys.exit(56)
    kind = next(a.split('=', 1)[1] for a in args if a.startswith('--type='))
    now = int(time.time())
    n = len(state.get('backups', [])) + 1
    label = time.strftime('%Y%m%d-%H%M%S', time.gmtime(now)) + {'full': 'F', 'diff': 'D', 'incr': 'I'}[kind] + str(n)
    state.setdefault('backups', []).append({
        'label': label, 'type': kind, 'prior': None, 'error': bool(state.get('backup_error')),
        'timestamp': {'start': now, 'stop': now + 1}, 'lsn': {'start': state['lsn_start'], 'stop': state['lsn_stop']},
        'archive': {'start': state['wal_start'], 'stop': state['wal_stop']}, 'database': {'id': 1, 'repo-key': repo},
        'info': {'size': 1000, 'delta': 1000, 'repository': {'size': 400, 'delta': 400}}, 'backrest': {'format': 5, 'version': state['version']}})
elif command == ['verify']:
    rc = state.get('verify_rc', 0)
    print('P00   INFO: verify command end: ' + ('completed successfully' if rc == 0 else f'aborted with exception [{rc}]'))
    state_path.write_text(json.dumps(state))
    sys.exit(rc)
else:
    print('ERROR: unexpected command ' + ' '.join(command), file=sys.stderr)
    sys.exit(2)
state_path.write_text(json.dumps(state))
