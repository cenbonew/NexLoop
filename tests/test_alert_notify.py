"""Out-of-workbench alert notification (scripts/deploy/alert_notify.py; docs/implementation/alert-notifier-design.md): dedupe, resolve,
the 24-hour critical repeat, a failed send that is retried and never silent, the URL in no log or error, the four formats, levels,
the evaluator's staleness, refused configuration. Pure: a stand-in reader and opener; files only."""
import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('alert_notify', ROOT / 'scripts/deploy/alert_notify.py')
N = importlib.util.module_from_spec(spec);spec.loader.exec_module(N)

URL = 'https://robot.example.invalid/hook/' + 'tokenmarker-zq'  # a synthetic URL whose path must never be printed
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def private(path, text):
    path.write_text(text);path.chmod(0o600)
    return path


@pytest.fixture
def setup(tmp_path):
    private(tmp_path / 'url', URL + '\n')
    private(tmp_path / 'dsn', 'host=synthetic user=nexloop_metrics password=synthetic-dsn-secret')

    def configure(**over):
        config = {'schema': N.SCHEMA, 'host_label': 'stage', 'metrics_dsn_file': str(tmp_path / 'dsn'), 'webhook_url_file': str(tmp_path / 'url'),
                  'format': 'generic', 'state_file': str(tmp_path / 'state' / 'state.json'), **over}
        return private(tmp_path / 'config.json', json.dumps(config))
    return tmp_path, configure


def export(*alerts, evaluated='now'):
    return [{'tenant_id': 'tenant-a', 'world': 'real', 'last_evaluated_at': evaluated if evaluated in ('now', None) else evaluated.isoformat().replace('+00:00', 'Z'),
             'firing': [{'dedupe_key': key, 'rule_id': key.split(':')[0], 'severity': severity, 'first_fired_at': first, 'fire_count': count}
                        for key, severity, first, count in alerts]}]


class Robot:
    """A stand-in webhook: records each POST; answers with `answers` in turn (status, body) or raises."""
    def __init__(self, *answers):
        self.answers, self.posts = list(answers) or [(200, b'{}')], []

    def __call__(self, request, timeout):
        self.posts.append((request.full_url, json.loads(request.data)))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer

        class Response:
            def __enter__(self):return self
            def __exit__(self, *a):return False
            def read(self, n):return body
        r = Response();r.status = status
        return r


def tick(config, data, robot, at, *extra):
    out, err = io.StringIO(), io.StringIO()
    stamp = at.isoformat().replace('+00:00', 'Z')  # the evaluator ran just now unless a test says otherwise
    data = [{**scope, 'last_evaluated_at': stamp if scope['last_evaluated_at'] == 'now' else scope['last_evaluated_at']} for scope in data]
    code = N.main(['--config', str(config), *extra], reader=lambda dsn: data, opener=robot, now=at, out=out, error_stream=err)
    return code, json.loads(out.getvalue()), err.getvalue()


def test_a_new_alert_is_sent_once_then_its_resolution_once(setup):
    tmp, configure = setup;config = configure()
    robot = Robot()
    firing = export(('timer_stale_file_backup:app_host:file-backup', 'warning', '2026-10-30T04:15:00Z', 11))
    code, out, _ = tick(config, firing, robot, T0)
    assert code == 0 and out['fired'] == 1 and out['sent'] is True and len(robot.posts) == 1
    sent = robot.posts[0][1]
    assert sent['events'][0]['rule_id'] == 'timer_stale_file_backup' and '新触发' in sent['text'] and 'file-backup' in sent['text']
    for later in (15, 30, 45):  # still firing: nothing more
        code, out, _ = tick(config, firing, robot, T0 + timedelta(minutes=later))
        assert code == 0 and out['events'] == 0 and out['sent'] is False
    assert len(robot.posts) == 1
    code, out, _ = tick(config, export(), robot, T0 + timedelta(hours=1))
    assert out['resolved'] == 1 and len(robot.posts) == 2 and '已恢复' in robot.posts[1][1]['text']
    code, out, _ = tick(config, export(), robot, T0 + timedelta(hours=2))
    assert out['events'] == 0 and len(robot.posts) == 2


def test_fired_again_is_a_new_alert(setup):
    tmp, configure = setup;config = configure();robot = Robot()
    tick(config, export(('a:x', 'warning', '2026-11-08T09:00:00Z', 1)), robot, T0)
    code, out, _ = tick(config, export(('a:x', 'warning', '2026-11-08T11:00:00Z', 2)), robot, T0 + timedelta(hours=2))
    assert out['fired'] == 1 and len(robot.posts) == 2


def test_a_critical_alert_repeats_every_24_hours_a_warning_never(setup):
    tmp, configure = setup;config = configure();robot = Robot()
    data = export(('crit:x', 'critical', '2026-11-08T09:00:00Z', 1), ('warn:y', 'warning', '2026-11-08T09:00:00Z', 1))
    tick(config, data, robot, T0)
    assert len(robot.posts) == 1 and len(robot.posts[0][1]['events']) == 2
    _, out, _ = tick(config, data, robot, T0 + timedelta(hours=23, minutes=59))
    assert out['events'] == 0
    _, out, _ = tick(config, data, robot, T0 + timedelta(hours=24))
    assert out['repeat'] == 1 and [e['key'] for e in robot.posts[1][1]['events']] == ['tenant-a/real/crit:x']
    _, out, _ = tick(config, data, robot, T0 + timedelta(hours=30))
    assert out['events'] == 0
    _, out, _ = tick(config, data, robot, T0 + timedelta(hours=48))
    assert out['repeat'] == 1 and len(robot.posts) == 3


def test_a_failed_send_is_logged_exits_1_and_is_sent_again_next_round(setup):
    tmp, configure = setup;config = configure()
    data = export(('a:x', 'critical', '2026-11-08T09:00:00Z', 1))
    for failure in (OSError('network down ' + URL), (500, b''), (200, b'{"errcode": 310000, "errmsg": "keywords not in content"}')):
        robot = Robot(failure)
        fmt = 'dingtalk' if isinstance(failure, tuple) and failure[0] == 200 else 'generic'
        config = configure(format=fmt)
        code, out, err = tick(config, data, robot, T0)
        assert code == 1 and out == {**out, 'ok': False, 'reason': 'send_failed'} and err.startswith('<3>alert-notify send failed')
        assert not (tmp / 'state' / 'state.json').exists()  # nothing remembered: retried
    robot = Robot()
    code, out, _ = tick(configure(), data, robot, T0 + timedelta(minutes=15))
    assert code == 0 and out['fired'] == 1 and len(robot.posts) == 1


def test_the_url_and_the_dsn_never_reach_output_or_errors(setup):
    tmp, configure = setup;config = configure()
    data = export(('a:x', 'warning', '2026-11-08T09:00:00Z', 1))
    for robot in (Robot(OSError('cannot reach ' + URL)), Robot(ValueError(URL)), Robot((403, URL.encode()))):
        code, out, err = tick(config, data, robot, T0)
        printed = json.dumps(out) + err
        assert code == 1 and 'tokenmarker' not in printed and 'robot.example' not in printed
    code, out, err = N.main(['--config', str(config)], reader=lambda dsn: (_ for _ in ()).throw(RuntimeError(dsn)), opener=Robot(), now=T0,
                            out=io.StringIO(), error_stream=(e := io.StringIO())), None, None
    assert code == 1 and 'synthetic-dsn-secret' not in e.getvalue()


@pytest.mark.parametrize('fmt,ok,check', [
    ('generic', b'', lambda b: b['source'] == 'nexloop' and b['events'] and '新触发' in b['text']),
    ('feishu', b'{"code":0,"msg":"success"}', lambda b: b['msg_type'] == 'text' and '新触发' in b['content']['text']),
    ('wecom', b'{"errcode":0,"errmsg":"ok"}', lambda b: b['msgtype'] == 'text' and '新触发' in b['text']['content']),
    ('dingtalk', b'{"errcode":0,"errmsg":"ok"}', lambda b: b['msgtype'] == 'text' and '新触发' in b['text']['content']),
])
def test_the_four_formats(setup, fmt, ok, check):
    tmp, configure = setup;config = configure(format=fmt);robot = Robot((200, ok))
    code, out, _ = tick(config, export(('a:x', 'critical', '2026-11-08T09:00:00Z', 1)), robot, T0)
    assert code == 0 and out['sent'] is True and robot.posts[0][0] == URL and check(robot.posts[0][1])
    if fmt != 'generic':  # a robot that answers 200 without its success code has refused
        tmp2 = tmp / 'refused';tmp2.mkdir()
        refused = configure(format=fmt, state_file=str(tmp2 / 'state.json'))
        assert tick(refused, export(('a:x', 'critical', '2026-11-08T09:00:00Z', 1)), Robot((200, b'{}')), T0)[0] == 1


def test_levels_and_the_evaluator(setup):
    tmp, configure = setup;config = configure(levels=['critical']);robot = Robot()
    _, out, _ = tick(config, export(('warn:y', 'warning', '2026-11-08T09:00:00Z', 1)), robot, T0)
    assert out['events'] == 0 and robot.posts == []
    _, out, _ = tick(config, export(), robot, T0 + timedelta(hours=1))
    assert out['events'] == 0 and robot.posts == []  # a warning never sent is not resolved either
    # The evaluator stopped: no alert can fire, so that itself is a critical notification.
    _, out, _ = tick(config, export(evaluated=T0), robot, T0 + timedelta(minutes=11))
    assert out['fired'] == 1 and 'evaluator_stale' in robot.posts[0][1]['text']


def test_dry_run_sends_nothing_and_remembers_nothing(setup):
    tmp, configure = setup;config = configure();robot = Robot()
    _, out, _ = tick(config, export(('a:x', 'warning', '2026-11-08T09:00:00Z', 1)), robot, T0, '--dry-run')
    assert out['fired'] == 1 and out['sent'] is False and robot.posts == [] and not (tmp / 'state' / 'state.json').exists()


@pytest.mark.parametrize('label,over,url', [
    ('unknown format', {'format': 'slack'}, URL), ('unknown key', {'extra': 1}, URL), ('bad level', {'levels': ['info']}, URL),
    ('repeat too short', {'critical_repeat_seconds': 60}, URL), ('not https', {}, 'http://robot.example.invalid/x'),
])
def test_refused_configuration(setup, label, over, url):
    tmp, configure = setup
    private(tmp / 'url', url)
    code, out, err = tick(configure(**over), export(), Robot(), T0)
    assert code == 2 and out == {'ok': False, 'reason': 'configuration'} and 'robot.example' not in err, label


def test_a_url_file_readable_by_others_is_refused(setup):
    tmp, configure = setup
    (tmp / 'url').chmod(0o644)
    assert tick(configure(), export(), Robot(), T0)[0] == 2


def test_the_shipped_example_configuration_loads(tmp_path):
    example = json.loads((ROOT / 'deploy/examples/alert-notify.example.json').read_text())
    config = private(tmp_path / 'config.json', json.dumps(example))
    loaded = N.load_config(config)
    assert loaded['format'] in N.FORMATS and loaded['levels'] == ['critical', 'warning'] and loaded['critical_repeat_seconds'] == 86400
    unit = (ROOT / 'deploy/stage/systemd/nexloop-alert-notify.service.example').read_text()
    assert 'alert_notify.py --config /etc/nexloop/alert-notify.json' in unit and 'https://' not in unit


def test_v34_adds_exactly_timer_stale_alert_notify_on_v33():
    rules = ROOT / 'deploy/configuration'
    v33, v34 = json.loads((rules / 'alert-rules.v33.json').read_text()), json.loads((rules / 'alert-rules.v34.json').read_text())
    assert v34['version'] == 34 and v34['rules'][:-1] == v33['rules'] and v34['decision'].startswith(v33['decision'] + ' v34 ')
    added = v34['rules'][-1]
    assert (added['rule_id'], added['selector'], added['metric'], added['severity']) == (
        'timer_stale_alert_notify', 'app_host:alert-notify', 'host_health.timer_success_age_seconds', 'warning')



@pytest.mark.parametrize('data', [[], [{'tenant_id': 'tenant-a', 'world': 'sim-1', 'last_evaluated_at': 'now', 'firing': []}]])
def test_no_real_world_entry_is_the_evaluator_stopped(setup, data):
    tmp, configure = setup;config = configure();robot = Robot()
    code, out, _ = tick(config, data, robot, T0)
    assert code == 0 and out['fired'] == 1 and len(robot.posts) == 1
    assert [e['rule_id'] for e in robot.posts[0][1]['events']] == ['evaluator_stale'] and robot.posts[0][1]['events'][0]['severity'] == 'critical'
    _, out, _ = tick(config, data, robot, T0 + timedelta(minutes=15))
    assert out['events'] == 0  # once, then every 24 hours like any critical
