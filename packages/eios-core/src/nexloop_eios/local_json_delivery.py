"""Local real JSON export delivery, with current EIOS effect authority.

The export is an actual private filesystem product, not a formal Artifact object.
Its journal contains technical idempotency/product evidence only. No HTTP input
chooses a path, source identity, endpoint, shell command or business database row.
Requires unregistered candidate migration 0051; never migrates on startup.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import argparse
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import threading
from urllib.parse import urlsplit
import uuid

from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.authorization import _identity
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_execution import EffectExecutionPort, SEND, QUERY
from nexloop_eios.effect_provider import EffectProviderConfiguration, HttpEffectProvider
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text

PROTOCOL = 'nexloop-local-json-delivery-v1'
FORMAT = 'nexloop.json-export.v1'
MAXIMUM = 131072


class DeliveryUnavailable(PermissionError):
    def __init__(self): super().__init__('local_delivery_unavailable')


class DeliveryConflict(ValueError):
    def __init__(self): super().__init__('local_delivery_conflict')


def _intent(value):
    if type(value) is not str or str(uuid.UUID(value)) != value: raise DeliveryUnavailable()
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{64}', value) is None: raise DeliveryUnavailable()
    return value


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise DeliveryUnavailable()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda unused: (_ for _ in ()).throw(DeliveryUnavailable()))


def _parameters(value):
    # Frozen service.request:1 already accepts parameters.message. Service owns
    # the export format/title; caller cannot add a path or alternative contract.
    if (type(value) is not dict or set(value) != {'message'} or type(value['message']) is not str
        or not 1 <= len(value['message'].encode()) <= 65536 or '\x00' in value['message']
        or len(canonical_payload(value).encode()) > MAXIMUM - 1024):
        raise DeliveryUnavailable()
    return value


class DeliveryAuthorityPort:
    """Trusted server-only port, fixed independent executor, real current proof.

    Private metadata is admitted only by 0051 for this executor's committed
    attempt/owned lease. The service never loads a caller-supplied Run digest.
    """
    def __init__(self, backend, credential_file, profile_digest):
        self.backend, self.credential_file = backend, credential_file
        self.profile_digest = _digest(profile_digest)

    def _port(self):
        service = self.backend.authenticate(read_private_text(self.credential_file, maximum=4096), world='real')
        return EffectExecutionPort(self.backend._pool, service._session, self.backend._signer)

    def _envelope(self, port, verb, target, intent_id, **parameters):
        proof, unused = port._proof(port.session, target)
        name, version = target.removeprefix('eios:action:').rsplit(':', 1)
        definition, capability = PostgresActionDefinitionReader(port.pool, port.session, port.signer).get(name, int(version))
        payload = canonical_payload({'verb': verb, 'intent_id': _intent(intent_id),
            'provider_profile_digest': self.profile_digest, **parameters})
        claims = {'protocol': PROTOCOL, 'key_id': port.signer.key_id, **proof,
            'definition': definition.model_dump(mode='json'), 'capability': capability.model_dump(mode='json'),
            'parameters_digest': hashlib.sha256(payload.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(port.signer.material, (PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()
        return text, signature, payload

    def _execute(self, db, port, verb, target, intent_id, *, wire=None, **parameters):
        text, signature, payload = wire or self._envelope(port, verb, target, intent_id, **parameters)
        if verify_application_role(db) != 'nexloop_action_worker': raise DeliveryUnavailable()
        return db.execute('select authz.nexloop_local_delivery_authority(%s,%s,%s,%s,%s)',
            (port.session.token_digest, 'real', text, signature, payload)).fetchone()[0]

    def query(self, intent_id):
        try:
            with self.backend._lock:
                self.backend._assert_open()
                port = self._port()
                with port.pool.connection() as db, db.transaction():
                    return self._execute(db, port, 'query', QUERY, intent_id)
        except Exception: raise DeliveryUnavailable() from None

    @contextmanager
    def delivery(self, intent_id, digest, parameters):
        try:
            _parameters(parameters); _digest(digest)
            text = canonical_payload(parameters)
            if hashlib.sha256(text.encode()).hexdigest() != digest: raise DeliveryUnavailable()
            with self.backend._lock:
                self.backend._assert_open()
                port = self._port()
                with port.pool.connection() as db, db.transaction():
                    metadata = self._execute(db, port, 'resolve', QUERY, intent_id)
                with port.pool.connection() as db, db.transaction():
                    run = _identity(db, metadata['_run_digest'], 'real')
                if run.run_context is None or run.run_context.run_id != metadata['origin_run_id']:
                    raise DeliveryUnavailable()
                proof, unused = port._proof(run, SEND)
                args = dict(origin_proof=proof, payload_digest=digest, parameters_text=text,
                    effect_fence=metadata['effect_fence'], attempt_revision=metadata['attempt_revision'])
                wire = self._envelope(port, 'deliver', SEND, intent_id, **args)
                with port.pool.connection() as db, db.transaction():
                    actual = self._execute(db, port, 'deliver', SEND, intent_id, wire=wire)
                    # Authoritative frozen parameters, never an HTTP identity or
                    # material chosen solely by the server's broader privilege.
                    if actual['parameters'] != parameters or actual['payload_digest'] != digest:
                        raise DeliveryUnavailable()
                    yield actual
                    # This reuses original source proof: expiry while rendering
                    # must fail, rather than minting a fresh permit after effect.
                    self._execute(db, port, 'deliver', SEND, intent_id, wire=wire)
        except DeliveryConflict: raise
        except Exception: raise DeliveryUnavailable() from None


    def recover_one(self, store, manifest):
        """No HTTP/POST. Exact original intent; genuine Governor then file effect.

        An active original claim yields IN_PROGRESS and recovery waits. Every
        failure remains accepted/unknown; it never invents a business success.
        """
        try:
            intent = _intent(manifest['intent_id'])
            digest = _digest(manifest['payload_digest'])
            parameters = _parameters(manifest['parameters'])
            if manifest['provider_profile_digest'] != self.profile_digest:
                raise DeliveryUnavailable()
            with self.backend._lock:
                self.backend._assert_open()
                port = self._port()
                # Nested original QUERY claim is bound to this exact existing
                # attempt by 0051; profile failure rolls back its lease reclaim.
                text, sig, payload = port._signed('claim', target=QUERY, intent_id=intent, lease_seconds=30)
                with port.pool.connection() as db, db.transaction():
                    self._execute(db, port, 'recover_claim', QUERY, intent,
                        execution_claim={'text': text, 'signature': sig, 'payload': payload})
                with port.pool.connection() as db, db.transaction():
                    metadata = self._execute(db, port, 'resolve', QUERY, intent)
                origin = port._origin_proof(metadata)
                args = dict(origin_proof=origin, payload_digest=digest,
                    parameters_text=canonical_payload(parameters), effect_fence=metadata['effect_fence'],
                    attempt_revision=metadata['attempt_revision'])
                authority = self
                class OriginalGovernorPort(EffectExecutionPort):
                    def _execute(self, db, verb, *, target, **arguments):
                        if verb != 'finalize' or target != SEND: raise DeliveryUnavailable()
                        return authority._execute(db, port, 'recover_reserve', SEND, intent, **arguments)
                governed = OriginalGovernorPort(port.pool, port.session, port.signer)
                # Genuine original Governor callback and nested reservation in
                # one transaction, persisted BEFORE any product generation.
                with port.pool.connection() as db, db.transaction():
                    reserved, unused_claim = governed._reserve_in_transaction(db, metadata, 'finalize',
                        lease_seconds=30, **args)
                args['attempt_revision'] = reserved['attempt_revision']
                wire = self._envelope(port, 'recover_deliver', SEND, intent, **args)
                with port.pool.connection() as db, db.transaction():
                    actual = self._execute(db, port, 'recover_deliver', SEND, intent, wire=wire)
                    if actual['parameters'] != parameters or actual['payload_digest'] != digest:
                        raise DeliveryUnavailable()
                    store.deliver(intent, digest, actual['parameters'], self.profile_digest)
                    self._execute(db, port, 'recover_deliver', SEND, intent, wire=wire)
                # Real QUERY observation and existing atomic dual-authority
                # finalizer. Revoke/expiry cannot become a fabricated success.
                port.record_effect_observation(intent_id=intent, fence=metadata['effect_fence'],
                    provider_profile_digest=self.profile_digest, provider_payload_digest=digest,
                    provider_state='fulfilled', provider_reference='json-export:' + intent)
            return True
        except Exception:
            return False


class BoundedDeliveryRecovery:
    """Independent bounded loop. GET has no access to this operation."""
    def __init__(self, authority, store):
        if type(authority) is not DeliveryAuthorityPort: raise DeliveryUnavailable()
        self.authority, self.store = authority, store
        self.stop = threading.Event()
        self.thread = None
        self.owner_fd = None

    def run_once(self):
        recovered = 0
        for manifest in self.store.recovery_candidates(self.authority.profile_digest, limit=4):
            if self.stop.is_set(): break
            recovered += int(self.authority.recover_one(self.store, manifest))
        return recovered

    def start(self):
        if self.thread is not None: raise DeliveryUnavailable()
        self.store._check()
        fd = os.open('.service-owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.store.fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise DeliveryUnavailable()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(fd); raise DeliveryUnavailable() from None
        self.owner_fd = fd
        def loop():
            while not self.stop.is_set():
                try: self.run_once()
                except Exception: pass  # Fixed silent failure; never logs credential/error text.
                self.stop.wait(2)
        self.thread = threading.Thread(target=loop, name='local-json-recovery', daemon=False)
        try: self.thread.start()
        except Exception:
            os.close(self.owner_fd); self.owner_fd = None
            self.thread = None
            raise DeliveryUnavailable() from None

    def close(self):
        self.stop.set()
        if self.thread is not None: self.thread.join()
        if self.owner_fd is not None:
            os.close(self.owner_fd); self.owner_fd = None


class JsonExportStore:
    """Single fixed root; per-intent flock, immutable atomic files, fsync.

    Manifest is durable before product. A crash after product rename is recovered
    by GET verifying its exact deterministic bytes. No simulated fulfillment flag.
    """
    def __init__(self, root):
        self.root = Path(root).absolute()
        # Operator creates this private root; startup never creates a guessed path.
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(self.fd); raise DeliveryUnavailable()
        self.identity = (info.st_dev, info.st_ino)

    def close(self): os.close(self.fd)

    def _check(self):
        info = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid() or (info.st_dev, info.st_ino) != self.identity:
            raise DeliveryUnavailable()

    @contextmanager
    def _lock(self, intent_id, *, readonly=False):
        self._check(); _intent(intent_id)
        flags = (os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT) | os.O_NOFOLLOW | os.O_NONBLOCK
        try: fd = os.open(intent_id + '.lock', flags, 0o600, dir_fd=self.fd)
        except FileNotFoundError:
            if not readonly: raise
            # No existing writer lock: query reads an immutable snapshot only.
            # A concurrent first publication may yield unavailable/accepted,
            # never create a lock or mutate to repair that snapshot.
            yield
            self._check()
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise DeliveryUnavailable()
            # No unbounded lock wait; HTTP caller can query after contention.
            fcntl.flock(fd, (fcntl.LOCK_SH if readonly else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            yield
            self._check()
        finally: os.close(fd)

    def _read(self, name, *, pending=False):
        try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError: return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink not in ({1, 2} if pending else {1}):
                raise DeliveryUnavailable()
            data = stream.read(MAXIMUM + 1)
            if not data or len(data) > MAXIMUM: raise DeliveryUnavailable()
            return data

    def _recover_link(self, name, expected, *, cleanup=True):
        # Caller first validates original-authority digest and full exact bytes.
        # Recover only a crash's one private same-root staging hardlink. Never
        # permit arbitrary hardlinks or remove unknown entries.
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if info.st_nlink == 1: return
        if info.st_nlink != 2 or self._read(name, pending=True) != expected: raise DeliveryUnavailable()
        entries = os.listdir(self.fd)
        if len(entries) > 10000: raise DeliveryUnavailable()
        matched = []
        for entry in entries:
            if re.fullmatch(r'.pending-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', entry) is None:
                continue
            other = os.stat(entry, dir_fd=self.fd, follow_symlinks=False)
            if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                if (not stat.S_ISREG(other.st_mode) or other.st_uid != os.geteuid() or other.st_mode & 0o077
                    or other.st_nlink != 2 or self._read(entry, pending=True) != expected): raise DeliveryUnavailable()
                matched.append(entry)
        if len(matched) != 1: raise DeliveryUnavailable()
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        other = os.stat(matched[0], dir_fd=self.fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 2) or (other.st_dev, other.st_ino, other.st_nlink) != (info.st_dev, info.st_ino, 2):
            raise DeliveryUnavailable()
        if not cleanup: return  # Pure owned staging validation; GET never repairs.
        os.unlink(matched[0], dir_fd=self.fd); os.fsync(self.fd)
        if self._read(name) != expected: raise DeliveryUnavailable()

    def _immutable(self, name, body):
        existing = self._read(name, pending=True)
        if existing is not None:
            if existing != body: raise DeliveryConflict()
            self._recover_link(name, body)
            return
        temporary = '.pending-' + str(uuid.uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
            # Atomic no-replace publication; an orphan temp is never fulfilled.
            try: os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            except FileExistsError:
                if self._read(name) != body: raise DeliveryConflict()
            os.unlink(temporary, dir_fd=self.fd); os.fsync(self.fd)
        finally:
            try: os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError: pass

    @staticmethod
    def _product(intent_id, digest, parameters):
        return (canonical_payload({'format': FORMAT, 'intent_id': intent_id, 'payload_digest': digest,
            'document': {'title': 'NexLoop service delivery', 'body': _parameters(parameters)['message']}}) + '\n').encode()

    def deliver(self, intent_id, digest, parameters, profile_digest=None):
        _digest(digest)
        if hashlib.sha256(canonical_payload(_parameters(parameters)).encode()).hexdigest() != digest:
            raise DeliveryUnavailable()
        with self._lock(intent_id):
            product = self._product(intent_id, digest, parameters)
            manifest = canonical_payload({'intent_id': intent_id, 'payload_digest': digest,
                'product_sha256': hashlib.sha256(product).hexdigest(), 'product_size': len(product),
                **({'parameters': parameters, 'provider_profile_digest': _digest(profile_digest)} if profile_digest is not None else {})}).encode()
            self._immutable(intent_id + '.manifest.json', manifest)
            self._immutable(intent_id + '.export.json', product)
            return self._receipt(intent_id, digest, 'fulfilled')

    def recovery_candidates(self, profile_digest, *, limit=4):
        _digest(profile_digest)
        if type(limit) is not int or not 1 <= limit <= 4: raise DeliveryUnavailable()
        self._check()
        names = []
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if len(names) >= 10000: raise DeliveryUnavailable()
                names.append(entry.name)
        eligible = sorted(name for name in names if re.fullmatch(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.manifest\.json', name))
        cursor = getattr(self, '_recovery_cursor', '')
        ordered = [name for name in eligible if name > cursor] + [name for name in eligible if name <= cursor]
        result = []
        for name in ordered[:16]:
            self._recovery_cursor = name
            intent = name.removesuffix('.manifest.json')
            try:
                with self._lock(intent, readonly=True):
                    raw = self._read(name, pending=True)
                    manifest = _decode(raw)
                    if (type(manifest) is not dict or set(manifest) != {'intent_id', 'payload_digest',
                        'product_sha256', 'product_size', 'parameters', 'provider_profile_digest'}
                        or manifest['intent_id'] != intent or manifest['provider_profile_digest'] != profile_digest
                        or canonical_payload(manifest).encode() != raw): continue
                    parameters = _parameters(manifest['parameters'])
                    digest = _digest(manifest['payload_digest'])
                    product = self._product(intent, digest, parameters)
                    if (hashlib.sha256(canonical_payload(parameters).encode()).hexdigest() != digest
                        or type(manifest['product_size']) is not int or manifest['product_size'] != len(product)
                        or manifest['product_sha256'] != hashlib.sha256(product).hexdigest()
                        or self._read(intent + '.export.json', pending=True) is not None): continue
                    self._recover_link(name, raw, cleanup=False)
                    result.append(manifest)
            except Exception: continue
            if len(result) == limit: break
        return result

    @staticmethod
    def _receipt(intent_id, digest, state):
        return {'intent_id': intent_id, 'payload_digest': digest, 'state': state,
            'provider_reference': None if state == 'not_found' else 'json-export:' + intent_id}

    def query(self, intent_id, expected_digest, profile_digest=None):
        _digest(expected_digest)
        with self._lock(intent_id, readonly=True):
            raw = self._read(intent_id + '.manifest.json', pending=True)
            product = self._read(intent_id + '.export.json', pending=True)
            if raw is None:
                if product is not None: raise DeliveryUnavailable()
                return self._receipt(intent_id, None, 'not_found')
            manifest = _decode(raw)
            if (type(manifest) is not dict or set(manifest) not in ({'intent_id', 'payload_digest', 'product_sha256', 'product_size'},
                    {'intent_id', 'payload_digest', 'product_sha256', 'product_size', 'parameters', 'provider_profile_digest'})
                or manifest['intent_id'] != intent_id or manifest['payload_digest'] != expected_digest
                or type(manifest['product_size']) is not int or not 1 <= manifest['product_size'] <= MAXIMUM
                or type(manifest['product_sha256']) is not str or re.fullmatch('[a-f0-9]{64}', manifest['product_sha256']) is None
                or canonical_payload(manifest).encode() != raw):
                raise DeliveryConflict()
            if 'parameters' in manifest:
                _parameters(manifest['parameters']); _digest(manifest['provider_profile_digest'])
                if (hashlib.sha256(canonical_payload(manifest['parameters']).encode()).hexdigest() != expected_digest
                    or profile_digest is not None and manifest['provider_profile_digest'] != _digest(profile_digest)):
                    raise DeliveryConflict()
            if product is None:
                self._recover_link(intent_id + '.manifest.json', raw, cleanup=False)
                return self._receipt(intent_id, expected_digest, 'accepted')
            value = _decode(product)
            if (type(value) is not dict or set(value) != {'format', 'intent_id', 'payload_digest', 'document'}
                or value['format'] != FORMAT or value['intent_id'] != intent_id or value['payload_digest'] != expected_digest
                or hashlib.sha256(product).hexdigest() != manifest['product_sha256']
                or type(manifest['product_size']) is not int or len(product) != manifest['product_size']):
                raise DeliveryUnavailable()
            document = value['document']
            if type(document) is not dict or set(document) != {'title', 'body'} or document['title'] != 'NexLoop service delivery':
                raise DeliveryUnavailable()
            parameters = _parameters({'message': document['body']})
            if hashlib.sha256(canonical_payload(parameters).encode()).hexdigest() != expected_digest:
                raise DeliveryUnavailable()
            self._recover_link(intent_id + '.manifest.json', raw, cleanup=False)
            self._recover_link(intent_id + '.export.json', product, cleanup=False)
            return self._receipt(intent_id, expected_digest, 'fulfilled')


@dataclass(frozen=True)
class DeliveryServerConfiguration:
    origin: str
    certificate_file: Path = field(repr=False)
    key_file: Path = field(repr=False)
    credential_file: Path = field(repr=False)
    request_seconds: float = 3.0

    def __post_init__(self):
        origin = urlsplit(self.origin)
        if (origin.scheme != 'https' or origin.hostname != '127.0.0.1' or origin.port is None
            or not 1 <= origin.port <= 65535 or origin.netloc != '127.0.0.1:' + str(origin.port)
            or origin.path or origin.query or origin.fragment or type(self.request_seconds) not in (int, float)
            or not 0 < self.request_seconds <= 10): raise DeliveryUnavailable()


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, handler)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False): self.shutdown_request(request); return
        try: super().process_request(request, address)
        except BaseException: self.slots.release(); raise
    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()
    def handle_error(self, request, client_address): pass


class _HeaderReader:
    def __init__(self, stream): self.stream, self.remaining = stream, 16384
    def readline(self, size=-1):
        line = self.stream.readline(min(size if size >= 0 else 16385, self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0: raise DeliveryUnavailable()
        return line
    def read(self, size=-1):
        if not 0 <= size <= MAXIMUM + 1024: raise DeliveryUnavailable()
        return self.stream.read(size)
    def close(self): self.stream.close()


def make_server(configuration, authority, store):
    if type(configuration) is not DeliveryServerConfiguration or type(authority) is not DeliveryAuthorityPort or type(store) is not JsonExportStore:
        raise DeliveryUnavailable()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Verify private regular service-owned inputs before OpenSSL loading. Snapshot
    # via /dev/fd prevents pathname replacement between validation and loading.
    from contextlib import ExitStack
    with ExitStack() as stack:
        descriptors = []
        for path in (configuration.certificate_file, configuration.key_file):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            stack.callback(os.close, fd); info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or not 1 <= info.st_size <= 32768:
                raise DeliveryUnavailable()
            descriptors.append(fd)
        context.load_cert_chain('/dev/fd/' + str(descriptors[0]), '/dev/fd/' + str(descriptors[1]))
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            self.request.settimeout(configuration.request_seconds)
            self.timer = threading.Timer(configuration.request_seconds, self._expire)
            self.timer.daemon = True; self.timer.start()
            try:
                self.request.do_handshake(); super().setup(); self.rfile = _HeaderReader(self.rfile)
            except BaseException: self.timer.cancel(); raise
        def _expire(self):
            import socket
            try: self.request.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        def finish(self):
            try: super().finish()
            finally: self.timer.cancel()
        def log_message(self, *unused): pass
        def send_error(self, code, message=None, explain=None): self._respond(code, {'error': 'local_delivery_unavailable'})
        def _respond(self, status, value):
            self.close_connection = True
            body = canonical_payload(value).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.send_header('Connection', 'close')
            self.end_headers(); self.wfile.write(body)
        def _authenticate(self):
            if (self.headers.get_all('Host') != [urlsplit(configuration.origin).netloc]
                or self.headers.get_all('Origin') is not None or len(self.headers.get_all('Authorization') or []) != 1
                or self.headers.get_all('Transfer-Encoding') is not None or self.headers.get_all('Content-Encoding') is not None):
                raise DeliveryUnavailable()
            expected = read_private_text(configuration.credential_file, maximum=4096)
            if re.fullmatch('[A-Za-z0-9._~-]{16,4096}', expected) is None or not hmac.compare_digest(self.headers['Authorization'], 'Bearer ' + expected):
                raise DeliveryUnavailable()
        def do_POST(self):
            try:
                self._authenticate()
                length = self.headers.get_all('Content-Length') or []
                if (self.path != '/v1/effects' or len(length) != 1 or not re.fullmatch('[0-9]{1,6}', length[0])
                    or not 1 <= int(length[0]) <= MAXIMUM + 1024
                    or self.headers.get_all('Content-Type') != ['application/json']): raise DeliveryUnavailable()
                raw = self.rfile.read(int(length[0]))
                if len(raw) != int(length[0]): raise DeliveryUnavailable()
                value = _decode(raw)
                if type(value) is not dict or set(value) != {'intent_id', 'payload_digest', 'parameters'}: raise DeliveryUnavailable()
                intent = _intent(value['intent_id']); digest = _digest(value['payload_digest'])
                if self.headers.get_all('Idempotency-Key') != [intent]: raise DeliveryUnavailable()
                with authority.delivery(intent, digest, value['parameters']) as actual:
                    receipt = store.deliver(intent, actual['payload_digest'], actual['parameters'], authority.profile_digest)
                self._respond(200, receipt)
            except DeliveryConflict: self._respond(409, {'error': 'local_delivery_conflict'})
            except Exception: self._respond(503, {'error': 'local_delivery_unavailable'})
        def do_GET(self):
            try:
                self._authenticate()
                if self.headers.get_all('Content-Length') not in (None, ['0']) or not self.path.startswith('/v1/effects/'):
                    raise DeliveryUnavailable()
                intent = _intent(self.path.removeprefix('/v1/effects/'))
                actual = authority.query(intent)
                receipt = store.query(intent, actual['payload_digest'], authority.profile_digest)
                self._respond(404 if receipt['state'] == 'not_found' else 200, receipt)
            except DeliveryConflict: self._respond(409, {'error': 'local_delivery_conflict'})
            except Exception: self._respond(503, {'error': 'local_delivery_unavailable'})
    server = _Server(('127.0.0.1', urlsplit(configuration.origin).port), Handler)
    try: server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    except BaseException: server.server_close(); raise
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local real governed JSON export service; no bootstrap/migration')
    for name in ('database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root', 'delivery-root',
                 'certificate-file', 'tls-key-file', 'provider-ca-file', 'provider-credential-file'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--signing-key-id', default='active')
    args = parser.parse_args(argv)
    server = store = recovery = None
    try:
        config = DeliveryServerConfiguration(args.origin, args.certificate_file, args.tls_key_file, args.provider_credential_file)
        profile = HttpEffectProvider(EffectProviderConfiguration(args.origin, credential_file=str(args.provider_credential_file),
            ca_file=str(args.provider_ca_file))).profile_digest
        with open_backend(database_url=read_private_text(args.database_url_file, maximum=8192), artifact_root=args.artifact_root,
            signing_key_file=args.signing_key_file, signing_key_id=args.signing_key_id) as backend:
            authority = DeliveryAuthorityPort(backend, args.service_credential_file, profile)
            authority._port()  # Current actual executor + role before listening.
            store = JsonExportStore(args.delivery_root)
            server = make_server(config, authority, store)
            recovery = BoundedDeliveryRecovery(authority, store)
            recovery.start()
            # Signal handler does not call shutdown from serve_forever's thread.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda unused_sig, unused_frame: threading.Thread(target=server.shutdown, daemon=True).start())
            try: server.serve_forever(poll_interval=.2)
            finally:
                recovery.close()
                server.server_close()  # Drain admitted handlers before Backend/PG/store close.
        return 0
    except Exception:
        print('{"error":"local_delivery_unavailable","product_ready":false}')
        return 1
    finally:
        if recovery is not None: recovery.close()
        if server is not None: server.server_close()
        if store is not None: store.close()


if __name__ == '__main__': raise SystemExit(main())
