from psycopg.conninfo import make_conninfo
import pytest
from eios.adapters.postgres.database import StorageUnavailable
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.assembly import open_core

def test_only_restricted_application_role_can_open_core(admin,pg):
    bootstrap(admin)
    with pytest.raises(StorageUnavailable):
        with open_core(pg):pass
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        with pool.connection() as conn:
            assert conn.execute('select current_user').fetchone()[0]=='nexloop_api'


def test_administrative_connection_cannot_disguise_as_application(admin,pg):
    bootstrap(admin)
    disguised = make_conninfo(pg,options='-c role=nexloop_api')
    with pytest.raises(StorageUnavailable):
        with open_core(disguised):
            pytest.fail('administrative session disguised as app was accepted')


def test_noinherit_owner_membership_is_still_rejected(admin,pg):
    bootstrap(admin)
    admin.execute('grant nexloop_owner to nexloop_api')
    with pytest.raises(StorageUnavailable):
        with open_core(make_conninfo(pg,user='nexloop_api')):
            pytest.fail('SET ROLE path to owner was accepted')
