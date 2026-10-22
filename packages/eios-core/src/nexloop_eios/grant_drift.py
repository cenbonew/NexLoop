"""E1 (docs/design/eios-capability-gap.md; L6 review I-2): the database's actual privileges against the ones the migrations give.

The expected set is not parsed from the migration text: it is the privilege set of a clean database bootstrapped from the same catalog
(every GRANT/REVOKE, DO-block dynamic SQL, ALTER DEFAULT PRIVILEGES and owner default applied by PostgreSQL itself) in a disposable
cluster the tool creates and removes itself; it never comes from an existing database. Both sides are read with the same catalog query
(any role can read the catalog; the transaction is read only and that is read back):

  relation  schema.name               tables, partitioned tables, views, materialized views, sequences, foreign tables
  column    schema.name.column        column-level grants
  function  schema.name(args)         functions and procedures (EXECUTE, the owner's default included); args as input types
  schema    name                      USAGE / CREATE
  default   role/schema/objtype       ALTER DEFAULT PRIVILEGES entries
  database  <current>                 the database's CONNECT / TEMPORARY / CREATE (its name differs per cluster)
  owner     kind:object               the owner of every relation, function and schema above and of the database
  role      name                      SUPERUSER, CREATEROLE, CREATEDB, REPLICATION, BYPASSRLS, INHERIT and per-role settings (SET ...)
  member    role                      a membership (member, its ADMIN / INHERIT / SET options), predefined pg_* roles included

One entry: kind|object|subject|privilege|option. A NULL ACL is expanded to the owner's default (acldefault), so a function nobody revoked
from PUBLIC shows PUBLIC EXECUTE on both sides. Names are built from pg_namespace/pg_proc with search_path set to pg_catalog, so the
reading role's search_path cannot change them. Extension members, the system schemas and temporary schemas are out of scope; so are
types, languages, large objects, foreign servers and parameter ACLs (none is granted by NexLoop migrations).
Roles: PUBLIC; a superuser as <superuser> (the bootstrap account is named differently per cluster); any other role by name. The cluster's
initial superuser (oid 10) has no role entry; every other non-predefined role has one, so a role a deployment or the runtime creates
outside the migrations (a login role of the stage, a world's or tenant's runtime role) shows as EXTRA with its grants and memberships.
LOGIN, CONNECTION LIMIT, passwords and VALID UNTIL are deployment-managed (the deploy enables logins; the configurator window opens one
for ten minutes) and are not compared. A first read-only run on the stage classifies such differences: into the migrations, or into
IGNORED with a reason.
Design after the upstream ops/postgres/verify_role_grants.py (NEX-EIOS 1e363db; it replays GRANT/REVOKE text for two roles over three
schemas); no code is copied.
"""
import json

SCHEMA_VERSION = 'nexloop-grant-snapshot/1'

_SCOPE = """n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg\\_toast%%' and n.nspname not like 'pg\\_temp%%'"""
_NOT_EXTENSION = """not exists(select 1 from pg_catalog.pg_depend d where d.classid=%s::regclass and d.objid=%s and d.deptype='e')"""
_FUNCTION = """n.nspname||'.'||p.proname||'('||pg_catalog.oidvectortypes(p.proargtypes)||')'"""
_ROLE = """case when %s then '<superuser>' else %s end"""
_GRANTEE = """case when a.grantee=0 then 'PUBLIC' when r.rolsuper then '<superuser>' else r.rolname end"""

SNAPSHOT = f"""
select 'relation', n.nspname||'.'||c.relname, {_GRANTEE}, a.privilege_type, a.is_grantable
from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace
cross join lateral pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault((case when c.relkind='S' then 's' else 'r' end)::"char",c.relowner))) a
left join pg_catalog.pg_roles r on r.oid=a.grantee
where c.relkind in ('r','p','v','m','S','f') and {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_class'", 'c.oid')}
union all
select 'column', n.nspname||'.'||c.relname||'.'||t.attname, {_GRANTEE}, a.privilege_type, a.is_grantable
from pg_catalog.pg_attribute t join pg_catalog.pg_class c on c.oid=t.attrelid join pg_catalog.pg_namespace n on n.oid=c.relnamespace
cross join lateral pg_catalog.aclexplode(t.attacl) a
left join pg_catalog.pg_roles r on r.oid=a.grantee
where t.attacl is not null and not t.attisdropped and {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_class'", 'c.oid')}
union all
select 'function', {_FUNCTION}, {_GRANTEE}, a.privilege_type, a.is_grantable
from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace
cross join lateral pg_catalog.aclexplode(coalesce(p.proacl,pg_catalog.acldefault('f'::"char",p.proowner))) a
left join pg_catalog.pg_roles r on r.oid=a.grantee
where {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_proc'", 'p.oid')}
union all
select 'schema', n.nspname, {_GRANTEE}, a.privilege_type, a.is_grantable
from pg_catalog.pg_namespace n
cross join lateral pg_catalog.aclexplode(coalesce(n.nspacl,pg_catalog.acldefault('n'::"char",n.nspowner))) a
left join pg_catalog.pg_roles r on r.oid=a.grantee
where {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_namespace'", 'n.oid')}
union all
select 'default', (case when o.rolsuper then '<superuser>' else o.rolname end)||'/'||coalesce(n.nspname,'*')||'/'||d.defaclobjtype::text,
       {_GRANTEE}, a.privilege_type, a.is_grantable
from pg_catalog.pg_default_acl d join pg_catalog.pg_roles o on o.oid=d.defaclrole left join pg_catalog.pg_namespace n on n.oid=d.defaclnamespace
cross join lateral pg_catalog.aclexplode(d.defaclacl) a
left join pg_catalog.pg_roles r on r.oid=a.grantee
union all
select 'database', '<current>', {_GRANTEE}, a.privilege_type, a.is_grantable
from pg_catalog.pg_database b cross join lateral pg_catalog.aclexplode(coalesce(b.datacl,pg_catalog.acldefault('d'::"char",b.datdba))) a
left join pg_catalog.pg_roles r on r.oid=a.grantee
where b.datname=pg_catalog.current_database()
union all
select 'owner', 'relation:'||n.nspname||'.'||c.relname, {_ROLE % ('o.rolsuper', 'o.rolname')}, 'OWNER', false
from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace join pg_catalog.pg_roles o on o.oid=c.relowner
where c.relkind in ('r','p','v','m','S','f') and {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_class'", 'c.oid')}
union all
select 'owner', 'function:'||{_FUNCTION}, {_ROLE % ('o.rolsuper', 'o.rolname')}, 'OWNER', false
from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace join pg_catalog.pg_roles o on o.oid=p.proowner
where {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_proc'", 'p.oid')}
union all
select 'owner', 'schema:'||n.nspname, {_ROLE % ('o.rolsuper', 'o.rolname')}, 'OWNER', false
from pg_catalog.pg_namespace n join pg_catalog.pg_roles o on o.oid=n.nspowner
where {_SCOPE} and {_NOT_EXTENSION % ("'pg_catalog.pg_namespace'", 'n.oid')}
union all
select 'owner', 'database:<current>', {_ROLE % ('o.rolsuper', 'o.rolname')}, 'OWNER', false
from pg_catalog.pg_database b join pg_catalog.pg_roles o on o.oid=b.datdba where b.datname=pg_catalog.current_database()
union all
select 'role', o.rolname, o.rolname, x.attribute, x.value
from pg_catalog.pg_roles o cross join lateral (values ('SUPERUSER',o.rolsuper),('CREATEROLE',o.rolcreaterole),('CREATEDB',o.rolcreatedb),
  ('REPLICATION',o.rolreplication),('BYPASSRLS',o.rolbypassrls),('INHERIT',o.rolinherit)) x(attribute,value)
where o.oid<>10 and o.rolname !~ '^pg_'
union all
select 'role', o.rolname, o.rolname, 'SET '||c.setting||case when s.setdatabase=0 then '' else ' (database <current>)' end, false
from pg_catalog.pg_db_role_setting s join pg_catalog.pg_roles o on o.oid=s.setrole cross join lateral pg_catalog.unnest(s.setconfig) c(setting)
where s.setrole<>0 and o.oid<>10 and o.rolname !~ '^pg_' and s.setdatabase in (0,(select oid from pg_catalog.pg_database where datname=pg_catalog.current_database()))
union all
select 'member', {_ROLE % ('g.rolsuper', 'g.rolname')}, {_ROLE % ('m.rolsuper', 'm.rolname')},
  'MEMBER'||case when a.inherit_option then ' INHERIT' else '' end||case when a.set_option then ' SET' else '' end, a.admin_option
from pg_catalog.pg_auth_members a join pg_catalog.pg_roles g on g.oid=a.roleid join pg_catalog.pg_roles m on m.oid=a.member
where m.oid<>10
"""

# Entries a live database may differ in, each with its reason; a pattern is fnmatch-style over the whole entry
# (kind|object|grantee|privilege|grantable). Empty until a reviewed difference needs one; never a way to silence an unexplained one.
IGNORED = (
)


def entry(row):
    kind, name, grantee, privilege, grantable = row
    return f'{kind}|{name}|{grantee}|{privilege}|{"grantable" if grantable else "plain"}'


def snapshot(connection):
    """The privilege entries of the connected database (read only: one catalog query in a read-only transaction)."""
    with connection.transaction():
        read_only(connection)
        connection.execute('set local search_path = pg_catalog')  # names never depend on the reading role's search_path
        return {entry(row) for row in connection.execute(SNAPSHOT).fetchall()}


def read_only(connection):
    """Make the current transaction read only and read that back (a refusal to write is the server's, not this module's promise)."""
    connection.execute('set transaction read only')
    if connection.execute('show transaction_read_only').fetchone()[0] != 'on':
        raise RuntimeError('the transaction is not read only')


def document(entries, *, catalog_revision):
    return {'schema': SCHEMA_VERSION, 'catalog_revision': catalog_revision, 'entries': sorted(entries)}


def load(path):
    data = json.loads(path.read_text())
    if type(data) is not dict or data.get('schema') != SCHEMA_VERSION or type(data.get('entries')) is not list \
            or not all(type(e) is str and e.count('|') >= 4 for e in data['entries']):
        raise ValueError('not a grant snapshot')
    return data


KINDS = ('relation', 'column', 'function', 'schema', 'default', 'database', 'owner', 'role', 'member')


def check_ignore(ignore):
    """An IGNORED pattern names its kind and its subject literally (no wildcard in either) and gives a reason."""
    for pattern, reason in ignore:
        fields = pattern.split('|')
        if len(fields) != 5 or fields[0] not in KINDS or any(ch in fields[2] for ch in '*?[') or not fields[1] \
                or type(reason) is not str or len(reason.strip()) < 10:
            raise ValueError(f'IGNORED pattern not allowed: {pattern}')
    return ignore


def ignored(item, ignore=None):
    from fnmatch import fnmatchcase
    ignore = IGNORED if ignore is None else ignore
    return next((reason for pattern, reason in ignore if fnmatchcase(item, pattern)), None)


def compare(expected, actual, ignore=None):
    """missing: expected but not granted; extra: granted but not expected; ignored: either side matched by an IGNORED pattern
    (listed with its reason, never counted)."""
    ignore = check_ignore(IGNORED if ignore is None else ignore)
    out = {'missing': [], 'extra': [], 'ignored': []}
    for side, items in (('missing', set(expected) - set(actual)), ('extra', set(actual) - set(expected))):
        for item in sorted(items):
            reason = ignored(item, ignore)
            if reason is None:
                out[side].append(item)
            else:
                out['ignored'].append({'side': side, 'entry': item, 'reason': reason})
    # A pattern that no longer matches anything is stale: it has to be removed, not kept as a silent allowance.
    from fnmatch import fnmatchcase
    out['stale_ignores'] = [pattern for pattern, _ in ignore if not any(fnmatchcase(i['entry'], pattern) for i in out['ignored'])]
    out['match'] = not out['missing'] and not out['extra'] and not out['stale_ignores']
    return out
