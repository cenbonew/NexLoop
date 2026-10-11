"""NX-031 D7 helper: a second backend on the same nexloop_api database and signing key as `api`, with the deployed launcher
pool size (background_services.API_POOL_MAX)."""
from contextlib import contextmanager

from psycopg.conninfo import make_conninfo

from nexloop_eios.backend import open_backend


@contextmanager
def launcher_api(api,pg,tmp_path,pool_max):
    key=tmp_path/'launcher-signing-key';key.write_bytes(api._signer.material);key.chmod(0o600)
    with open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'launcher-artifacts',signing_key_file=key,
            signing_key_id=api._signer.key_id,pool_max_size=pool_max) as backend:
        yield backend
