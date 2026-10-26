"""Test teardown helper: give the owner write access back on every directory and file below a path (links are not followed), so
pytest can remove a tmp_path in which an installer under test made its release tree read-only (CI 2026-10-26: undeletable
garbage-* directories filled the CI host's /tmp quota). Only the test's own tmp_path is touched; the scripts under test keep
their permission logic."""
import os
import stat


def make_writable(top):
    if not os.path.lexists(top) or os.path.islink(top):
        return
    for directory, dirs, files in os.walk(top, topdown=True, followlinks=False):
        try:
            info = os.lstat(directory)
            os.chmod(directory, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
        except OSError:
            pass
        for name in dirs + files:
            path = os.path.join(directory, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue  # never through a link
            try:
                os.chmod(path, stat.S_IMODE(info.st_mode) | (stat.S_IRWXU if stat.S_ISDIR(info.st_mode) else stat.S_IRUSR | stat.S_IWUSR))
            except OSError:
                pass
