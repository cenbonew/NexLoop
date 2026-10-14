"""NX-037 real-mode start guards of the controlled interaction run entry (run_interactions.py), checked before a real trial starts
or resumes. Each returns refusal codes (empty when the guard holds); no guard writes anything outside the trial directory, and
none prints a name, a path, a credential or a body.

- D3 (owner 2026-10-14, ROADMAP §7: the customers are AI agents the owner operates through the browser; the system is real):
  the owner's own one-time operator authorization (a private file; its sha256 in the trial configuration's
  operator_authorization, --operator-authorization-file) else authorization_missing; every consumer of the interaction plan a
  participant of kind agent (the tool, never a credential); kind human only for the owner in the staff role (the owner is the
  only staff member), never as a consumer; else consent_missing. Only consumer ids, kinds and personas reach the trial
  directory (consent.json) and the evidence pack.
- trial window: the trial configuration's plan.window (local start/end, timezone, default Asia/Shanghai); outside it a trial
  neither starts nor resumes (outside_trial_window).
- enterprise endpoint: scripts/nx037/endpoint_conformance.py against the effect worker's enterprise provider file (origin,
  credential_file, ca_file, connect_address, timeout); any failed check refuses (enterprise_endpoint_nonconformant). The check
  creates three marked effects on the endpoint (its documented behaviour): give --endpoint-parameters-file with a payload the
  enterprise accepts as a no-op.
- WebChat delivery: GET /v1/health of the reply delivery (local_json_delivery) through the effect worker's reply provider file;
  it must answer 200 {state: ok} within the deadline (webchat_delivery_unavailable).
- outbound recorder: no delivered reply waiting more than 60 s to be written into its conversation (0480 for the metrics role);
  otherwise outbound_recorder_stalled. A recorder that is down with nothing to record passes: the first interaction's reply then
  times out (interaction_timeout), which pauses the trial.
"""
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CONSUMER = 'consumer:'


def _private(path):
    """A regular file, not a symbolic link, mode 0600, owned by this user."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()


def _consumer_id(ref):
    return ref[len(CONSUMER):] if type(ref) is str and ref.startswith(CONSUMER) and len(ref) == len(CONSUMER) + 64 else None


KINDS = ('agent', 'human')
AGENT_TOOL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._()/+-]{0,79}')


def consent(trial_config, plan_value, authorization_file=None):
    """D3 (owner 2026-10-14, ROADMAP §7: the customers are AI agents the owner operates; the owner is the only staff member).
    (refusal codes, participants for the record: [{consumer_id, kind, persona}]).

    - operator_authorization {authorization_sha256}: the owner's own one-time authorization (a private file, 0600) must exist and
      match, else authorization_missing;
    - participants: every consumer of the plan is a participant of kind agent (consumer_ref and the tool, e.g. "Claude Code via
      browser", never a credential); kind human appears only for the owner in the staff role ({kind: human, role: staff}, no
      consumer_ref): a consumer that is not an agent participant, a human entry with a consumer_ref, or any other kind refuses
      with consent_missing."""
    failed = []
    authorization = trial_config.get('operator_authorization')
    digest = authorization.get('authorization_sha256') if type(authorization) is dict else None
    if (type(digest) is not str or len(digest) != 64 or authorization_file is None or not _private(authorization_file)
            or hashlib.sha256(Path(authorization_file).read_bytes()).hexdigest() != digest):
        failed.append('authorization_missing')
    participants = trial_config.get('participants')
    consumers = plan_value.get('consumers')
    if type(participants) is not list or type(consumers) is not list or not consumers or any(type(p) is not dict for p in participants):
        return failed + ['consent_missing'], []
    for p in participants:
        if p.get('kind') not in KINDS or (p['kind'] == 'human' and (p.get('role') != 'staff' or 'consumer_ref' in p)):
            return failed + ['consent_missing'], []
    agents = {p.get('consumer_ref'): p for p in participants if p['kind'] == 'agent'}
    recorded = []
    for ref in consumers:
        entry = agents.get(ref)
        if _consumer_id(ref) is None or entry is None or type(entry.get('agent')) is not str or not AGENT_TOOL.fullmatch(entry['agent']):
            return failed + ['consent_missing'], []
        recorded.append({'consumer_id': _consumer_id(ref), 'kind': 'agent', 'persona': 'simulated_agent'})
    return failed, ([] if failed else recorded)


def persona(participants):
    """The trial's customer persona: simulated_agent when every participant is an agent, human when all are people, else mixed."""
    kinds = {p['kind'] for p in participants}
    return 'simulated_agent' if kinds == {'agent'} else 'human' if kinds == {'human'} else 'mixed' if kinds else None


def window(trial_config, now):
    """Refusal codes for the trial window plan.window {start, end[, timezone]} at `now` (aware)."""
    value = (trial_config.get('plan') or {}).get('window')
    try:
        zone = ZoneInfo(value.get('timezone', 'Asia/Shanghai'))
        start, end = (datetime.strptime(value[k], '%H:%M').time() for k in ('start', 'end'))
    except (AttributeError, KeyError, TypeError, ValueError):
        return ['outside_trial_window']
    local = now.astimezone(zone).time()
    return [] if start <= local < end else ['outside_trial_window']


def _provider(path):
    from nexloop_eios.private_configuration import read_private_text
    value = json.loads(read_private_text(path, maximum=8192))
    if type(value) is not dict or type(value.get('origin')) is not str:
        raise ValueError('provider configuration')
    return value


def enterprise_endpoint(provider_file, parameters_file, out_dir):
    """endpoint_conformance against the enterprise provider; its report stays private in the trial directory."""
    sys.path.insert(0, str(ROOT / 'scripts/nx037'))
    import endpoint_conformance
    try:
        value = _provider(provider_file)
    except (OSError, ValueError):
        return ['enterprise_endpoint_nonconformant']
    argv = ['--origin', value['origin'], '--timeout', str(value.get('timeout', 2.0))]
    for key, flag in (('credential_file', '--credential-file'), ('ca_file', '--ca-file'), ('connect_address', '--connect-address')):
        if value.get(key):
            argv += [flag, str(value[key])]
    if parameters_file:
        argv += ['--parameters-file', str(parameters_file)]
    report = Path(out_dir) / 'preflight' / 'endpoint-conformance.json'
    report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if report.exists():
        report.unlink()
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()):  # its summary line is the report's; the run entry prints only its own
            code = endpoint_conformance.main([*argv, '--out', str(report)])
    except Exception:  # noqa: BLE001 - fixed refusal code, nothing private
        code = 2
    return [] if code == 0 else ['enterprise_endpoint_nonconformant']


def webchat_delivery(provider_file):
    """GET /v1/health of the reply delivery (local_json_delivery) through the effect worker's provider file: 200 {state: ok} means
    reachable over TLS, the bearer credential accepted, the executor authority current in the database and the store usable."""
    sys.path.insert(0, str(ROOT / 'scripts/nx037'))
    from types import SimpleNamespace
    import endpoint_conformance
    try:
        value = _provider(provider_file)
        endpoint = endpoint_conformance.Endpoint(SimpleNamespace(origin=value['origin'], credential_file=value.get('credential_file'),
                                                                 ca_file=value.get('ca_file'), connect_address=value.get('connect_address'),
                                                                 timeout=float(value.get('timeout', 2.0)), test_loopback_http=False))
        status, _, body, *_ = endpoint.call('GET', '/v1/health')
    except Exception:  # noqa: BLE001 - fixed refusal code, nothing private
        return ['webchat_delivery_unavailable']
    return [] if status == 200 and body == {'state': 'ok'} else ['webchat_delivery_unavailable']


def outbound_recorder(connection, tenant):
    backlog = connection.execute("select authz.nexloop_nx037_outbound_backlog(%s,'real')", (tenant,)).fetchone()[0]
    return [] if backlog['stalled'] == 0 else ['outbound_recorder_stalled']


def record_consent(out_dir, participants):
    """consent.json in the trial directory: consumer ids and consent sha256 only (the evidence pack copies it)."""
    root = Path(out_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=root, delete=False) as handle:
        handle.write(json.dumps({'participants': participants}, sort_keys=True))
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, root / 'consent.json')
