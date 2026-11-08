#!/usr/bin/env python3
"""Out-of-workbench alert notification (owner 2026-11-08; docs/implementation/alert-notifier-design.md): the firing alerts, read
read-only, sent to one group-robot webhook when they change.

    alert_notify.py --config /etc/nexloop/alert-notify.json [--dry-run]

Run by a systemd timer on APP_HOST every 15 minutes (deploy/stage/systemd/nexloop-alert-notify.*.example). Each run:
  1. reads authz.nexloop_alerts_firing_export() as the read-only nexloop_metrics login (migration alert_notify_export) in a read-only
     transaction: per tenant and world, each firing alert's dedupe_key, rule_id, severity, first_fired_at, fire_count, and the
     evaluator's last evaluation. Nothing else: no value, no business or Consumer data;
  2. compares with the local state file and sends one message when something changed:
       fired       a firing alert not notified yet (or fired again: another first_fired_at);
       resolved    a notified alert that no longer fires;
       repeat      a critical alert still firing 24 hours (critical_repeat_seconds) after its last notification;
       evaluator   the evaluator has not evaluated for 10 minutes (no alert can fire then): critical, with the same rules;
     severities not in `levels` are kept in the state but never sent;
  3. writes the state only after the webhook accepted the message, so a failed send is sent again next round.

Webhook formats (`format`): generic (JSON POST {source, host, events, text}), feishu, wecom, dingtalk (their text robots). The URL is
read from a 0600 file (the owner writes it from Keychain); it never appears in the repository, a log line, a process argument or an
error message: errors carry the exception class and the HTTP status only. A robot that answers 200 with a non-zero code (feishu
code, wecom/dingtalk errcode) has refused the message.

Configuration (JSON, 0600, root): {"schema": "nexloop-alert-notify/1", "host_label": "stage", "metrics_dsn_file": "<0600>",
"webhook_url_file": "<0600>", "format": "generic|feishu|wecom|dingtalk", "state_file": "/var/lib/nexloop-alert-notify/state.json",
"levels": ["critical", "warning"], "critical_repeat_seconds": 86400, "evaluator_stale_seconds": 600, "timeout_seconds": 10}
Output: one JSON line of counts (never the URL or the DSN). A failure is also one journal line of priority err (`<3>` prefix) on
stderr. Exit 0 sent or nothing to send (or dry run); 1 the read or the send failed (the unit fails, host health sees the timer
stale: timer key alert-notify); 2 configuration refused.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.request
from datetime import UTC, datetime

SCHEMA = 'nexloop-alert-notify/1'
FORMATS = ('generic', 'feishu', 'wecom', 'dingtalk')
SEVERITIES = ('critical', 'warning')
EXPORT_SQL = 'select authz.nexloop_alerts_firing_export()'
LABEL = {'fired': '新触发', 'resolved': '已恢复', 'repeat': '持续中（critical 超过 24 小时）'}


class Refused(ValueError):
    pass


class SendFailed(RuntimeError):
    pass


def read_private(path, maximum=8192):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller')
        return stream.read(maximum).decode('utf-8')


def load_config(path):
    try:
        config = json.loads(read_private(path))
    except (OSError, ValueError) as error:
        raise Refused(f'configuration unreadable ({type(error).__name__})') from None
    required = {'schema', 'host_label', 'metrics_dsn_file', 'webhook_url_file', 'format', 'state_file'}
    optional = {'levels', 'critical_repeat_seconds', 'evaluator_stale_seconds', 'timeout_seconds'}
    if type(config) is not dict or not required <= set(config) or set(config) - required - optional or config['schema'] != SCHEMA:
        raise Refused('configuration keys')
    if config['format'] not in FORMATS:
        raise Refused('configuration format')
    if type(config['host_label']) is not str or not 1 <= len(config['host_label']) <= 40:
        raise Refused('configuration host_label')
    levels = config.setdefault('levels', list(SEVERITIES))
    if type(levels) is not list or not levels or not set(levels) <= set(SEVERITIES):
        raise Refused('configuration levels')
    for key, default, low, high in (('critical_repeat_seconds', 86400, 3600, 604800), ('evaluator_stale_seconds', 600, 300, 86400),
                                    ('timeout_seconds', 10, 1, 60)):
        value = config.setdefault(key, default)
        if type(value) is not int or not low <= value <= high:
            raise Refused(f'configuration {key}')
    return config


def webhook_url(path):
    url = read_private(path, 2048).strip()
    if not url.startswith('https://') or any(c.isspace() for c in url):
        raise Refused('webhook url file must hold one https URL')
    return url


def read_alerts(dsn):
    """The export as the read-only metrics login, in a read-only transaction."""
    import psycopg
    with psycopg.connect(dsn, autocommit=False) as db:
        db.execute('set transaction read only')
        if db.execute('select current_user').fetchone()[0] != 'nexloop_metrics':
            raise Refused('the metrics DSN must log in as nexloop_metrics')
        return db.execute(EXPORT_SQL).fetchone()[0]


def _at(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')) if text else None


def current(export, now, evaluator_stale_seconds):
    """{key: {rule_id, severity, first_fired_at, fire_count}} of what fires now, the evaluator's staleness included."""
    out = {}
    for scope in export or []:
        base = f"{scope['tenant_id']}/{scope['world']}/"
        for alert in scope.get('firing') or []:
            out[base + alert['dedupe_key']] = {k: alert[k] for k in ('rule_id', 'severity', 'first_fired_at', 'fire_count')}
        last = _at(scope.get('last_evaluated_at'))
        if scope['world'] == 'real' and (last is None or (now - last).total_seconds() > evaluator_stale_seconds):
            out[base + 'evaluator_stale'] = {'rule_id': 'evaluator_stale', 'severity': 'critical',
                                             'first_fired_at': scope.get('last_evaluated_at'), 'fire_count': 1}
    return out


def decide(firing, state, now, config):
    """(events, next state). state: {key: {rule_id, severity, first_fired_at, notified_at}}."""
    events, nxt, levels = [], {}, set(config['levels'])
    for key, alert in sorted(firing.items()):
        known = state.get(key)
        entry = {**{k: alert[k] for k in ('rule_id', 'severity', 'first_fired_at')}, 'notified_at': known['notified_at'] if known else None}
        if known is None or known.get('first_fired_at') != alert['first_fired_at']:
            kind = 'fired'
        elif alert['severity'] == 'critical' and known.get('notified_at') and \
                (now - _at(known['notified_at'])).total_seconds() >= config['critical_repeat_seconds']:
            kind = 'repeat'
        else:
            kind = None
        if kind and alert['severity'] in levels:
            events.append({'kind': kind, 'key': key, **{k: alert[k] for k in ('rule_id', 'severity', 'first_fired_at', 'fire_count')}})
            entry['notified_at'] = now.isoformat()
        elif kind == 'fired':
            entry['notified_at'] = None  # a level not sent: remembered, never sent, and its resolution is not sent either
        nxt[key] = entry
    for key, known in sorted(state.items()):
        if key not in firing and known.get('notified_at') and known.get('severity') in levels:
            events.append({'kind': 'resolved', 'key': key, 'rule_id': known['rule_id'], 'severity': known['severity'],
                           'first_fired_at': known['first_fired_at'], 'fire_count': None})
    return events, nxt


def text(events, host_label):
    counts = {k: sum(e['kind'] == k for e in events) for k in LABEL}
    head = f"[NexLoop {host_label}] 告警：" + '，'.join(f"{LABEL[k]} {n}" for k, n in counts.items() if n)
    lines = [f"- {LABEL[e['kind']]} {e['severity']} {e['key'].split('/', 2)[2]}" + (f"（首次 {e['first_fired_at']}，次数 {e['fire_count']}）"
             if e['kind'] != 'resolved' else f"（首次 {e['first_fired_at']}）") for e in events[:40]]
    if len(events) > 40:
        lines.append(f'- 另有 {len(events) - 40} 条，见工作台告警页')
    return '\n'.join([head, *lines, '详情见工作台告警页。'])


def body(events, config):
    message = text(events, config['host_label'])
    if config['format'] == 'feishu':
        return {'msg_type': 'text', 'content': {'text': message}}
    if config['format'] in ('wecom', 'dingtalk'):
        return {'msgtype': 'text', 'text': {'content': message}}
    return {'source': 'nexloop', 'host': config['host_label'], 'events': events, 'text': message}


def send(url, payload, config, opener=urllib.request.urlopen):
    """POST the payload; any failure raises SendFailed with the exception class or the status only - never the URL."""
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST',
                                     headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with opener(request, timeout=config['timeout_seconds']) as response:
            status, raw = response.status, response.read(65536)
    except Exception as error:  # noqa: BLE001 - urllib's messages can carry the URL: only the class leaves this function
        status = getattr(error, 'code', None)
        raise SendFailed(type(error).__name__ + (f' status {status}' if type(status) is int else '')) from None
    if not 200 <= status < 300:
        raise SendFailed(f'status {status}')
    if config['format'] != 'generic':
        try:
            answer = json.loads(raw or b'{}')
        except ValueError:
            raise SendFailed('robot answer not JSON') from None
        code = (answer.get('code', answer.get('StatusCode')) if config['format'] == 'feishu' else answer.get('errcode')) if type(answer) is dict else None
        if code != 0:
            raise SendFailed(f'robot refused (code {code if type(code) is int else "missing"})')


def load_state(path):
    try:
        with open(path, encoding='utf-8') as stream:
            state = json.load(stream)
        return state if type(state) is dict else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}  # damaged: start again (every firing alert is sent once more)


def save_state(path, state):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = path + '.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(state, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def err(message, stream=sys.stderr):
    print(f'<3>alert-notify {message}', file=stream)


def main(argv=None, *, reader=read_alerts, opener=urllib.request.urlopen, now=None, out=sys.stdout, error_stream=sys.stderr):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)
    try:
        config = load_config(args.config)
        url = webhook_url(config['webhook_url_file'])
        dsn = read_private(config['metrics_dsn_file']).strip()
    except (Refused, OSError) as error:
        err(f'configuration refused: {error if isinstance(error, Refused) else type(error).__name__}', error_stream)
        print(json.dumps({'ok': False, 'reason': 'configuration'}), file=out)
        return 2
    try:
        export = reader(dsn)
    except Exception as error:  # noqa: BLE001 - never the DSN
        err(f'read failed: {type(error).__name__}', error_stream)
        print(json.dumps({'ok': False, 'reason': 'read_failed'}), file=out)
        return 1
    state = load_state(config['state_file'])
    events, nxt = decide(current(export, now, config['evaluator_stale_seconds']), state, now, config)
    summary = {'ok': True, 'firing': len(nxt), 'events': len(events),
               **{k: sum(e['kind'] == k for e in events) for k in LABEL}, 'sent': False, 'dry_run': args.dry_run}
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False), file=out)
        return 0
    if events:
        try:
            send(url, body(events, config), config, opener)
        except SendFailed as error:
            err(f'send failed: {error}', error_stream)
            print(json.dumps({**summary, 'ok': False, 'reason': 'send_failed'}, ensure_ascii=False), file=out)
            return 1  # the state is not written: the same events are sent next round
        summary['sent'] = True
    save_state(config['state_file'], nxt)
    print(json.dumps(summary, ensure_ascii=False), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
