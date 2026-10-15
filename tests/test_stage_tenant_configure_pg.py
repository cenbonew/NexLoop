"""Run sheet 3.4 gap G2: on the stage, the tenant's effect categories and alert rules are configured through the same configurator
login window as `stage_secrets.py register` (`real_tenant_init.py configure --bootstrap-dsn-file`).

Clean catalog PostgreSQL: bootstrap, register through the window, configure through the window; the alert rules of the trial
configuration and the effect category declarations of the published Actions are in effect, a second configure changes nothing,
and the configurator is NOLOGIN without a password after every window."""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.conninfo import make_conninfo

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_stage_secrets import S, tree  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import real_tenant_init as I  # noqa: E402

NOW = datetime.now(UTC).replace(microsecond=0)


def closed(admin):
    return admin.execute("select rolcanlogin, rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone() == (False, True)


def test_configure_after_register_through_the_bootstrap_window_is_idempotent_and_closes_the_window(pg, admin, tree, capsys):  # noqa: F811
    bootstrap(admin)
    socket_dsn = tree['tmp'] / 'bootstrap-socket-dsn-configure'
    socket_dsn.write_text(make_conninfo(pg, user='nexloop_bootstrap'))
    socket_dsn.chmod(0o600)
    expires = (NOW + timedelta(days=30)).isoformat()
    assert S.main(['register', '--out', str(tree['out']), '--tenant', TENANT_A, '--bootstrap-dsn-file', str(socket_dsn),
                   '--signing-key-id', 'stage-1', '--credential-expires-at', expires]) == 0
    capsys.readouterr()
    catalog = tree['tmp'] / 'value-catalog-configure.json'
    catalog.write_text(json.dumps({'offerings': ['synthetic-offering']}))
    args = ['configure', '--tenant', TENANT_A, '--owner-principal', 'owner-principal-synthetic', '--value-catalog', str(catalog),
            '--channel-ref', 'channel:webchat:synthetic', '--period-start', (NOW - timedelta(hours=1)).isoformat(),
            '--period-end', (NOW + timedelta(days=3)).isoformat(), '--bootstrap-dsn-file', str(socket_dsn)]
    def configure():
        assert I.main(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert closed(admin) and out['mode'] == 'configure'
        return out['steps']

    def state():
        return admin.execute("select authz.nexloop_nx037_tenant_state(%s,'real')", (TENANT_A,)).fetchone()[0]
    p = I.parameters(tenant=TENANT_A, owner_principal='owner-principal-synthetic', value_catalog=catalog, channel_ref='channel:webchat:synthetic',
                     period_start=(NOW - timedelta(hours=1)).isoformat(), period_end=(NOW + timedelta(days=3)).isoformat())
    # Right after register no Action of the categories manifest is published: every declaration is deferred (undeclared is
    # customer contact and external, fail closed) and the alert rules take effect.
    first = configure()
    assert first['effect_categories'] == {'declared': 0, 'deferred': 3}
    assert first['alert_rules']['configured'] is True and first['alert_rules']['replay'] is False
    assert state()['alert_rules']['version'] == p['alert_rules']['version'] == first['alert_rules']['version'] and state()['effect_categories'] == 0
    # An Action published later (here the fixture's governed publication) is declared by running configure again.
    from test_nx037_tenant_init_pg import publish_incentive
    publish_incentive(admin)
    second = configure()
    assert second['effect_categories']['declared'] == 1 and second['effect_categories']['deferred'] == 2 and second['alert_rules']['replay'] is True
    after = state()
    assert after['effect_categories'] == 1 and after['alert_rules']['version'] == p['alert_rules']['version']
    # Idempotent: the same configuration again changes nothing.
    third = configure()
    assert third['effect_categories']['declared'] == 1 and third['alert_rules']['replay'] is True and state() == after, (second, third)
    assert third['effect_categories'].get('written', 0) == 0, third
    # The window refuses anything but the bootstrap socket DSN, and stays closed.
    tcp = tree['tmp'] / 'bootstrap-tcp-dsn'
    tcp.write_text(make_conninfo(pg, user='nexloop_bootstrap', host='localhost'))
    tcp.chmod(0o600)
    assert I.main([*args[:-1], str(tcp)]) == 2 and 'local socket' in capsys.readouterr().out and closed(admin)
    assert I.main([*args, '--configurator-dsn-file', str(socket_dsn)]) == 2 and closed(admin)  # exactly one connection source
