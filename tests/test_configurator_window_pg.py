"""scripts/deploy/configurator_window.py hardening (L6 sim-credentials-window review 1-1, 5-1), on PostgreSQL: the window's password
is valid for WINDOW_MINUTES only; SIGTERM closes it like any exception; the close comes first and uses a fresh bootstrap connection
when the window's own is gone; a close that cannot happen at all is reported and the lapsed window healed by the next one; a window
still valid is never taken over."""
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import configurator_window as W  # noqa: E402

from nexloop_eios.bootstrap import bootstrap  # noqa: E402


@pytest.fixture
def window(admin, pg, tmp_path):
    bootstrap(admin)
    admin.execute(f'alter role {W.CONFIGURATOR} nologin password null')
    path = tmp_path / 'bootstrap-dsn'
    path.write_text(pg)
    path.chmod(0o600)
    return path


def role(admin):
    """(can log in, has a password, expires (a finite VALID UNTIL), time left until it)."""
    return admin.execute(f"""select rolcanlogin, rolpassword is not null, coalesce(rolvaliduntil<>'infinity',false),
        case when rolvaliduntil<>'infinity' then rolvaliduntil - clock_timestamp() end from pg_authid where rolname='{W.CONFIGURATOR}'""").fetchone()


def closed(admin):
    login, has_password, expires, _ = role(admin)
    return not login and not has_password and not expires


def test_the_window_lasts_ten_minutes_and_closes_to_nologin_without_password_or_expiry(window, admin):
    from datetime import timedelta
    with W.configurator_window(window) as dsn:
        login, has_password, expires, left = role(admin)
        assert login and has_password and expires and timedelta(minutes=9) < left <= timedelta(minutes=10)
        with psycopg.connect(Path(dsn).read_text(), autocommit=True) as c:
            assert c.execute('select session_user').fetchone()[0] == W.CONFIGURATOR
    assert closed(admin) and not Path(dsn).exists() and not Path(dsn).parent.exists()


def test_sigterm_inside_the_window_closes_it(window, admin, tmp_path):
    script = tmp_path / 'term.py'
    script.write_text(f"""import os, signal, sys, time
sys.path.insert(0, {str(ROOT / 'scripts/deploy')!r})
import configurator_window as W
with W.configurator_window({str(window)!r}) as dsn:
    print('OPEN', flush=True)
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)
    print('NOT REACHED', flush=True)
""")
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                          env={**os.environ, 'PYTHONPATH': os.environ.get('PYTHONPATH', '') + os.pathsep + str(ROOT / 'packages/eios-core/src')})
    assert 'OPEN' in done.stdout and 'NOT REACHED' not in done.stdout and done.returncode == 128 + 15, (done.stdout, done.stderr)
    assert closed(admin)


def test_a_lost_bootstrap_connection_closes_over_a_fresh_one(window, admin):
    with pytest.raises(RuntimeError, match='inside'):
        with W.configurator_window(window):
            # The window's own bootstrap session dies (a PostgreSQL restart, a network cut): the close reconnects once.
            admin.execute(f"""select pg_terminate_backend(pid) from pg_stat_activity where usename='nexloop_bootstrap' and pid<>pg_backend_pid()
                and backend_type='client backend'""")
            raise RuntimeError('inside')
    assert closed(admin)


def test_a_close_that_cannot_happen_is_reported_and_the_next_window_heals_the_lapsed_one(window, admin, monkeypatch):
    real_connect = psycopg.connect
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) > 1:  # the window's own connection works; the fresh one for the close does not
            raise psycopg.OperationalError('bootstrap unavailable')
        return real_connect(*a, **kw)
    monkeypatch.setattr(psycopg, 'connect', flaky)
    with pytest.raises(W.WindowNotClosed):
        with W.configurator_window(window):
            admin.execute(f"""select pg_terminate_backend(pid) from pg_stat_activity where usename='nexloop_bootstrap' and pid<>pg_backend_pid()
                and backend_type='client backend'""")
    monkeypatch.undo()
    login, has_password, expires, left = role(admin)
    assert login and has_password and expires and left.total_seconds() > 0  # left open - but only until its VALID UNTIL
    # A window still valid is another window: never taken over.
    with pytest.raises(ValueError, match='window is open'):
        with W.configurator_window(window):
            pass
    # Once lapsed it is stale: the next window closes it first, then opens (and closes) its own.
    admin.execute(f"alter role {W.CONFIGURATOR} valid until '2000-01-01'")
    with W.configurator_window(window) as dsn:
        assert role(admin)[0] and Path(dsn).exists()
    assert closed(admin)


def advisory_locks(admin):
    return admin.execute("select count(*) from pg_locks where locktype='advisory'").fetchone()[0]


def test_a_second_window_is_refused_at_once_and_the_first_is_untouched(window, admin):
    """L6 window-hardening review 1.1: one window at a time (a session advisory lock held from the decision to the close). A second
    window started meanwhile is refused at once - it neither overwrites the first one's password nor closes it from its own finally."""
    with W.configurator_window(window) as dsn:
        with pytest.raises(ValueError, match='another configurator window is in use'):
            with W.configurator_window(window):
                pass
        assert role(admin)[0] and advisory_locks(admin) == 1
        with psycopg.connect(Path(dsn).read_text(), autocommit=True) as c:  # the first window's login still works
            assert c.execute('select session_user').fetchone()[0] == W.CONFIGURATOR
    assert closed(admin) and advisory_locks(admin) == 0


def test_the_lock_is_released_on_an_error_and_on_sigterm(window, admin, tmp_path):
    with pytest.raises(RuntimeError):
        with W.configurator_window(window):
            raise RuntimeError('inside')
    assert closed(admin) and advisory_locks(admin) == 0
    test_sigterm_inside_the_window_closes_it(window, admin, tmp_path)  # a killed window's session ends: its lock with it
    assert advisory_locks(admin) == 0
    with W.configurator_window(window):  # the next window opens at once
        assert advisory_locks(admin) == 1
    assert closed(admin) and advisory_locks(admin) == 0
