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
    # 0204: the NX-037 Run evidence names why this Run failed (codes only).
    tenant = admin.execute('select tenant_id from runtime.nexloop_message_routes where run_id=%s', (command['run_id'],)).fetchone()[0]
    failure = admin.execute("select runtime.nexloop_nx037_run_evidence(%s,'real',%s)->'failure'", (tenant, command['run_id'])).fetchone()[0]
    assert failure == {'code': NO_REPLY_CODE}
    # The message's reply-due item (0109) waited start_after_seconds; it is due now.
    assert waiting == (True,) and admin.execute(due, ('reply:' + task[1],)).fetchone() == (False,)


def test_a_message_run_that_replied_never_makes_the_reply_due_early(relay_plan, admin, tmp_path):  # noqa: F811
    """10/17 stage smoke: a Run that submitted its reply and then failed (a later tool call) must not expedite the reply
    guarantee: 0200 looks only at whether the Run submitted nexloop.service.request, never at its outcome."""
    from nexloop_eios.runtime_dispatch import reply_settle
    candidate = relay_plan
    f = candidate['f']
    assert invoke(candidate).returncode == 0
    task = admin.execute('select task_id,message_id,run_id from runtime.nexloop_message_routes').fetchone()
    due = "select available_at from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s"
    before = admin.execute(due, ('reply:' + task[1],)).fetchone()
    runtime, key = files(tmp_path)
    guard_key = tmp_path / 'guard-key'
    guard_key.write_text(secrets.token_hex(32))
    guard_key.chmod(0o600)
    with fresh_worker(f, tmp_path) as worker:
        with guard_server(worker, tmp_path, guard_key) as port:
            with actual_host(runtime, key, effect_configuration(tmp_path, port, guard_key)) as (_, client, headers):
                control = HostControlConfiguration(str(client.base_url).rstrip('/'), key, tmp_path / 'host-cert.pem')
                dispatched = RuntimeDispatcher(worker, control, queue='operations', total_timeout=15).run_once()
                assert dispatched['claimed'] is True, dispatched
                result = worker.inspect_task(queue='operations', task_id=task[0])['result']
                assert result.get('code') != NO_REPLY_CODE
        # Settled again as a failed Run would be: replied, nothing expedited, the due time unchanged.
        assert reply_settle(worker, str(task[2])) == {'reply_run': True, 'kind': 'message', 'replied': True, 'expedited': False}
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions where run_id=%s', (task[2],)).fetchone()[0] >= 1
    assert admin.execute(due, ('reply:' + task[1],)).fetchone() in (before, None)


def test_a_message_run_failed_for_good_before_the_host_still_makes_its_reply_due_now(relay_plan, admin, tmp_path):  # noqa: F811
    """10/18 stage (Run 13a9a048): a message Run whose task ended in the dispatch's error path (here its credential expired while
    queued: runtime_command_expired, terminal) never reached the reply settle, so its reply guarantee waited start_after_seconds.
    The error path settles it too: no reply was submitted, so the reply-due item is due now."""
    from psycopg.types.json import Jsonb
    candidate = relay_plan
    f = candidate['f']
    assert invoke(candidate).returncode == 0
    job_id, normalized = admin.execute('select job_id,normalized_input from runtime.jobs').fetchone()
    normalized['run_command']['not_after'] = '2000-01-01T00:00:00Z'  # test data: the queued Run's lifetime is over
    admin.execute('update runtime.jobs set normalized_input=%s where job_id=%s', (Jsonb(normalized), job_id))
    task = admin.execute('select task_id,message_id from runtime.nexloop_message_routes').fetchone()
    due = "select available_at>clock_timestamp() from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s"
    assert admin.execute(due, ('reply:' + task[1],)).fetchone() == (True,)
    runtime, key = files(tmp_path)
    guard_key = tmp_path / 'guard-key'
    guard_key.write_text(secrets.token_hex(32))
    guard_key.chmod(0o600)
    with fresh_worker(f, tmp_path) as worker:
        with guard_server(worker, tmp_path, guard_key) as port:
            with actual_host(runtime, key, effect_configuration(tmp_path, port, guard_key)) as (_, client, headers):
                control = HostControlConfiguration(str(client.base_url).rstrip('/'), key, tmp_path / 'host-cert.pem')
                dispatched = RuntimeDispatcher(worker, control, queue='operations', total_timeout=15).run_once()
                assert dispatched['status'] == 'failed' and dispatched['result']['code'] == 'runtime_command_expired', dispatched
    assert admin.execute(due, ('reply:' + task[1],)).fetchone() == (False,)
