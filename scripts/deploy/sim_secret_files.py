#!/usr/bin/env python3
"""Simulation stage enablement (docs/implementation/sim-stage-enable.md, "compose 需要的 secret 文件"): every secret file of the
simulation compose project that is not a DSN or a world credential, generated in the sim-setup one-off container on APP_HOST as
NEXLOOP_SIM_UID (so the files belong to that uid from the start). Owner rule: one configuration file, one command.

    sim_secret_files.py --config /setup/PRIVATE.json --dry-run    which files would be written and which are present; writes nothing
    sim_secret_files.py --config /setup/PRIVATE.json              writes the missing files (never overwrites one), then the env draft
    sim_secret_files.py --config /setup/PRIVATE.json --export-registration
        the two keys the database must hold (the simulation signing key and the Host purge key) as one JSON line on stdout - only as
        the APP_HOST end of the pipeline `ssh APP_HOST ... --export-registration | ssh DATA_HOST ... sim_secret_register.py`, run on
        the dispatcher's machine (NEXLOOP_SIM_TRANSFER_PIPE=1, set by that pipeline; refused otherwise: a guard, not a boundary)

Both keys are symmetric (authz.nexloop_authority_signing_keys and authz.nexloop_sim_hosts hold the key material itself: HMAC), so
there is no public part: the registration needs the material, which moves only through that pipe (never a file, an argument or a log).
The output of every other mode names files, key ids, the host id and sha256 digests of the keys (for the registration's self-check),
never a value.

Files written into secret_dir (0600, the caller's: NEXLOOP_SIM_UID in the container), each only when absent:
  sim_host_transport_key, sim_host_guard_key, sim_host_purge_key   64 hex characters (32 random bytes); the purge key is registered
  sim_signing_key                                                  32 URL-safe ASCII characters (AuthoritySigner reads exactly 32
                                                                   bytes, as NX-032's signing.key); registered under signing_key_id
  sim_host_runtime_config        world_group simulation, context_input_protocol nexloop.context-pack.sim.v1, host_id, the purge key
                                 file, the guard on 127.0.0.1:<guard_port>, runtime_profile deterministic-test (D6)
  sim_host_model_configuration   the deterministic profile's marker (the Host reads no model configuration in that profile; the
                                 compose project mounts the file)
  sim_pg_ca                      a copy of the real PostgreSQL CA bundle (real_pg_ca_file: the same server certificate)
  sim_host_cert, sim_host_key, sim_host_ca, sim_guard_certificate, sim_guard_tls_key, sim_guard_ca
                                 issued by issue_certs from the simulation CA in ca_dir (created there when absent: a root and an
                                 intermediate of its own, never the real CA); the Host and the guard serve on the shared loopback
A certificate is issued only with its key: a certificate present without its key (or the reverse) is refused, nothing written.
env_draft_file (optional): the private simulation env as a draft - every NEXLOOP_SIM_* the compose projects read, values from the
configuration's "env" (and host_id, signing_key_id, guard_port), placeholders <...> for the rest; written only when absent.

Configuration (private JSON, 0600; deploy/examples/sim-secret-files.example.json):
  {"schema": "nexloop-sim-secret-files/1", "secret_dir": "/sim-secrets", "ca_dir": "/sim-tokens/sim-ca", "host_id": "sim-host-1",
   "signing_key_id": "sim-<...>", "guard_port": 8201, "real_pg_ca_file": "/setup/pg-ca.crt",
   "sans": {"host": ["IP:127.0.0.1"], "guard": ["IP:127.0.0.1"]}, "env_draft_file": "/sim-tokens/sim.env.draft", "env": {...}}
Exit: 0 done, 1 failed, 2 input refused.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import string
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import issue_certs  # noqa: E402

SCHEMA = 'nexloop-sim-secret-files/1'
KEYS = {'schema', 'secret_dir', 'ca_dir', 'host_id', 'signing_key_id', 'guard_port', 'real_pg_ca_file', 'sans'}
OPTIONAL = {'env_draft_file', 'env'}
SIM_PROTOCOL = 'nexloop.context-pack.sim.v1'
HEX_KEYS = ('sim_host_transport_key', 'sim_host_guard_key', 'sim_host_purge_key')
# (certificate secret, key secret, CA secret, issue_certs name, SAN list)
TLS = (('sim_host_cert', 'sim_host_key', 'sim_host_ca', 'sim-host', 'host'),
       ('sim_guard_certificate', 'sim_guard_tls_key', 'sim_guard_ca', 'sim-guard', 'guard'))
FILES = (*HEX_KEYS, 'sim_signing_key', 'sim_host_runtime_config', 'sim_host_model_configuration', 'sim_pg_ca',
         *(name for row in TLS for name in row[:3]))
# The NEXLOOP_SIM_* names the simulation compose projects read (compose.simulation / compose.sim-issue), for the env draft.
ENV_NAMES = ('NEXLOOP_SIM_UID', 'NEXLOOP_SIM_GID', 'NEXLOOP_SIM_SUBNET', 'NEXLOOP_SIM_HOST_PORT', 'NEXLOOP_SIM_GUARD_PORT', 'NEXLOOP_SIM_HOST_ID',
             'NEXLOOP_SIM_SIGNING_KEY_ID', 'NEXLOOP_SIM_RUNTIME_DIR', 'NEXLOOP_SIM_ARTIFACT_DIR', 'NEXLOOP_SIM_SECRET_DIR', 'NEXLOOP_SIM_TOKEN_DIR',
             'NEXLOOP_SIM_RELEASE_CHECKOUT', 'NEXLOOP_SIM_SETUP_INPUT_DIR', 'NEXLOOP_SIM_WORLD', 'NEXLOOP_BACKEND_IMAGE', 'AGENT_HOST_IMAGE')
_HOST = re.compile(r'[a-z][a-z0-9-]{2,62}')
_KEY_ID = re.compile(r'sim-[A-Za-z0-9._-]{1,60}')
_ENV_VALUE = re.compile(r'[A-Za-z0-9._:/@=+-]{1,256}')


class Refused(ValueError):
    pass


def read_private(path, maximum=65536):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, TypeError):
        raise Refused('private file unreadable: ' + Path(str(path)).name) from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller: ' + Path(str(path)).name)
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Refused('private file too large')
    return data


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or not KEYS <= set(value) <= KEYS | OPTIONAL:
        raise Refused('configuration fields: ' + ', '.join(sorted((KEYS | OPTIONAL) - {'schema'})))
    for key in ('secret_dir', 'ca_dir', 'real_pg_ca_file') + (('env_draft_file',) if 'env_draft_file' in value else ()):
        if type(value[key]) is not str or not value[key].startswith('/') or '..' in value[key]:
            raise Refused(f'{key}: an absolute path')
    secret, ca = Path(value['secret_dir']).resolve(), Path(value['ca_dir']).resolve()
    if ca == secret or secret in ca.parents:
        raise Refused('ca_dir: outside secret_dir (the CA keys are never a compose secret)')
    if type(value['host_id']) is not str or not _HOST.fullmatch(value['host_id']):
        raise Refused('host_id: [a-z][a-z0-9-]{2,62} (authz.nexloop_sim_hosts)')
    if type(value['signing_key_id']) is not str or not _KEY_ID.fullmatch(value['signing_key_id']):
        raise Refused('signing_key_id: sim-<id> (the simulation group\'s own key id, never a real one)')
    if type(value['guard_port']) is not int or not 1024 <= value['guard_port'] <= 65535:
        raise Refused('guard_port: the compose NEXLOOP_SIM_GUARD_PORT')
    sans = value['sans']
    if type(sans) is not dict or set(sans) != {'host', 'guard'} or not all(type(v) is list and v for v in sans.values()):
        raise Refused('sans: {"host": [...], "guard": [...]} (DNS:<name> or IP:<address>)')
    for v in sans.values():
        try:
            issue_certs._san(v)  # refuses anything but DNS:/IP:
        except (issue_certs.Refused, ValueError):
            raise Refused('sans: DNS:<name> or IP:<address>') from None
    env = value.get('env') or {}
    if type(env) is not dict or any(k not in ENV_NAMES or type(v) not in (str, int) or not _ENV_VALUE.fullmatch(str(v)) for k, v in env.items()):
        raise Refused('env: NEXLOOP_SIM_* names of the compose projects, plain values')
    return value


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data if isinstance(data, bytes) else data.encode())


def present(config):
    d = Path(config['secret_dir'])
    return {name: (d / name).exists() or (d / name).is_symlink() for name in FILES}


def runtime_config(config):
    """The simulation Host's runtime configuration (accepted by apps/agent-host/src/runtime-host.ts; as tests/test_nx041_sim_pi_run_pg)."""
    return {'guard_url': f"https://127.0.0.1:{config['guard_port']}/internal/v1/runtime/authorize", 'guard_key_file': '/run/secrets/sim_host_guard_key',
            'guard_ca_file': '/run/secrets/sim_guard_ca', 'runtime_profile': 'deterministic-test', 'world_group': 'simulation',
            'host_id': config['host_id'], 'world_purge_key_file': '/run/secrets/sim_host_purge_key', 'context_input_protocol': SIM_PROTOCOL,
            'cost_currency': 'CNY'}


def ca(config):
    """The simulation CA (ca_dir/root, ca_dir/intermediate), created once; never the real one."""
    base = Path(config['ca_dir'])
    root, mid = base / 'root', base / 'intermediate'
    made = False
    if not (mid / 'ca.crt').exists():
        if base.exists() and any(base.iterdir()):
            raise Refused('ca_dir: a partial simulation CA (remove it to start again)')
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        issue_certs.root(root, 3650)
        issue_certs.intermediate(root, mid, 365)
        made = True
    return mid, made


def generate(config):
    d = Path(config['secret_dir'])
    if not d.is_dir() or d.is_symlink() or d.stat().st_mode & 0o077:
        raise Refused('secret_dir: an existing 0700 directory (sim-setup-dir creates it)')
    have = present(config)
    for cert, key, _, _, _ in TLS:
        if have[cert] != have[key]:
            raise Refused(f'{cert} and {key}: one present without the other (remove both to issue them again)')
    written = []
    for name in HEX_KEYS:
        if not have[name]:
            write_private(d / name, secrets.token_hex(32))
            written.append(name)
    if not have['sim_signing_key']:
        write_private(d / 'sim_signing_key', ''.join(secrets.choice(string.ascii_letters + string.digits + '-_') for _ in range(32)))
        written.append('sim_signing_key')
    if not have['sim_host_runtime_config']:
        write_private(d / 'sim_host_runtime_config', json.dumps(runtime_config(config), indent=1) + '\n')
        written.append('sim_host_runtime_config')
    if not have['sim_host_model_configuration']:
        write_private(d / 'sim_host_model_configuration', json.dumps({'runtime_profile': 'deterministic-test', 'model': None,
                      'note': 'D6: the simulation Host runs the deterministic profile and reads no model configuration'}, indent=1) + '\n')
        written.append('sim_host_model_configuration')
    if not have['sim_pg_ca']:
        source = Path(config['real_pg_ca_file'])
        if not source.is_file() or source.is_symlink() or b'BEGIN CERTIFICATE' not in source.read_bytes()[:4096]:
            raise Refused('real_pg_ca_file: the real PostgreSQL CA bundle (PEM)')
        write_private(d / 'sim_pg_ca', source.read_bytes())
        written.append('sim_pg_ca')
    ca_made = False
    if not all(have[row[0]] for row in TLS) or not all(have[row[2]] for row in TLS):
        mid, ca_made = ca(config)
        bundle = (mid / 'bundle.crt').read_bytes()
        for cert, key, ca_name, issue_name, san in TLS:
            if not have[cert]:
                with tempfile.TemporaryDirectory(prefix='sim-tls-') as work:
                    issue_certs.leaf('server', mid, work, issue_name, sans=config['sans'][san])
                    write_private(d / key, (Path(work) / f'{issue_name}.key').read_bytes())
                    write_private(d / cert, (Path(work) / f'{issue_name}.chain.crt').read_bytes())
                written += [cert, key]
            if not have[ca_name]:
                write_private(d / ca_name, bundle)
                written.append(ca_name)
    draft = None
    if config.get('env_draft_file'):
        path = Path(config['env_draft_file'])
        draft = 'kept' if path.exists() else 'written'
        if draft == 'written':
            values = {**{k: str(v) for k, v in (config.get('env') or {}).items()}, 'NEXLOOP_SIM_HOST_ID': config['host_id'],
                      'NEXLOOP_SIM_SIGNING_KEY_ID': config['signing_key_id'], 'NEXLOOP_SIM_GUARD_PORT': str(config['guard_port'])}
            write_private(path, '# Private simulation env (draft from sim_secret_files.py): fill every <...> before use.\n'
                          + ''.join(f'{k}={values.get(k, "<" + k.lower() + ">")}\n' for k in ENV_NAMES))
    return {'ok': True, 'written': sorted(written), 'kept': sorted(n for n in FILES if have[n]), 'changed': bool(written), 'ca_created': ca_made,
            'env_draft': draft, 'registration': registration_public(config)}


def _key_material(config):
    d = Path(config['secret_dir'])
    signing = read_private(d / 'sim_signing_key', 128).strip()
    purge = bytes.fromhex(read_private(d / 'sim_host_purge_key', 128).decode().strip())
    if len(signing) != 32 or len(purge) != 32:
        raise Refused('sim_signing_key and sim_host_purge_key: 32 bytes each')
    return signing, purge


def registration_public(config):
    """What the registration's self-check compares - identifiers and sha256 digests, never the keys."""
    signing, purge = _key_material(config)
    return {'signing_key_id': config['signing_key_id'], 'signing_key_sha256': hashlib.sha256(signing).hexdigest(),
            'host_id': config['host_id'], 'purge_key_sha256': hashlib.sha256(purge).hexdigest()}


def main(argv=None, *, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--export-registration', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True), file=out)
        return code
    try:
        config = load_config(read_private(args.config).decode())
        if args.dry_run:
            have = present(config)
            return emit({'ok': True, 'dry_run': True, 'to_write': sorted(n for n in FILES if not have[n]), 'present': sorted(n for n in FILES if have[n]),
                         'ca_present': (Path(config['ca_dir']) / 'intermediate' / 'ca.crt').exists(), 'host_id': config['host_id'],
                         'signing_key_id': config['signing_key_id']}, 0)
        if args.export_registration:
            if os.environ.get('NEXLOOP_SIM_TRANSFER_PIPE') != '1':
                raise Refused('--export-registration runs only as the APP_HOST end of the documented pipeline into sim_secret_register.py')
            signing, purge = _key_material(config)
            out.write(json.dumps({'schema': 'nexloop-sim-registration/1', 'signing_key_id': config['signing_key_id'], 'signing_key_hex': signing.hex(),
                                  'host_id': config['host_id'], 'purge_key_hex': purge.hex()}) + '\n')
            return 0
        return emit(generate(config), 0)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    except issue_certs.Refused as error:
        return emit({'ok': False, 'detail': 'certificate: ' + str(error)}, 1)
    except Exception as error:  # noqa: BLE001 - the type only (a message may hold a value)
        return emit({'ok': False, 'error': type(error).__name__}, 1)


if __name__ == '__main__':
    sys.exit(main())
