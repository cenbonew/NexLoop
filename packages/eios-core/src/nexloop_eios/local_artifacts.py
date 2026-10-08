"""Private filesystem blobs; EIOS-authorized reads, no public/static paths.

Metadata reservation/finalization and GC claims remain the caller's PostgreSQL
responsibility. This adapter deliberately does not invent an in-memory index.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import errno
import hashlib
import heapq
import os
from pathlib import Path
import re
import secrets
import stat

from eios.authz.facts import AuthorizationFactQuery
from eios.authz.operations import Operation
from eios.authz.service import AuthorizationDecisionService
from eios.runtime.artifacts import MAX_ARTIFACT_BYTES, ArtifactRetentionTerminalProof


class ArtifactAccessDenied(PermissionError):
    pass


class ArtifactIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlobReference:
    tenant_id: str
    world: str
    artifact_id: str
    sha256: str
    size_bytes: int
    media_type: str
    encryption: str = 'none'

    def __post_init__(self):
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/~-]{0,254}', value) for value in (self.tenant_id, self.world)):
            raise ValueError('tenant/world binding required')
        if not re.fullmatch(r'[a-f0-9]{32}', self.artifact_id):
            raise ValueError('artifact identity must be an opaque server identifier')
        if not re.fullmatch(r'[a-f0-9]{64}', self.sha256):
            raise ValueError('invalid SHA-256')
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError('invalid artifact size')
        if (len(self.media_type) > 255 or not re.fullmatch(
                r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:; charset=[A-Za-z0-9._-]+)?',
                self.media_type) or self.encryption != 'none'):
            raise ValueError('invalid storage metadata')

    @property
    def namespace(self):
        return hashlib.sha256((self.tenant_id+'\0'+self.world).encode()).hexdigest()

    @property
    def resource_id(self):
        # Same bytes in different tenant/world must not share a permission target.
        return f'eios:artifact:{self.namespace}_{self.artifact_id}'


def _sync(fd):
    os.fsync(fd)


class LocalBlobStore:
    """Multi-process immutable store on a service-owned private directory.

    Only opaque, server-computed names reach filesystem APIs. O_NOFOLLOW and
    directory FDs reject symlink escapes. Cross-process flock serializes writers
    and GC; FULL file and parent-directory fsync precede a successful receipt.
    """
    def __init__(self, root: Path):
        root = Path(root).expanduser().absolute()
        if root.is_symlink():
            raise ValueError('artifact root cannot be a symlink')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self._root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        mode = os.fstat(self._root)
        if mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
            os.close(self._root)
            self._root = None
            raise PermissionError('artifact root must be service-owned and mode 0700')

    def close(self):
        if self._root is not None:
            os.close(self._root)
            self._root = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _namespace(self, ref):
        if self._root is None:
            raise RuntimeError('artifact store closed')
        try:
            os.mkdir(ref.namespace, mode=0o700, dir_fd=self._root)
            _sync(self._root)
        except FileExistsError:
            pass
        directory = os.open(ref.namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._root)
        try:
            mode = os.fstat(directory)
            if mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
                raise PermissionError('artifact namespace is not private')
            # Lock the stable private directory inode itself: no separate lock
            # file creation race and no replaceable lock-file pathname.
            fcntl.flock(directory, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(directory)

    @staticmethod
    def _read(directory, ref):
        fd = os.open(ref.artifact_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != ref.size_bytes:
                raise ArtifactIntegrityError('artifact size/type mismatch')
            payload = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(payload) != ref.size_bytes or hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError('artifact checksum mismatch')
        return payload

    def put(self, *, tenant_id, world, payload: bytes, media_type, artifact_id=None):
        if type(payload) is not bytes or len(payload) > MAX_ARTIFACT_BYTES:
            raise ValueError('artifact must be bounded bytes')
        # Retry reuses the ID reserved by PostgreSQL. Distinct logical artifacts
        # never share deletion/retention state merely because bytes match.
        ref = BlobReference(tenant_id, world, secrets.token_hex(16) if artifact_id is None else artifact_id,
                            hashlib.sha256(payload).hexdigest(), len(payload), media_type)
        with self._namespace(ref) as directory:
            temporary = '.tmp-'+secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    _sync(stream.fileno())
                try:
                    # Atomic create-if-absent; do not replace a corrupt collision.
                    os.link(temporary, ref.artifact_id, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    self._read(directory, ref)
                os.unlink(temporary, dir_fd=directory)
                _sync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return ref

    def read(self, ref: BlobReference):
        with self._namespace(ref) as directory:
            return self._read(directory, ref)

    def remove(self, ref: BlobReference):
        """Infrastructure primitive; only a verified PostgreSQL GC claim may call it."""
        with self._namespace(ref) as directory:
            try:
                self._read(directory, ref)
                os.unlink(ref.artifact_id, dir_fd=directory)
                _sync(directory)
            except FileNotFoundError:
                pass

    def collect_abandoned_temporary_files(self, ref: BlobReference, *, older_than: datetime):
        """Only abandoned temporary files; never infer final-blob liveness locally."""
        if older_than.tzinfo is None or older_than > datetime.now(UTC):
            raise ValueError('GC cutoff must be an aware past time')
        removed = 0
        with self._namespace(ref) as directory:
            for name in os.listdir(directory):
                if not re.fullmatch(r'\.tmp-[a-f0-9]{32}', name):
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < older_than.timestamp():
                    os.unlink(name, dir_fd=directory)
                    removed += 1
            if removed:
                _sync(directory)
        return removed

    def collect_temporary_page(self, ref: BlobReference, *, older_than, limit, after, guard):
        """Trusted coordinator only; bounded page, directory lock and live DB guard.

        Final blobs are excluded regardless of age or metadata state. The caller
        keeps its PostgreSQL authorization transaction open through fsync.
        """
        if (older_than.tzinfo is None or older_than>datetime.now(UTC)
                or type(limit) is not int or not 1<=limit<=100
                or type(after) is not str or (after and not re.fullmatch(r'\.tmp-[a-f0-9]{32}',after))
                or not callable(guard)):
            raise ValueError('invalid temporary cleanup page')
        removed=0
        with self._namespace(ref) as directory:
            guard()  # Recheck after a possibly long wait for a writer's flock.
            with os.scandir(directory) as entries:
                page=heapq.nsmallest(limit+1,(entry.name for entry in entries
                    if re.fullmatch(r'\.tmp-[a-f0-9]{32}',entry.name) and entry.name>after))
            exhausted=len(page)<=limit;page=page[:limit]
            try:
                for name in page:
                    guard()  # Current PG clock/fact epoch, including permit expiry.
                    try:
                        info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if (stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid()
                            and info.st_mtime<older_than.timestamp()):
                        os.unlink(name,dir_fd=directory);removed+=1
            finally:
                if removed:_sync(directory)
        return {'removed':removed,'examined':len(page),'next_after':page[-1] if page else after,'exhausted':exhausted}

    def orphan_candidate_page(self, ref, *, older_than, limit, after):
        """Trusted discovery only; snapshot final file identity before planning."""
        candidates=[]
        with self._namespace(ref) as directory:
            with os.scandir(directory) as entries:
                page=heapq.nsmallest(limit+1,(e.name for e in entries
                    if re.fullmatch(r'[a-f0-9]{32}',e.name) and e.name>after))
            exhausted=len(page)<=limit;page=page[:limit]
            for name in page:
                try:
                    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    if error.errno==errno.ELOOP:continue
                    raise
                with os.fdopen(fd,'rb') as stream:
                    info=os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                            or info.st_size>MAX_ARTIFACT_BYTES or info.st_mtime>=older_than.timestamp()):
                        continue
                    payload=stream.read(MAX_ARTIFACT_BYTES+1)
                    if len(payload)!=info.st_size:continue
                candidates.append({'artifact_id':name,'sha256':hashlib.sha256(payload).hexdigest(),
                    'size_bytes':info.st_size,'inode':info.st_ino,'mtime_ns':info.st_mtime_ns})
        return {'candidates':candidates,'next_after':page[-1] if page else after,'exhausted':exhausted}

    def apply_orphan_plan(self, namespace, candidates, guard):
        """Only an authoritative DB absence lock may permit final-file unlink."""
        outcomes=[];removed=0
        with self._namespace(namespace) as directory:
            try:
                for item in candidates:
                    state=guard()
                    name=item['artifact_id']
                    if name not in state['eligible']:
                        disposition='protected'
                    else:
                        try:
                            info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                        except FileNotFoundError:
                            disposition='absent'
                        else:
                            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                                or info.st_ino!=item['inode'] or info.st_mtime_ns!=item['mtime_ns']
                                or info.st_size!=item['size_bytes']):
                                disposition='changed'
                            else:
                                ref=BlobReference(namespace.tenant_id,namespace.world,name,item['sha256'],item['size_bytes'],'application/octet-stream')
                                try:self._read(directory,ref)
                                except (ArtifactIntegrityError,OSError):disposition='changed'
                                else:
                                    guard()  # PG expiry/live evidence again after the bounded hash read.
                                    os.unlink(name,dir_fd=directory);removed+=1;disposition='removed'
                    outcomes.append({'artifact_id':name,'disposition':disposition})
            finally:
                if removed:_sync(directory)
        return outcomes


class AuthorizedArtifactAccess:
    """Require the live EIOS resolver; callers supply server-bound queries only."""
    def __init__(self, store: LocalBlobStore, authorization: AuthorizationDecisionService):
        self.store, self.authorization = store, authorization

    def _authorize(self, query, ref, operation):
        target = query.target
        if (query.tenant_id != ref.tenant_id or target.resource_id != ref.resource_id
                or target.operation is not operation
                or query.request_attributes.get('world') != ref.world):
            raise ArtifactAccessDenied('artifact binding mismatch')
        decision = self.authorization.decide(query)
        if (not decision.authoritative or not decision.allowed or decision.obligations
                or decision.expires_at <= datetime.now(UTC)):
            raise ArtifactAccessDenied('artifact access denied')

    def read(self, query: AuthorizationFactQuery, ref: BlobReference, *, start=0, stop=None):
        self._authorize(query, ref, Operation.READ)
        if type(start) is not int or (stop is not None and type(stop) is not int):
            raise ValueError('invalid byte range')
        stop = ref.size_bytes if stop is None else stop
        if not 0 <= start <= stop <= ref.size_bytes:
            raise ValueError('unsatisfiable byte range')
        return self.store.read(ref)[start:stop]

    def delete_expired(self, query: AuthorizationFactQuery, ref: BlobReference,
                       proof: ArtifactRetentionTerminalProof):
        """Only trusted DB proof + current EIOS DELETE permission can retire data.

        The PostgreSQL metadata coordinator must persist its fenced GC claim
        before invoking this method and finalize deletion afterward.
        """
        if (proof.tenant_id != ref.tenant_id or proof.artifact_id != ref.artifact_id
                or not proof.owner_is_terminal or not proof.deadline_is_expired):
            raise ArtifactAccessDenied('retention proof rejected')
        self._authorize(query, ref, Operation.DELETE)
        self.store.remove(ref)
