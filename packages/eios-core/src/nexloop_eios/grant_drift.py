"""E1 (docs/design/eios-capability-gap.md; L6 review I-2): the database's actual privileges against the ones the migrations give.

The expected set is not parsed from the migration text: it is the privilege set of a clean database bootstrapped from the same catalog
(every GRANT/REVOKE, DO-block dynamic SQL, ALTER DEFAULT PRIVILEGES and owner default applied by PostgreSQL itself). Both sides are read
with the same catalog query (any role can read it; nothing is written):

  relation  schema.name               tables, partitioned tables, views, materialized views, sequences, foreign tables
  column    schema.name.column        column-level grants
  function  schema.name(args)         functions and procedures (EXECUTE, the owner's default included)
  schema    name                      USAGE / CREATE
  default   role/schema/objtype       ALTER DEFAULT PRIVILEGES entries

One entry: kind|object|grantee|privilege|grantable. A NULL ACL is expanded to the owner's default (acldefault), so a function nobody
revoked from PUBLIC shows PUBLIC EXECUTE on both sides. Extension members, the system schemas and temporary schemas are out of scope.
Grantees: PUBLIC; a superuser as <superuser> (the bootstrap account is named differently per cluster); any other role by name.
Design after the upstream ops/postgres/verify_role_grants.py (NEX-EIOS 1e363db; it replays GRANT/REVOKE text for two roles over three
schemas); no code is copied.
"""
import json

SCHEMA_VERSION = 'nexloop-grant-snapshot/1'

_SCOPE = """n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg\\_toast%%' and n.nspname not like 'pg\\_temp%%'"""
_NOT_EXTENSION = """not exists(select 1 from pg_catalog.pg_depend d where d.classid=%s::regclass and d.objid=%s and d.deptype='e')"""
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
select 'function', p.oid::regprocedure::text, {_GRANTEE}, a.privilege_type, a.is_grantable
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
        connection.execute('set transaction read only')
        return {entry(row) for row in connection.execute(SNAPSHOT).fetchall()}


def document(entries, *, catalog_revision):
    return {'schema': SCHEMA_VERSION, 'catalog_revision': catalog_revision, 'entries': sorted(entries)}


def load(path):
    data = json.loads(path.read_text())
    if type(data) is not dict or data.get('schema') != SCHEMA_VERSION or type(data.get('entries')) is not list \
            or not all(type(e) is str and e.count('|') >= 4 for e in data['entries']):
        raise ValueError('not a grant snapshot')
    return data


def ignored(item, ignore=None):
    from fnmatch import fnmatchcase
    ignore = IGNORED if ignore is None else ignore
    return next((reason for pattern, reason in ignore if fnmatchcase(item, pattern)), None)


def compare(expected, actual, ignore=None):
    """missing: expected but not granted; extra: granted but not expected; ignored: either side matched by an IGNORED pattern
    (listed with its reason, never counted)."""
    out = {'missing': [], 'extra': [], 'ignored': []}
    for side, items in (('missing', set(expected) - set(actual)), ('extra', set(actual) - set(expected))):
        for item in sorted(items):
            reason = ignored(item, ignore)
            if reason is None:
                out[side].append(item)
            else:
                out['ignored'].append({'side': side, 'entry': item, 'reason': reason})
    out['match'] = not out['missing'] and not out['extra']
    return out
