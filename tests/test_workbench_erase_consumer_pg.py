"""NX-029 erasure over the workbench HTTP entry (AT-072 local restore drill: the owner's synthetic erasure after the drill mark).

`erase_consumer` is a registered human operation (0132, capability consumer.erase) granted to the owner only (workbench roles
v4); the workbench entry adds only its validator, so the write goes through the same governed path as every workbench
Action: current Human authentication, the owner's grant, the SQL handler. Synthetic data."""
import pytest

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def test_owner_erases_a_consumer_from_the_workbench_and_the_operator_cannot(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.consumer.erase',))
    consumer = c['consumer']
    body = {'consumer_id': consumer, 'reason': '合成删除'}
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'erase_consumer', body).status_code == 403  # owner only
    assert admin.execute('select count(*) from control.nexloop_deletion_ledger').fetchone() == (0,)
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'erase_consumer', {**body, 'consumer_id': 'not-a-consumer'}).status_code == 422
        assert act(client, csrf, 'erase_consumer', {'consumer_id': consumer}).status_code == 422  # the reason is required
        done = act(client, csrf, 'erase_consumer', body, key='wb-erase-consumer-0001')
        assert done.status_code == 200, done.text
        assert done.json()['operation'] == 'erase_consumer' and done.json()['replayed'] is False
        again = act(client, csrf, 'erase_consumer', body, key='wb-erase-consumer-0001')
        assert again.status_code == 200 and again.json()['replayed'] is True
    assert admin.execute('select runtime.nexloop_consumer_erasing(%s,%s,%s)', (TENANT, 'real', consumer)).fetchone() == (True,)
