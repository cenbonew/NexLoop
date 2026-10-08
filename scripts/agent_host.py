"""Acquire the kernel runtime-directory lock, then exec the Node Agent Host.

The inherited descriptor stays in the actual Host process. No PID lock, helper
process or lease timeout can release it while the Host is alive. This launcher
is not a RuntimeAdapter and grants no business or Run execution authority.
"""
import argparse
import fcntl
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
OWNER_HEADER = b'NexLoop runtime owner v1\n'


def acquire_owner(root):
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError('runtime root refused')
    root = root.resolve(strict=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('private runtime root required')
    fd = os.open(root / '.nexloop-owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('owner descriptor refused')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        content = os.read(fd, len(OWNER_HEADER) + 1)
        if content not in (b'', OWNER_HEADER):
            raise ValueError('foreign owner file refused')
        if not content:
            os.write(fd, OWNER_HEADER)
            os.fsync(fd)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        os.set_inheritable(fd, True)
        return root, fd
    except BaseException:
        os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--internal-key-file', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--tls-certificate-file', type=Path, required=True)
    parser.add_argument('--tls-key-file', type=Path, required=True)
    parser.add_argument('--runtime-config-file', type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('unprivileged local port required')
    fd = None
    try:
        node = args.node.resolve(strict=True)
        if node.name != 'node' or not node.is_file() or not os.access(node, os.X_OK):
            raise ValueError('Node executable refused')
        entry = ROOT / 'apps/agent-host/dist/main.js'
        if entry.is_symlink() or not entry.is_file():
            raise ValueError('built Agent Host required')
        root, fd = acquire_owner(args.runtime_root)
        # Deliberate allowlist: no DB, admin, channel or model credentials enter
        # this foundation. A future Provider must use its separately audited port.
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
        os.execve(node, [str(node), str(entry), str(root), str(fd),
                        str(args.internal_key_file.absolute()), str(args.port),
                        str(args.tls_certificate_file.absolute()), str(args.tls_key_file.absolute()),
                        *([str(args.runtime_config_file.absolute())] if args.runtime_config_file else [])], env)
    except Exception:
        if fd is not None:
            os.close(fd)
        print('Agent Host startup refused or unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
