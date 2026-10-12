"""NX-031 ladder (500 customers): the multi-consumer relay issues no new Run while its tenant already has max_backlog open
message Run tasks, so a Run credential (300 s) is never issued only to expire in the queue. The bound is a number from a
definer (authz.nexloop_relay_backlog: the caller's tenant only, service identities only); without a bound the relay claims as
before. Actual PG authority, synthetic data (test_relay_multi_consumer_pg fixture)."""
import psycopg
import pytest
from nexloop_eios.message_relay import MessageRelayUnavailable
from nexloop_eios.run_credential_vault import RunCredentialVault
from test_relay_multi_consumer_pg import claims, multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401


def open_tasks(admin):
    return admin.execute("select count(*) from runtime.jobs where queue='operations' and status in ('pending','retry_wait','running')").fetchone()[0]


def test_the_relay_waits_at_its_backlog_bound_and_resumes_below_it(multi):
    m = multi
    admin = m['admin']
    for label in 'AB':
        m['enroll'](label)
    with RunCredentialVault(m['vault_dir']) as vault:
        assert m['relay'](vault, max_backlog=1).run_once() == 'queued'
        assert open_tasks(admin) == 1
        issued = admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()[0]
        # At the bound: nothing is claimed and no Run credential is issued.
        assert m['relay'](vault, max_backlog=1).run_once() == 'idle'
        assert open_tasks(admin) == 1 and admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()[0] == issued
        # A higher bound (or none) claims again.
        assert m['relay'](vault, max_backlog=2).run_once() == 'queued'
        assert m['relay'](vault).run_once() in ('queued', 'idle')


def test_the_bound_is_validated_and_the_count_is_service_only(multi):
    m = multi
    with RunCredentialVault(m['vault_dir']) as vault:
        for bad in (0, -1, 10001, '4', 1.5):
            with pytest.raises(MessageRelayUnavailable):
                m['relay'](vault, max_backlog=bad)
    # Only the API role (the relay's) may ask, and only for its own tenant (no tenant argument exists).
    with psycopg.connect(m['admin'].info.dsn.replace('user=nexloop_bootstrap', 'user=nexloop_domain_worker'), autocommit=True) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("select authz.nexloop_relay_backlog('x','real')")
