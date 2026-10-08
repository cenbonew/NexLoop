"""Offline private-file evidence, not PG issuance or relay acceptance evidence."""
from datetime import UTC, datetime, timedelta
import os
import uuid
import pytest
from nexloop_eios.run_credential_vault import RunCredentialVault, VaultUnavailable

@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / 'private-vault'; root.mkdir(mode=0o700)
    return root

def key(): return RunCredentialVault.message_key(str(uuid.uuid4()), 'real', 'a'*64)

def test_prepared_reopen_same_run_and_secret(private_root):
    message = key()
    with RunCredentialVault(private_root) as vault:
        initial = vault.prepare(message_key=message, assignment_digest='b'*64)
        assert initial.state == 'prepared'
        assert initial.token not in repr(initial) and initial.token not in repr(vault)
    with RunCredentialVault(private_root) as vault:
        recovered = vault.prepare(message_key=message, assignment_digest='b'*64)
        assert recovered == initial
        assert os.stat(private_root / (message+'.json')).st_mode & 0o777 == 0o600
        with pytest.raises(VaultUnavailable, match='^run_vault_unavailable$'):
            vault.prepare(message_key=message, assignment_digest='c'*64)

def test_commit_response_replay_preserves_first_deadline(private_root):
    message = key(); expiry = (datetime.now(UTC)+timedelta(seconds=100)).isoformat()
    with RunCredentialVault(private_root) as vault:
        initial = vault.prepare(message_key=message, assignment_digest='b'*64)
    with RunCredentialVault(private_root) as vault:
        issued = vault.mark_issued(message_key=message, run_id=initial.run_id, token_digest=initial.token_digest, expires_at=expiry)
        assert issued.state == 'issued' and issued.token == initial.token
        assert vault.mark_issued(message_key=message, run_id=initial.run_id, token_digest=initial.token_digest, expires_at=expiry) == issued
        with pytest.raises(VaultUnavailable):
            vault.mark_issued(message_key=message, run_id=initial.run_id, token_digest=initial.token_digest, expires_at=(datetime.now(UTC)+timedelta(seconds=200)).isoformat())

def test_expiry_keeps_original_run_requires_governed_replan(private_root):
    message = key()
    with RunCredentialVault(private_root) as vault:
        initial = vault.prepare(message_key=message, assignment_digest='b'*64)
        vault.mark_issued(message_key=message, run_id=initial.run_id, token_digest=initial.token_digest, expires_at=(datetime.now(UTC)-timedelta(seconds=1)).isoformat())
        expired = vault.mark_expired(message)
        assert expired.state == 'requires_governed_replan'
        assert vault.prepare(message_key=message, assignment_digest='b'*64) == expired

@pytest.mark.parametrize('mode', [0o755, 0o777, 0o750])
def test_unprivate_root_rejected(private_root, mode):
    private_root.chmod(mode)
    with pytest.raises(VaultUnavailable): RunCredentialVault(private_root)

def test_record_symlink_and_corruption_rejected(private_root, tmp_path):
    message = key(); target = tmp_path/'sentinel'; target.write_text('synthetic private sentinel')
    (private_root/(message+'.json')).symlink_to(target)
    with RunCredentialVault(private_root) as vault:
        with pytest.raises(VaultUnavailable): vault.prepare(message_key=message, assignment_digest='b'*64)
    (private_root/(message+'.json')).unlink()
    (private_root/(message+'.json')).write_text('{}'); (private_root/(message+'.json')).chmod(0o600)
    with RunCredentialVault(private_root) as vault:
        with pytest.raises(VaultUnavailable): vault.read(message)

def test_live_directory_replacement_rejected(private_root):
    with RunCredentialVault(private_root) as vault:
        private_root.rename(private_root.with_name('old-vault'))
        private_root.mkdir(mode=0o700)
        with pytest.raises(VaultUnavailable): vault.prepare(message_key=key(), assignment_digest='b'*64)

def test_record_and_lock_unprivate_rejected(private_root):
    message = key()
    with RunCredentialVault(private_root) as vault:
        vault.prepare(message_key=message, assignment_digest='b'*64)
        (private_root/(message+'.json')).chmod(0o644)
        with pytest.raises(VaultUnavailable): vault.read(message)
        (private_root/(message+'.json')).chmod(0o600)
        (private_root/(message+'.lock')).chmod(0o644)
        with pytest.raises(VaultUnavailable): vault.read(message)

def test_sigkill_after_prepared_reopens_exact_record(private_root):
    import subprocess, sys
    message = key()
    program = '''import os,signal,sys
from nexloop_eios.run_credential_vault import RunCredentialVault
with RunCredentialVault(sys.argv[1]) as vault:
    vault.prepare(message_key=sys.argv[2], assignment_digest='b'*64)
    os.kill(os.getpid(), signal.SIGKILL)
'''
    result = subprocess.run([sys.executable, '-c', program, str(private_root), message], capture_output=True)
    assert result.returncode == -9 and result.stdout == b'' and result.stderr == b''
    with RunCredentialVault(private_root) as vault:
        original = vault.read(message)
        assert vault.prepare(message_key=message, assignment_digest='b'*64) == original
        assert original.state == 'prepared'


def test_two_independent_processes_prepare_one_record(private_root):
    import subprocess, sys
    message = key()
    program = '''import json,sys
from nexloop_eios.run_credential_vault import RunCredentialVault,VaultUnavailable
print('ready',flush=True)
if sys.stdin.buffer.read(1)!=b'x':sys.exit(2)
try:
    with RunCredentialVault(sys.argv[1]) as vault:
        record=vault.prepare(message_key=sys.argv[2], assignment_digest='b'*64)
        print(record.run_id)
except VaultUnavailable as error:
    cause=error.__context__
    print(json.dumps({'error':'run_vault_unavailable','cause_type':type(cause).__name__,
        'cause_errno':getattr(cause,'errno',None)}),file=sys.stderr)
    sys.exit(1)
'''
    processes = [subprocess.Popen([sys.executable, '-c', program, str(private_root), message], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        assert all(process.stdout.readline() == b'ready\n' for process in processes)
        for process in processes:
            process.stdin.write(b'x');process.stdin.flush()
        results = [process.communicate(timeout=10) for process in processes]
        diagnostics = [(process.returncode, stderr.decode()) for process, (stdout, stderr) in zip(processes, results)]
        assert all(process.returncode == 0 for process in processes), diagnostics
        assert results[0][0] == results[1][0] and all(stderr == b'' for stdout, stderr in results)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill();process.communicate(timeout=5)

    with RunCredentialVault(private_root) as vault:
        record = vault.read(message)
        assert results[0][0].decode().strip() == record.run_id
        assert all(record.token.encode() not in stdout for stdout, stderr in results)

@pytest.mark.parametrize('content', ['{"token":"first","token":"second"}', '{"state":"prepared","state":"issued"}', '{"token":NaN}', '[]'])
def test_duplicate_nonfinite_or_nonobject_record_rejected(private_root, content):
    message = key(); path=private_root/(message+'.json'); path.write_text(content); path.chmod(0o600)
    with RunCredentialVault(private_root) as vault:
        with pytest.raises(VaultUnavailable): vault.read(message)


def test_paused_lock_owner_is_bounded(private_root):
    import subprocess, sys, time
    message=key()
    program='''import os,fcntl,sys,time
fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
time.sleep(30)
'''
    process=subprocess.Popen([sys.executable,'-c',program,str(private_root/(message+'.lock'))],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        assert process.stdout.readline()==b'locked\n'
        started=time.monotonic()
        with RunCredentialVault(private_root,lock_timeout=0.05) as vault:
            with pytest.raises(VaultUnavailable,match='^run_vault_unavailable$'):
                vault.prepare(message_key=message,assignment_digest='b'*64)
        assert 0.04 <= time.monotonic()-started < 1
        assert not (private_root/(message+'.json')).exists()
    finally:
        process.kill(); process.communicate(timeout=5)


def test_existing_lock_symlink_rejected(private_root,tmp_path):
    message=key();target=tmp_path/'foreign-lock';target.write_bytes(b'');target.chmod(0o600)
    (private_root/(message+'.lock')).symlink_to(target)
    with RunCredentialVault(private_root) as vault:
        with pytest.raises(VaultUnavailable,match='^run_vault_unavailable$'):
            vault.prepare(message_key=message,assignment_digest='b'*64)
    assert not (private_root/(message+'.json')).exists()


def test_waited_lock_path_replacement_rejected(private_root,monkeypatch):
    import fcntl,subprocess,sys,threading
    from concurrent.futures import ThreadPoolExecutor
    message=key();program='''import fcntl,os,sys
path=sys.argv[1]
fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
assert sys.stdin.buffer.read(1)==b'x'
os.rename(path,path+'.old')
replacement=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
os.close(replacement);os.close(fd)
'''
    process=subprocess.Popen([sys.executable,'-c',program,str(private_root/(message+'.lock'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    original=fcntl.flock;contended=threading.Event()
    def observed(*arguments):
        try:return original(*arguments)
        except BlockingIOError:contended.set();raise
    monkeypatch.setattr(fcntl,'flock',observed)
    try:
        assert process.stdout.readline()==b'locked\n'
        with RunCredentialVault(private_root) as vault,ThreadPoolExecutor(max_workers=1) as threads:
            pending=threads.submit(vault.prepare,message_key=message,assignment_digest='b'*64)
            assert contended.wait(.5)
            process.stdin.write(b'x');process.stdin.flush()
            with pytest.raises(VaultUnavailable,match='^run_vault_unavailable$'):pending.result(timeout=2)
        stdout,stderr=process.communicate(timeout=5)
        assert process.returncode==0 and stdout==stderr==b''
        assert not (private_root/(message+'.json')).exists()
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)


def test_existing_lock_hardlink_rejected(private_root,tmp_path):
    message=key();target=tmp_path/'linked-lock';target.write_bytes(b'');target.chmod(0o600)
    os.link(target,private_root/(message+'.lock'))
    with RunCredentialVault(private_root) as vault:
        with pytest.raises(VaultUnavailable,match='^run_vault_unavailable$'):
            vault.prepare(message_key=message,assignment_digest='b'*64)
    assert not (private_root/(message+'.json')).exists()


def test_waited_lock_unlink_rejected(private_root,monkeypatch):
    import fcntl,subprocess,sys,threading
    from concurrent.futures import ThreadPoolExecutor
    message=key();program='''import fcntl,os,sys
path=sys.argv[1]
fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
assert sys.stdin.buffer.read(1)==b'x'
os.unlink(path);os.close(fd)
'''
    process=subprocess.Popen([sys.executable,'-c',program,str(private_root/(message+'.lock'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    original=fcntl.flock;contended=threading.Event()
    def observed(*arguments):
        try:return original(*arguments)
        except BlockingIOError:contended.set();raise
    monkeypatch.setattr(fcntl,'flock',observed)
    try:
        assert process.stdout.readline()==b'locked\n'
        with RunCredentialVault(private_root) as vault,ThreadPoolExecutor(max_workers=1) as threads:
            pending=threads.submit(vault.prepare,message_key=message,assignment_digest='b'*64)
            assert contended.wait(.5)
            process.stdin.write(b'x');process.stdin.flush()
            with pytest.raises(VaultUnavailable,match='^run_vault_unavailable$'):pending.result(timeout=2)
        stdout,stderr=process.communicate(timeout=5)
        assert process.returncode==0 and stdout==stderr==b''
        assert not (private_root/(message+'.json')).exists()
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)
