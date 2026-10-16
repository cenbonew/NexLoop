"""Reply fail-fast (10/16 stage smoke): a message Run that ends without submitting nexloop.service.request (the model answered in
plain text, which is never delivered) is a definite failure, runtime_no_reply_submitted, and the message's reply guarantee is due
now instead of start_after_seconds later. A Run that submitted its reply is unchanged (test_message_relay's actual Pi test)."""
import json
import secrets

from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.runtime_dispatch import NO_REPLY_CODE, RuntimeDispatcher
from test_message_relay import invoke, message_plan, receipt_plan, relay_plan  # noqa: F401
from test_message_runtime_effect_e2e import actual_host, effect_configuration, files, fresh_worker, guard_server


def text_only_configuration(*args):
    """The Host with its effect tools, whose deterministic model answers in plain text and calls no tool."""
    path = effect_configuration(*args)
    body = json.loads(path.read_text())
    body.pop('deterministic_effect_message')
    path.write_text(json.dumps(body))
    return path


def test_a_message_run_without_a_reply_fails_and_makes_the_reply_due_now(relay_plan, admin, tmp_path):  # noqa: F811
    candidate = relay_plan
    f = candidate['f']
    assert invoke(candidate).returncode == 0
    command = admin.execute('select normalized_input from runtime.jobs').fetchone()[0]['run_command']
    task = admin.execute('select task_id,message_id from runtime.nexloop_message_routes').fetchone()
    due = "select available_at>clock_timestamp() from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s"
    waiting = admin.execute(due, ('reply:' + task[1],)).fetchone()
    runtime, key = files(tmp_path)
    guard_key = tmp_path / 'guard-key'
    guard_key.write_text(secrets.token_hex(32))
    guard_key.chmod(0o600)
    with fresh_worker(f, tmp_path) as worker:
        with guard_server(worker, tmp_path, guard_key) as port:
            with actual_host(runtime, key, text_only_configuration(tmp_path, port, guard_key)) as (_, client, headers):
                control = HostControlConfiguration(str(client.base_url).rstrip('/'), key, tmp_path / 'host-cert.pem')
                dispatched = RuntimeDispatcher(worker, control, queue='operations', total_timeout=15).run_once()
                assert dispatched['claimed'] is True and dispatched['status'] == 'failed', dispatched
                result = worker.inspect_task(queue='operations', task_id=task[0])['result']
                assert result['runtime_outcome'] == 'succeeded' and result['code'] == NO_REPLY_CODE and result['run_id'] == command['run_id']
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions').fetchone() == (0,)
    # The message's reply-due item (0109) waited start_after_seconds; it is due now.
    assert waiting == (True,) and admin.execute(due, ('reply:' + task[1],)).fetchone() == (False,)
