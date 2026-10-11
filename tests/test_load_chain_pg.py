"""NX-031 slice 2 on clean catalog PostgreSQL: manifest consumers set up and enrolled on the actual multi-consumer relay chain,
small scale. The production MessageRelay (run profile mode) claims every customer's message with that customer's own profile."""
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path

from psycopg.conninfo import make_conninfo
from nexloop_eios.backend import open_backend
from nexloop_eios.browser_identity import open_browser_identity
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from nexloop_eios.message_relay import MessageRelay
from nexloop_eios.run_credential_vault import RunCredentialVault
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from load_chain import LoadChain

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('load_synthetic',ROOT/'scripts/load/synthetic.py');S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)


@contextmanager
def chain_tenant(f,tmp_path):
    o=f['original'];p=o['paths']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_api'),artifact_root=tmp_path/'load-api',signing_key_file=p['backend_signing'],
                      signing_key_id='explicit-configuration') as api, open_browser_identity(make_conninfo(o['pg'],user='nexloop_identity')) as pool:
        yield api,PostgresBrowserSessionUnitOfWork(pool,tenant_id=o['tenant'],application_id=o['application'])


def test_manifest_consumers_enroll_and_the_relay_routes_each_with_its_own_profile(assembled_message,admin,tmp_path):
    f=assembled_message
    *records,trailer=[json.loads(x) for x in S.lines(31,3)]
    with chain_tenant(f,tmp_path) as (api,uow):
        chain=LoadChain(f,admin,api,uow)
        loaded=chain.load(records)
        assert loaded['consumers']==loaded['enrolled']==3 and loaded['messages']==trailer['totals']['messages']
        vault_dir=tmp_path/'vault';vault_dir.mkdir(mode=0o700)
        with RunCredentialVault(vault_dir) as vault:
            relay=MessageRelay(route=api.authenticate(chain.route_token,world='real'),source=api.authenticate(chain.source_token,world='real'),
                               planner=api.authenticate(chain.planner_token,world='real'),executor_token=chain.executor_token,vault=vault,run_profile=chain.run_profile)
            statuses=[relay.run_once() for _ in range(3)]
        assert statuses==['queued']*3,statuses
        people={p['consumer']:p for _,p in loaded['people']}
        for consumer,message in admin.execute('select consumer_id,message_id from runtime.nexloop_relay_claims').fetchall():
            props=admin.execute("select properties from ontology.objects where type_name='MessageAssignment' and properties->>'message_id'=%s",(message,)).fetchone()[0]
            assert props['consumer_id']==consumer and props['control_id']==people[consumer]['control']
