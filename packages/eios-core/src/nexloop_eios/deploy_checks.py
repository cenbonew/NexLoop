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
    """spec: {"ca": path, "certificates": [{"name", "chain", "key", "purpose", "names"|"cn", "uid"?}], "dsn_files": [{name, path}],
    "valkey_conf": path}. Paths come from the private deployment; results carry names and codes only."""
    certs = {c['name']: check_certificate(chain=c['chain'], key=c['key'], ca=spec['ca'], purpose=c['purpose'], names=c.get('names', ()),
                                          cn=c.get('cn'), uid=c.get('uid'), now=now) for c in spec.get('certificates', [])}
    dsns = {}
    for item in spec.get('dsn_files', []):
        try:
            from nexloop_eios.private_configuration import read_private_text
            dsns[item['name']] = check_dsn(read_private_text(item['path'], maximum=16384))
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
    dispatch_open = []
    if compose is not None:
        for svc, spec in (compose.get('services') or {}).items():
            env = spec.get('environment') or {}
            for key, value in env.items():
                if _SECRET_ENV.search(key) and not key.endswith('_FILE'):
                    problems.append(f'secret_in_environment:{svc}')
                if key == 'REAL_DISPATCH_ENABLED' and str(value).lower() != 'false':
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


def inventory_compare(before, after, *, prefix='nexloop'):
    """Only NexLoop additions are allowed between the read-only inventories taken before and after a deployment step: every item
    that existed before is unchanged and present, and the firewall rule set differs only by the NexLoop table. Counts only."""
    problems, added = [], {}
    for section in INVENTORY_SECTIONS:
        b, a = before.get(section, {}), after.get(section, {})
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
    if before.get('firewall_without_nexloop_sha256') != after.get('firewall_without_nexloop_sha256'):
        problems.append('changed:firewall')
    return {'checks': {'existing_services_unchanged': not problems}, 'details': {'inventory': {'problems': sorted(set(problems)), 'added': added}}}


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
    return {'checks': {'compose_matches_budget': not problems}, 'details': {'compose_budget': sorted(problems)}}


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
        from nexloop_eios import host_concurrency
        from nexloop_eios.private_configuration import read_private_text
        try:
            checks['agent_host_concurrency'] = host_concurrency.evaluate(host_concurrency.load(read_private_text(spec['agent_host_config'], maximum=32768)))['passed']
        except Exception:
            checks['agent_host_concurrency'] = False
    return {'checks': checks, 'details': details}
