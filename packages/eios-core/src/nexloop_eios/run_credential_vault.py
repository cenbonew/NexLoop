"""Private PREPARED-first bearer storage; no database authority is conferred."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import uuid
import time


class VaultUnavailable(RuntimeError):
    def __init__(self): super().__init__('run_vault_unavailable')


@dataclass(frozen=True, repr=False)
class VaultRecord:
    message_key: str
    run_id: str
    request_id: str
    issuance_nonce: str
    assignment_digest: str
    state: str
    token: str = field(repr=False)
    expires_at: str | None = None

    def __repr__(self): return '<PrivateRunVaultRecord>'

    @property
    def token_digest(self): return hashlib.sha256(self.token.encode()).hexdigest()


class RunCredentialVault:
    """Uses an existing private directory. Caller must keep it outside runtime."""
    def __init__(self, root, *, lock_timeout=1.0):
        self._fd = -1
        if type(lock_timeout) not in (int, float) or not 0.01 <= lock_timeout <= 5: raise VaultUnavailable()
        self._lock_timeout = lock_timeout
        try:
            self._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self._fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError()
            self._path = os.path.abspath(root)
            self._identity = (info.st_dev, info.st_ino)
        except Exception:
            self.close()
            raise VaultUnavailable() from None

    def __repr__(self): return '<PrivateRunCredentialVault>'

    def close(self):
        if self._fd >= 0: os.close(self._fd)
        self._fd = -1

    def __enter__(self): return self
    def __exit__(self, *unused): self.close()

    def _check(self):
        info = os.stat(self._path, follow_symlinks=False)
        if self._fd < 0 or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._identity or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()

    @staticmethod
    def message_key(tenant_id, world, message_id):
        if str(uuid.UUID(tenant_id)) != tenant_id or world != 'real' or not re.fullmatch('[0-9a-f]{64}', message_id): raise VaultUnavailable()
        return hashlib.sha256(json.dumps([tenant_id, world, message_id], separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _hex(value):
        if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None: raise ValueError()
        return value

    @contextmanager
    def _locked(self, key):
        lock = -1
        try:
            self._hex(key); self._check()
            # Separate exclusive creation from opening an existing lock.
            # Concurrent non-exclusive O_CREAT|O_NOFOLLOW opens may
            # report ENOENT before either contender obtains the lock.
            try:
                lock = os.open(key + '.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self._fd)
            except FileExistsError:
                lock = os.open(key + '.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1: raise ValueError()
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise ValueError()
                    time.sleep(min(0.01, remaining))
            self._check()
            current = os.stat(key + '.lock', dir_fd=self._fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino): raise ValueError()
            yield
            self._check()
            current = os.stat(key + '.lock', dir_fd=self._fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino): raise ValueError()
        except Exception: raise VaultUnavailable() from None
        finally:
            if lock >= 0: os.close(lock)

    def _load(self, key):
        try: fd = os.open(key + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError: return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or not 0 < info.st_size <= 8192: raise ValueError()
            raw = os.read(fd, 8193)
            def pairs(items):
                value = {}
                for name, item in items:
                    if name in value: raise ValueError()
                    value[name] = item
                return value
            def constant(unused): raise ValueError()
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
            if type(value) is not dict or any(type(value[name]) is not str for name in value if name != 'expires_at'): raise ValueError()
            if set(value) != {'message_key','run_id','request_id','issuance_nonce','assignment_digest','state','token','expires_at'}: raise ValueError()
            if value['message_key'] != key or str(uuid.UUID(value['run_id'])) != value['run_id'] or value['request_id'] != 'message-' + key: raise ValueError()
            self._hex(value['assignment_digest']); self._hex(value['issuance_nonce'])
            if type(value['token']) is not str or re.fullmatch('[0-9a-f]{96}', value['token']) is None: raise ValueError()
            if value['state'] not in ('prepared','issued','requires_governed_replan'): raise ValueError()
            if value['state'] == 'prepared' and value['expires_at'] is not None: raise ValueError()
            if value['state'] != 'prepared': self._deadline(value['expires_at'])
            return VaultRecord(**value)
        finally: os.close(fd)

    @staticmethod
    def _deadline(value):
        from datetime import datetime
        if type(value) is not str or len(value) > 64: raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0: raise ValueError()

    def _write(self, key, record):
        from dataclasses import asdict
        temporary = key + '.' + secrets.token_hex(16) + '.tmp'
        fd = -1
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
            payload = json.dumps(asdict(record), separators=(',', ':'), sort_keys=True).encode()
            with os.fdopen(fd, 'wb', closefd=True) as stream:
                fd = -1
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            self._check()
            os.rename(temporary, key + '.json', src_dir_fd=self._fd, dst_dir_fd=self._fd)
            os.fsync(self._fd)
        finally:
            if fd >= 0: os.close(fd)
            try: os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError: pass

    def prepare(self, *, message_key, assignment_digest):
        with self._locked(message_key):
            self._hex(assignment_digest)
            record = self._load(message_key)
            if record is not None:
                if record.assignment_digest != assignment_digest: raise VaultUnavailable()
                return record
            record = VaultRecord(message_key, str(uuid.uuid4()), 'message-' + message_key,
                secrets.token_hex(32), assignment_digest, 'prepared', secrets.token_hex(48))
            self._write(message_key, record)
            return record

    def read(self, message_key):
        with self._locked(message_key):
            record = self._load(message_key)
            if record is None: raise VaultUnavailable()
            return record

    def mark_issued(self, *, message_key, run_id, token_digest, expires_at):
        with self._locked(message_key):
            self._deadline(expires_at)
            record = self._load(message_key)
            if record is None or record.run_id != run_id or record.token_digest != token_digest: raise VaultUnavailable()
            if record.state != 'prepared':
                if record.expires_at != expires_at: raise VaultUnavailable()
                return record
            from dataclasses import replace
            record = replace(record, state='issued', expires_at=expires_at)
            self._write(message_key, record)
            return record

    def mark_expired(self, message_key):
        with self._locked(message_key):
            record = self._load(message_key)
            if record is None or record.state == 'prepared': raise VaultUnavailable()
            from datetime import UTC, datetime
            if datetime.fromisoformat(record.expires_at) > datetime.now(UTC): raise VaultUnavailable()
            from dataclasses import replace
            record = replace(record, state='requires_governed_replan')
            self._write(message_key, record)
            return record
