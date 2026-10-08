"""Bounded service-owned secret files; no default or environment fallback."""
import os
import stat


def read_private_text(path, *, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('private service-owned configuration required')
        value = stream.read(maximum+1)
    if not value or len(value)>maximum:
        raise ValueError('configuration length invalid')
    text = value.decode('utf-8').strip()
    if not text or '\0' in text:
        raise ValueError('configuration content invalid')
    return text
