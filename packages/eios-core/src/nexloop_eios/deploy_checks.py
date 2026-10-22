"""NX-032 slices 1–2: deployment gate checks for the doctor (docs/implementation/NX-032-design.md §2, §3.4, §4, §6).

Every check takes files or already-collected read-only inventory and returns {'checks': {name: bool}, 'details': {...}}. Details
carry names, codes, counts, hashes and validity only — never a key, a password, an address or a path from the private files.
Nothing here connects to a server except the role check, which reads pg_roles through the DSN the doctor is given.
"""
import os
import re
import socket
import ssl
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

WARN_DAYS, FAIL_DAYS = 30, 7


# TLS (§2.1–§2.3) ----------------------------------------------------------------------------------------------------------
def _key_file_ok(path, uid=None):
    try:
        info = os.lstat(path)
    except OSError:
        return 'key_missing'
    if not stat.S_ISREG(info.st_mode):
        return 'key_not_regular_file'
    if info.st_mode & 0o077:
        return 'key_permissions'
    if info.st_nlink != 1:
        return 'key_hard_linked'
    if uid is not None and info.st_uid != uid:
        return 'key_owner'
    return None


def _handshake(server_ctx, client_ctx, server_name):
    """One TLS handshake in this process over a socket pair: proves chain, name and key together without any network."""
    a, b = socket.socketpair()
    error = []

    def serve():
        try:
            with server_ctx.wrap_socket(a, server_side=True) as s:
                s.recv(1)
        except (ssl.SSLError, OSError) as e:
            error.append(e)
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with client_ctx.wrap_socket(b, server_hostname=server_name) as c:
            c.sendall(b'x')
        ok = True
    except (ssl.SSLError, OSError) as e:
        error.append(e)
        ok = False
    t.join(5)
    return ok and not error, error


def _days_left(cert, now):
    decoded = ssl._ssl._test_decode_cert(str(cert))
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(decoded['notAfter']), UTC)
    return (not_after - now).total_seconds() / 86400, decoded


def check_certificate(*, chain, key, ca, purpose, names=(), cn=None, uid=None, now=None):
    """purpose 'server': the chain verifies to the CA for every expected name (verify-full). purpose 'client': the chain verifies
    as a client certificate and its CN is the expected role or ACL user. Validity: < 30 days warns, < 7 days fails."""
    now = now or datetime.now(UTC)
    problems, warnings = [], []
    key_problem = _key_file_ok(key, uid)
    if key_problem:
        problems.append(key_problem)
    try:
        days, decoded = _days_left(chain, now)
    except Exception:
        return {'passed': False, 'problems': ['certificate_unreadable'], 'warnings': []}
    if days < FAIL_DAYS:
        problems.append('expires_within_7_days' if days >= 0 else 'expired')
    elif days < WARN_DAYS:
        warnings.append('expires_within_30_days')
    if not key_problem:
        if purpose == 'server':
            for name in names or ['']:
                server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                try:
                    server.load_cert_chain(chain, key)
                    client.load_verify_locations(ca)
                except ssl.SSLError:
                    problems.append('key_does_not_match_certificate')
                    break
                ok, _ = _handshake(server, client, name)
                if not ok:
                    problems.append('chain_or_name_not_verified:' + ('expected_name_' + str(list(names).index(name)) if names else 'none'))
        else:
            subject = dict(x[0] for x in decoded.get('subject', ()))
            if subject.get('commonName') != cn:
                problems.append('client_cn_mismatch')
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.check_hostname, client.verify_mode = False, ssl.CERT_NONE
            server.verify_mode = ssl.CERT_REQUIRED
            try:
                # A throwaway server identity: only the client side (chain to the CA, key match) is under test.
                server.load_cert_chain(chain, key)
                server.load_verify_locations(ca)
                client.load_cert_chain(chain, key)
            except ssl.SSLError:
                problems.append('key_does_not_match_certificate')
            else:
                ok, _ = _handshake(server, client, None)
                if not ok:
                    problems.append('client_chain_not_verified')
    return {'passed': not problems, 'problems': sorted(set(problems)), 'warnings': warnings, 'days_left': int(days)}


class OwnedFileRefused(ValueError):
    """A private file the gate refused; .code is the reason code reported in the details."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _read_owned_file(path, uid=None, *, maximum=16384):
    """A service's private file as the gate reads it. Without `uid`: owned by this process (read_private_text). With `uid`: the gate
    runs as root to see every service's directory, so the file must be owned by that service uid instead - still a regular file (not
    followed through a link), owner-only (no group or other bits), bounded, like _key_file_ok for the keys. Refusals carry a code."""
    if uid is None:
        from nexloop_eios.private_configuration import read_private_text
        try:
            return read_private_text(path, maximum=maximum)
        except FileNotFoundError:
            raise OwnedFileRefused('missing') from None
        except PermissionError:
            raise OwnedFileRefused('not_owned_by_reader_or_not_private') from None
        except (OSError, ValueError):
            raise OwnedFileRefused('unreadable_or_invalid') from None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise OwnedFileRefused('missing') from None
    except OSError:
        raise OwnedFileRefused('not_openable_or_link') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OwnedFileRefused('not_regular_file')
        if info.st_uid != uid:
            raise OwnedFileRefused('owner_differs')
        if info.st_mode & 0o077:
            raise OwnedFileRefused('permissions')
        value = stream.read(maximum + 1)
    if not value or len(value) > maximum:
        raise OwnedFileRefused('length')
    try:
        return value.decode('utf-8').strip()
    except UnicodeDecodeError:
        raise OwnedFileRefused('not_utf8') from None


def _read_dsn_file(path, uid=None):
    return _read_owned_file(path, uid, maximum=16384)


def check_dsn(text):
    """Every service DSN is a complete verify-full connection string (11 §4): no other sslmode, CA, client cert and key named."""
    from psycopg.conninfo import conninfo_to_dict
    try:
        d = conninfo_to_dict(text)
    except Exception:
        return ['dsn_unparseable']
    problems = []
    if d.get('sslmode') != 'verify-full':
        problems.append('sslmode_not_verify_full')
    for key in ('sslrootcert', 'sslcert', 'sslkey', 'host', 'user', 'dbname'):
        if not d.get(key):
            problems.append('missing_' + key)
    if d.get('sslrootcert') == 'system':
        problems.append('sslrootcert_system')
    return problems


def check_valkey_conf(text):
    conf = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if parts and not parts[0].startswith('#'):
            conf[parts[0]] = parts[1].strip() if len(parts) > 1 else ''
    problems = []
    if conf.get('port') != '0':
        problems.append('plaintext_port_open')
    if not conf.get('tls-port'):
        problems.append('tls_port_missing')
    if conf.get('tls-auth-clients') != 'yes':
        problems.append('client_certificates_not_required')
    if not conf.get('aclfile'):
        problems.append('acl_file_missing')
    return problems


def tls(spec, *, now=None):
    """spec: {"ca": path, "certificates": [{"name", "chain", "key", "purpose", "names"|"cn", "uid"?}], "dsn_files": [{name, path, uid?}],
    "valkey_conf": path}. Paths come from the private deployment; results carry names and codes only."""
    certs = {c['name']: check_certificate(chain=c['chain'], key=c['key'], ca=spec['ca'], purpose=c['purpose'], names=c.get('names', ()),
                                          cn=c.get('cn'), uid=c.get('uid'), now=now) for c in spec.get('certificates', [])}
    dsns = {}
    for item in spec.get('dsn_files', []):
        try:
            dsns[item['name']] = check_dsn(_read_dsn_file(item['path'], item.get('uid')))
        except Exception:
            dsns[item['name']] = ['dsn_file_unreadable_or_not_private']
    valkey = check_valkey_conf(Path(spec['valkey_conf']).read_text()) if spec.get('valkey_conf') else ['valkey_conf_not_given']
    checks = {'tls_certificates': bool(certs) and all(c['passed'] for c in certs.values()),
              'tls_dsn_verify_full': bool(dsns) and all(not p for p in dsns.values()), 'tls_valkey_only': not valkey}
    return {'checks': checks, 'details': {'tls': {'certificates': certs, 'dsn': dsns, 'valkey': valkey}}}


# Slice 2: process shape and registration (ADR-024, NX-031 D7, §6) ------------------------------------------------------------
GUARD_WORKERS = 4


def guard_and_registration(budget, running=None):
    """The budget's Runtime Worker runs exactly 4 guard processes (ADR-024's deployed value), and every running process that opens
    a PostgreSQL pool is declared with at least its running process count; the running guard children are exactly 4."""
    from nexloop_eios import connection_budget
    services = {s['name']: s for s in budget.get('services', []) if type(s) is dict}
    runtime = [s for s in services.values() if 'guard_workers' in s]
    checks = {'guard_workers_exact': bool(runtime) and all(s['guard_workers'] == GUARD_WORKERS for s in runtime)}
    details = {'guard_workers': {s['name']: s['guard_workers'] for s in runtime}}
    try:
        evaluated = connection_budget.evaluate(budget)
        checks['budget_all_required_declared'] = evaluated['checks']['all_services_declared']
        details['missing_required'] = evaluated['details']['missing_services']
    except connection_budget.BudgetInvalid as error:
        checks['budget_all_required_declared'] = False
        details['budget_error'] = str(error)
    if running is None:
        checks['running_processes_registered'] = False
        details['running'] = 'not_provided'
    else:
        unregistered = sorted(n for n, r in running.get('services', {}).items() if r.get('pg') and n not in services)
        over = sorted(n for n, r in running.get('services', {}).items() if r.get('pg') and n in services and r.get('processes', 0) > services[n]['processes'])
        guards = {n: r.get('guard_children') for n, r in running.get('services', {}).items() if n in services and 'guard_workers' in services[n]}
        checks['running_processes_registered'] = not unregistered and not over
        checks['running_guard_children_exact'] = bool(guards) and all(v == GUARD_WORKERS for v in guards.values())
        details.update(unregistered=unregistered, over_declared=over, running_guard_children=guards)
    return {'checks': checks, 'details': {'process_shape': details}}


# Slice 2: port bindings (§3.1) -----------------------------------------------------------------------------------------------
_LOOPBACK = {'127.0.0.1', '::1'}
_WILDCARD = {'0.0.0.0', '::', '*'}


def port_bindings(listening, stage, host_role, *, before_deploy=False):
    """listening: [{"address", "port", "owner": "nexloop"|"other"}] from the read-only inventory of one host. stage: the rendered
    parameters (render.load_env). DATA_HOST: PG and Valkey only on DATA_HOST_ADDR. APP_HOST: the Gateway only on APP_HOST_ADDR,
    the internal ports only on loopback. No NexLoop socket on a wildcard address. Before deploying, a planned port held by
    another service is a conflict (D7: change the port in the private env, never the existing service)."""
    if host_role == 'data':
        public = {'PG_PORT': stage['DATA_HOST_ADDR'], 'VALKEY_PORT': stage['DATA_HOST_ADDR']}
        internal = {}
    else:
        public = {'GATEWAY_HTTPS_PORT': stage['APP_HOST_ADDR'], 'GATEWAY_REDIRECT_PORT': stage['APP_HOST_ADDR']}
        internal = {k: None for k in ('API_INTERNAL_PORT', 'API_INTERNAL_PORT_2', 'HOST_INTERNAL_PORT', 'GUARD_PORT')}
    problems = []
    by_port = {}
    for entry in listening:
        by_port.setdefault(int(entry['port']), []).append(entry)
    for param, address in {**public, **internal}.items():
        for entry in by_port.get(int(stage[param]), []):
            if entry['owner'] != 'nexloop':
                problems.append('port_conflict:' + param)
            elif param in public and entry['address'] != address:
                problems.append('public_port_wrong_address:' + param)
            elif param in internal and entry['address'] not in _LOOPBACK:
                problems.append('internal_port_not_loopback:' + param)
    for entry in listening:
        if entry['owner'] == 'nexloop' and entry['address'] in _WILDCARD:
            problems.append('nexloop_wildcard_bind')
    if not before_deploy:
        for param in public:
            if not any(e['owner'] == 'nexloop' for e in by_port.get(int(stage[param]), [])):
                problems.append('public_port_not_listening:' + param)
    return {'checks': {'port_bindings': not problems}, 'details': {'port_bindings': sorted(set(problems))}}


# Slice 2: roles (§4.1) --------------------------------------------------------------------------------------------------------
ADMIN_ROLES = ('nexloop_bootstrap', 'nexloop_owner')


def roles(connection, *, admin=ADMIN_ROLES, disabled=('nexloop_bootstrap',)):
    """Application roles (every nexloop_* role except the administrative identities: bootstrap, schema owner, migration identity)
    hold no superuser, BYPASSRLS, CREATEROLE or CREATEDB; the identities in `disabled` cannot log in outside their window."""
    rows = connection.execute(
        "select rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolcanlogin from pg_roles where left(rolname,8)='nexloop_'").fetchall()
    privileged = sorted(r[0] for r in rows if r[0] not in admin and r[0] not in disabled and any(r[1:5]))
    can_login = sorted(r[0] for r in rows if r[0] in disabled and r[5])
    return {'checks': {'application_roles_unprivileged': not privileged, 'bootstrap_identities_disabled': not can_login},
            'details': {'roles': {'privileged': privileged, 'bootstrap_login_enabled': can_login, 'count': len(rows)}}}


# Slice 2: secret distribution (§4.2) -------------------------------------------------------------------------------------------
_SECRET_NAME = re.compile(r'(dsn|database|password|credential|signing|channel|token|api[_-]?key|service-credential)', re.I)
_SECRET_ENV = re.compile(r'(PASSWORD|SECRET|TOKEN|DSN|DATABASE_URL|API_KEY|CREDENTIAL|PRIVATE)', re.I)
AGENT_HOST = 'agent-host'


# The stage Compose example takes the deployment switch from the compose env, default false (calibration 2026-10-17); unrendered
# (stage_secrets check) that reference is closed. The deploy gate reads the rendered Compose, where the value is true or false.
DISPATCH_TEMPLATE_DEFAULT = '${REAL_DISPATCH_ENABLED:-false}'


def secret_distribution(secret_root, *, compose=None, uids=None):
    """NEXLOOP_ROOT/secrets/<service>/: directory 0700, files 0600, regular, one link, owned by the service UID when given. The Agent
    Host directory holds no DSN, credential, signing or channel file. In the rendered APP_HOST Compose: no secret value in any
    environment, only `_FILE` paths; the Agent Host mounts none of the other services' secrets and no Artifact directory; real
    dispatch starts closed."""
    problems = []
    root = Path(secret_root)
    services = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
    if not services:
        problems.append('secret_root_empty_or_missing')
    for name in services:
        d = root / name
        info = os.lstat(d)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            problems.append(f'directory_permissions:{name}')
            continue
        if uids and name in uids and info.st_uid != uids[name]:
            problems.append(f'directory_owner:{name}')
        for f in sorted(d.iterdir()):
            fi = os.lstat(f)
            if stat.S_ISLNK(fi.st_mode) or not stat.S_ISREG(fi.st_mode):
                problems.append(f'not_regular_file:{name}')
            elif fi.st_mode & 0o077:
                problems.append(f'file_permissions:{name}')
            elif fi.st_nlink != 1:
                problems.append(f'hard_linked:{name}')
            elif uids and name in uids and fi.st_uid != uids[name]:
                problems.append(f'file_owner:{name}')
            if name == AGENT_HOST and _SECRET_NAME.search(f.name):
                problems.append('agent_host_holds_service_secret')
    dispatch_open = []  # rendered Compose: 'false' closed; the unrendered example: its default-false variable reference
    if compose is not None:
        for svc, spec in (compose.get('services') or {}).items():
            env = spec.get('environment') or {}
            for key, value in env.items():
                if _SECRET_ENV.search(key) and not key.endswith('_FILE'):
                    problems.append(f'secret_in_environment:{svc}')
                if key == 'REAL_DISPATCH_ENABLED' and str(value).lower() != 'false' and str(value) != DISPATCH_TEMPLATE_DEFAULT:
                    dispatch_open.append(svc)
            if svc == AGENT_HOST:
                secrets = [s if isinstance(s, str) else s.get('source', '') for s in spec.get('secrets') or []]
                if any(_SECRET_NAME.search(s) for s in secrets):
                    problems.append('agent_host_mounts_service_secret')
                for volume in spec.get('volumes') or []:
                    source = volume.get('source', '') if isinstance(volume, dict) else str(volume).split(':')[0]
                    if 'artifact' in source or '/secrets/' in source:
                        problems.append('agent_host_mounts_artifacts_or_secrets')
    checks = {'secret_distribution': not problems}
    if compose is not None:
        checks['real_dispatch_closed'] = not dispatch_open
    return {'checks': checks, 'details': {'secrets': {'services': services, 'problems': sorted(set(problems)), 'dispatch_open': sorted(dispatch_open)}}}


# Slice 2: existing services untouched (§3.4) -----------------------------------------------------------------------------------
INVENTORY_SECTIONS = ('listening', 'containers', 'compose_projects', 'units', 'data_directories')


_EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'  # sha256 of nothing
_USER_SESSION = re.compile(r'user(-runtime-dir)?@\d+\.service')


def _nexloop_entry(key, value, prefix):
    return key.startswith(prefix) or (value or {}).get('owner') == prefix


def inventory_compare(before, after, *, prefix='nexloop'):
    """Only NexLoop additions are allowed between the read-only inventories taken before and after a deployment step: every item
    that existed before is unchanged and present, and the firewall rule set differs only by the NexLoop table. Counts only.
    A baseline (phase baseline, taken after deployment) is compared on its non-NexLoop entries only. Units that start and stop on
    their own (TriggeredBy) and login sessions (user@<uid>) are listed apart, never a change. A firewall that either side could not
    read (firewall_unreadable, or the hash of an empty rule set an ordinary account recorded) is a problem, never a pass."""
    problems, added = [], {}
    baseline = before.get('phase') == 'baseline'
    on_demand = set(before.get('on_demand_units') or []) | set(after.get('on_demand_units') or [])
    transient = {'appeared': [], 'disappeared': [], 'changed': []}
    for section in INVENTORY_SECTIONS:
        b, a = before.get(section, {}), after.get(section, {})
        if baseline:
            b = {k: v for k, v in b.items() if not _nexloop_entry(k, v, prefix)}
            a = {k: v for k, v in a.items() if not _nexloop_entry(k, v, prefix)} if section != 'listening' else a
        if section == 'units':
            moving = {k for k in set(b) | set(a) if k in on_demand or _USER_SESSION.fullmatch(k)}
            transient['appeared'] += sorted(k for k in moving if k in a and k not in b)
            transient['disappeared'] += sorted(k for k in moving if k in b and k not in a)
            transient['changed'] += sorted(k for k in moving if k in a and k in b and a[k] != b[k])
            b = {k: v for k, v in b.items() if k not in moving}
            a = {k: v for k, v in a.items() if k not in moving}
        for key, value in b.items():
            if key not in a:
                problems.append(f'removed:{section}')
            elif a[key] != value:
                problems.append(f'changed:{section}')
        new = [k for k in a if k not in b]
        # An addition is NexLoop's when its key (container, project, unit, directory) carries the prefix or it is marked so.
        if any(not k.startswith(prefix) and (a[k] or {}).get('owner') != prefix for k in new):
            problems.append(f'non_nexloop_addition:{section}')
        added[section] = len(new)
    firewall = {}
    unreadable = [side for side, inv in (('before', before), ('after', after))
                  if inv.get('firewall_unreadable') or _EMPTY_SHA256 in (inv.get('firewall_without_nexloop_sha256'), inv.get('firewall_normalized_sha256'))]
    if unreadable:
        problems.append('firewall_unreadable')
        firewall = {'unreadable': unreadable, 'note': 'nft list ruleset was not read (run inventory.py as root); nothing was compared'}
    elif before.get('firewall_algorithm') == 2 and after.get('firewall_algorithm') == 2:
        # Algorithm 2 (scripts/deploy/inventory.py): rules naming only NexLoop's own networks are left out on both sides.
        if before.get('firewall_normalized_sha256') != after.get('firewall_normalized_sha256'):
            problems.append('changed:firewall')
            b, a = before.get('firewall_normalized_rules') or [], after.get('firewall_normalized_rules') or []
            firewall = {'lines_only_before': len([x for x in b if x not in a]), 'lines_only_after': len([x for x in a if x not in b])}
        if before.get('firewall_rebaselined'):
            firewall['before_rebaselined'] = True  # the owner accepted this baseline (inventory.py --rebaseline)
    elif after.get('firewall_algorithm') == 2:
        # The before inventory has only the algorithm 1 hash, which cannot be recomputed from it: never a pass by default.
        problems.append('firewall_before_old_algorithm')
        firewall = {'note': 'before uses the old algorithm and cannot be recomputed; inventory.py --rebaseline (owner decision)'}
    elif before.get('firewall_without_nexloop_sha256') != after.get('firewall_without_nexloop_sha256'):
        problems.append('changed:firewall')
    details = {'problems': sorted(set(problems)), 'added': added}
    if firewall:
        details['firewall'] = firewall
    if any(transient.values()):
        details['units_on_demand_or_session'] = {k: v for k, v in transient.items() if v}  # a hint, not a change
    if baseline:
        details['before_is_baseline'] = True
    return {'checks': {'existing_services_unchanged': not problems}, 'details': {'inventory': details}}


# Budget <-> Compose (L5 review 2026-10-11) ---------------------------------------------------------------------------------------
BUDGET_LABEL = 'nexloop.budget-service'


def compose_budget(budget, compose):
    """Every budget entry is opened by the declared number of containers of the rendered APP_HOST Compose (label
    nexloop.budget-service), and every container names budget entries or "none"; any difference fails."""
    declared = {s['name']: s['processes'] for s in budget.get('services', []) if type(s) is dict}
    seen, problems = {}, []
    for name, spec in (compose.get('services') or {}).items():
        label = ((spec.get('labels') or {}).get(BUDGET_LABEL) or '').strip()
        if not label:
            problems.append('unlabelled_service:' + name)
            continue
        if label == 'none':
            continue
        for entry in label.split(','):
            seen[entry] = seen.get(entry, 0) + 1
    for entry, processes in declared.items():
        if entry not in seen:
            problems.append('budget_entry_without_service:' + entry)
        elif seen[entry] != processes:
            problems.append('process_count_differs:' + entry)
    for entry in seen:
        if entry not in declared:
            problems.append('service_not_in_budget:' + entry)
    # NX-038 E3 wiring (L6 review 5.1): a container's --semantic-read-pool-max is exactly its budget entry's
    # semantic_read_pool_max (absent = 0), so the semantic-read pool the process opens is the one the budget counts.
    semantic = {s['name']: s.get('semantic_read_pool_max', 0) for s in budget.get('services', []) if type(s) is dict}
    for name, spec in (compose.get('services') or {}).items():
        label = ((spec.get('labels') or {}).get(BUDGET_LABEL) or '').strip()
        command = [str(c) for c in (spec.get('command') or [])] if type(spec.get('command')) is list else []
        flag = command[command.index('--semantic-read-pool-max') + 1] if '--semantic-read-pool-max' in command[:-1] else '0'
        wanted = sum(semantic.get(e, 0) for e in label.split(',')) if label and label != 'none' else 0
        if flag != str(wanted):
            problems.append('semantic_read_pool_differs:' + name)
    return {'checks': {'compose_matches_budget': not problems}, 'details': {'compose_budget': sorted(problems)}}


SIMULATION_PROJECT = 'nexloop-simulation'
# The services of deploy/stage/compose.simulation.example.yaml that only the simulation group runs (L6 sim-connection-budget review 1.1:
# a project started with another -p name is still the simulation group when it runs any of them; images are shared with real, so they
# do not tell the two apart).
SIMULATION_SERVICES = frozenset({'sim-agent-host', 'sim-runtime-worker', 'sim-stand-in-executor', 'sim-world-files'})


def simulation_projects(inventory):
    """The running projects that are the simulation group: by name, or by any simulation service among their containers."""
    named = {p for p in (inventory.get('compose_projects') or {}) if p == SIMULATION_PROJECT}
    by_service = {p for p, services in (inventory.get('compose_services') or {}).items() if SIMULATION_SERVICES & set(services or ())}
    return named | by_service


def simulation_compose_budget(budget, compose):
    """ADR-037: the simulation Compose against the budget's simulation section, both ways (every entry opened by its declared
    containers, every container labelled with an entry or none), and the simulation runtime worker's pools exactly as declared."""
    section = budget.get('simulation') if type(budget.get('simulation')) is dict else {}
    result = compose_budget({'services': section.get('services') or []}, compose)
    problems = list(result['details']['compose_budget'])
    for entry in section.get('services') or []:
        if type(entry) is not dict or 'guard_workers' not in entry:
            continue
        for name, spec in (compose.get('services') or {}).items():
            if ((spec.get('labels') or {}).get(BUDGET_LABEL) or '').strip() != entry['name']:
                continue
            command = [str(c) for c in spec.get('command') or []] if type(spec.get('command')) is list else []
            value = lambda flag: command[command.index(flag) + 1] if flag in command[:-1] else None  # noqa: E731
            pool = str((spec.get('environment') or {}).get('NEX_EIOS_DB_POOL_MAX', '4'))
            if (value('--guard-workers'), value('--dispatcher-pool-max') or '2', pool, value('--dispatch-loops') or '4') != (
                    str(entry['guard_workers']), str(entry['dispatcher_pool_max']), str(entry['guard_pool_max']), str(entry['dispatch_loops'])):
                problems.append('simulation_runtime_pools_differ:' + name)
    return {'checks': {'simulation_compose_matches_budget': not problems}, 'details': {'simulation_compose_budget': sorted(problems)}}


# Compose networks <-> firewall (G-APP-SUBNET, run sheet 4.2) -------------------------------------------------------------------
# The APP_HOST nft rules (app-host.nft.template) match these Compose networks by source CIDR; each must have exactly that subnet,
# declared (rendered Compose) and running (running_snapshot, docker network inspect). Names only in the details, never addresses.
FIREWALL_NETWORKS = {'app': 'APP_CONTAINER_CIDR', 'effect_egress': 'EFFECT_EGRESS_CIDR', 'agent_egress': 'AGENT_EGRESS_CIDR',
                     'claim_egress': 'CLAIM_EGRESS_CIDR'}


def _subnets(values):
    import ipaddress
    out = set()
    for v in values or ():
        try:
            out.add(ipaddress.ip_network(str(v), strict=False))
        except ValueError:
            out.add(str(v))
    return out


def firewall_networks(stage, compose, running=None):
    """stage: stage_render.load_env values; compose: the rendered APP_HOST Compose (config --format json); running: running.json
    (networks: {compose network: [subnets]}). Each firewall network has exactly the stage.env subnet; a missing input fails."""
    import ipaddress
    declared, live = [], []
    networks = (compose or {}).get('networks') or {}
    running_networks = None if running is None else running.get('networks')
    for network, variable in FIREWALL_NETWORKS.items():
        want = {ipaddress.ip_network(str(stage[variable]), strict=False)} if stage.get(variable) else None
        if want is None:
            declared.append('stage_env_missing:' + variable)
            continue
        got = _subnets(c.get('subnet') for c in (((networks.get(network) or {}).get('ipam') or {}).get('config') or []))
        if got != want:
            declared.append('subnet_differs:' + network)
        if running_networks is not None and _subnets(running_networks.get(network)) != want:
            live.append('subnet_differs:' + network)
    checks = {'compose_networks_match_firewall': not declared}
    details = {'compose_networks': declared}
    if running_networks is None:
        checks['running_networks_match_firewall'] = False
        details['running_networks'] = ['not_provided']
    else:
        checks['running_networks_match_firewall'] = not live
        details['running_networks'] = live
    return {'checks': checks, 'details': {'firewall_networks': details}}


# WebChat delivery provider profile (delivery CA, 2026-10-16) ------------------------------------------------------------------
WEBCHAT_ACTION = 'nexloop.service.request'


def _argument(command, flag):
    command = [str(c) for c in command or []]
    return command[command.index(flag) + 1] if flag in command and command.index(flag) + 1 < len(command) else None


def delivery_provider_profile(compose, uids=None):
    """The WebChat delivery service and the action worker's route to it must compute the same provider profile (origin, connect
    address, CA content: effect_provider.profile_identity), or the delivery authority (0175) refuses every delivery and each effect
    ends unknown. compose: the rendered APP_HOST Compose (container secret paths are mapped to their host files); the gate reads
    the files by their service uid. Details are codes only."""
    import json
    from nexloop_eios.effect_provider import profile_identity
    uids = uids or {}
    services = (compose or {}).get('services') or {}
    files = {name: (spec or {}).get('file') for name, spec in ((compose or {}).get('secrets') or {}).items()}
    def host(container_path):
        if not container_path or not container_path.startswith('/run/secrets/'):
            raise OwnedFileRefused('not_a_secret_path')
        path = files.get(container_path.removeprefix('/run/secrets/'))
        if not path:
            raise OwnedFileRefused('secret_not_declared')
        return path
    def read(container_path):
        path = host(container_path)
        return _read_owned_file(path, uids.get(Path(path).parent.name), maximum=32768)
    try:
        delivery = (services.get('webchat-delivery') or {}).get('command')
        worker = (services.get('action-worker') or {}).get('command')
        origin, delivery_ca = _argument(delivery, '--origin'), _argument(delivery, '--provider-ca-file')
        routes_file = _argument(worker, '--provider-routes-file')
        if not (origin and delivery_ca and routes_file):
            raise OwnedFileRefused('arguments_missing')
        route = json.loads(read(routes_file)).get('routes', {}).get(WEBCHAT_ACTION)
        if not route:
            raise OwnedFileRefused('route_missing')
        config = json.loads(read(route))
        mine = profile_identity(origin, None, read(delivery_ca))
        theirs = profile_identity(config['origin'], config.get('connect_address'), read(config['ca_file']))
    except OwnedFileRefused as error:
        return {'checks': {'webchat_delivery_profile_matches_route': False}, 'details': {'webchat_delivery_profile': ['input_' + error.code]}}
    except (KeyError, TypeError, ValueError):
        return {'checks': {'webchat_delivery_profile_matches_route': False}, 'details': {'webchat_delivery_profile': ['input_invalid']}}
    differs = sorted(f'{field}_differs' for field in ('origin', 'connect_address', 'trust_digest') if mine[field] != theirs[field])
    return {'checks': {'webchat_delivery_profile_matches_route': not differs}, 'details': {'webchat_delivery_profile': differs}}


# The gate (§6) ----------------------------------------------------------------------------------------------------------------
REQUIRED_INPUTS = {
    'app': ('stage_env', 'connection_budget', 'running', 'inventory_before', 'inventory_after', 'secret_root', 'compose_json', 'tls_spec',
            'roles_database_url_file', 'agent_host_config'),
    'data': ('stage_env', 'inventory_before', 'inventory_after', 'secret_root', 'tls_spec'),
}


def deploy_gate(spec):
    """Every pre-start check of one host from a private spec of input files; a missing input fails its check (never skipped)."""
    import json
    role = spec.get('host_role')
    if role not in REQUIRED_INPUTS:
        return {'checks': {'host_role': False}, 'details': {}}
    checks, details = {}, {}
    missing = [k for k in REQUIRED_INPUTS[role] if not spec.get(k)]
    for k in missing:
        checks[f'input_provided:{k}'] = False

    def merge(result):
        checks.update(result['checks'])
        details.update(result['details'])

    load = lambda key: json.loads(Path(spec[key]).read_text())
    stage = None
    if spec.get('stage_env'):
        from nexloop_eios import stage_render as module
        try:
            stage = module.load_env(spec['stage_env'])
            checks['stage_env_valid'] = True
        except module.Refused:
            checks['stage_env_valid'] = False
    if spec.get('tls_spec'):
        merge(tls(load('tls_spec')))
    if role == 'app' and spec.get('connection_budget'):
        merge(guard_and_registration(load('connection_budget'), load('running') if spec.get('running') else None))
    if role == 'app' and spec.get('connection_budget') and spec.get('compose_json'):
        merge(compose_budget(load('connection_budget'), load('compose_json')))
    # ADR-037: the simulation project (off by default) is checked whenever its rendered Compose is given; L6 review 2.1: when the
    # inventory shows the project running (docker compose ls: nexloop-simulation) and no Compose of it was given, the gate fails.
    if role == 'app' and spec.get('connection_budget'):
        running_simulation = set()
        for key in ('inventory_before', 'inventory_after'):
            if spec.get(key):
                running_simulation |= simulation_projects(load(key))
        if spec.get('simulation_compose_json'):
            merge(simulation_compose_budget(load('connection_budget'), load('simulation_compose_json')))
        elif running_simulation:
            checks['simulation_compose_matches_budget'] = False
            details['simulation_compose_budget'] = ['simulation_running_without_compose']
            details['simulation_projects_running'] = sorted(running_simulation)
    if role == 'app':
        if spec.get('compose_json'):
            merge(delivery_provider_profile(load('compose_json'), spec.get('secret_uids')))
        else:
            checks['webchat_delivery_profile_matches_route'] = False
    if role == 'app':
        if stage is not None and spec.get('compose_json'):
            merge(firewall_networks(stage, load('compose_json'), load('running') if spec.get('running') else None))
        else:
            checks['compose_networks_match_firewall'] = checks['running_networks_match_firewall'] = False
    if spec.get('inventory_before') and spec.get('inventory_after'):
        before, after = load('inventory_before'), load('inventory_after')
        merge(inventory_compare(before, after))
        if stage is not None:
            listening = list(after.get('listening', {}).values())
            merge(port_bindings(listening, stage, role))
            if before.get('phase') == 'baseline':
                # A baseline is taken with NexLoop running: its planned ports must be NexLoop's (no other service on them, the
                # public ones listening), not free.
                held = port_bindings(list(before.get('listening', {}).values()), stage, role)['details']['port_bindings']
                checks['planned_ports_nexloop_at_baseline'] = not held
                details['planned_ports_at_baseline'] = held
            else:
                conflicts = port_bindings(list(before.get('listening', {}).values()), stage, role, before_deploy=True)['details']['port_bindings']
                checks['planned_ports_free_before'] = not [p for p in conflicts if p.startswith('port_conflict')]
    if spec.get('secret_root'):
        merge(secret_distribution(spec['secret_root'], compose=load('compose_json') if spec.get('compose_json') else None, uids=spec.get('secret_uids')))
    if role == 'app' and spec.get('roles_database_url_file'):
        import psycopg
        from nexloop_eios.private_configuration import read_private_text
        try:
            with psycopg.connect(read_private_text(spec['roles_database_url_file'], maximum=16384), connect_timeout=5) as c, c.transaction():
                c.execute('set transaction read only')
                merge(roles(c, admin=tuple(spec.get('admin_roles', ADMIN_ROLES)), disabled=tuple(spec.get('disabled_roles', ('nexloop_bootstrap',)))))
        except psycopg.Error:
            checks['roles_readable'] = False
    if role == 'app' and spec.get('agent_host_config'):
        # The Agent Host's runtime configuration is owned by the Host uid (10020); the gate runs as root, so it reads it by that
        # uid (agent_host_config_uid, written by gate_dir) and reports why it could not.
        from nexloop_eios import host_concurrency
        try:
            text = _read_owned_file(spec['agent_host_config'], spec.get('agent_host_config_uid'), maximum=32768)
            result = host_concurrency.evaluate(host_concurrency.load(text))
            checks['agent_host_concurrency'] = result['passed']
            details['agent_host_concurrency'] = 'passed' if result['passed'] else 'limits_not_met'
        except OwnedFileRefused as error:
            checks['agent_host_concurrency'] = False
            details['agent_host_concurrency'] = 'file_' + error.code
        except Exception as error:  # noqa: BLE001 - the code names the kind of failure only
            checks['agent_host_concurrency'] = False
            details['agent_host_concurrency'] = 'configuration_invalid:' + type(error).__name__
    return {'checks': checks, 'details': details}
