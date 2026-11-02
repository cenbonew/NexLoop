"""Claim auto-write whitelist (plan docs/implementation/claim-to-consumer-property-plan.md §9.1 and §10 item 1; migration
claim_auto_write): the (type, property) pairs the claim matcher may write without review. The owner keeps it in a manifest,
deploy/configuration/auto-write.v<N>.json, published by the trusted configuration identity (scripts/deploy/auto_write_apply.py):

    {"schema_version": "nexloop-auto-write/1", "version": N, "world": "real", "properties": [{"type_name": "Consumer", "property_name": "..."}]}

Empty means default deny. Identity, contact, address and payment properties are refused by SQL whatever the manifest says
(control.nexloop_auto_write_sensitive); this module checks the shape only, so a sensitive entry is refused by the database, never
silently dropped here.
"""
import json
import re
from pathlib import Path

from nexloop_eios.postgres_artifacts import canonical_payload

SCHEMA = 'nexloop-auto-write/1'
_TYPE = re.compile(r'[A-Z][A-Za-z0-9_]{0,63}')
_PROPERTY = re.compile(r'[a-z][a-z0-9_]{0,63}')
_WORLD = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}')


class AutoWriteRejected(ValueError):
    pass


def validate_manifest(value):
    if type(value) is not dict or set(value) != {'schema_version', 'version', 'world', 'properties'} or value['schema_version'] != SCHEMA:
        raise AutoWriteRejected('manifest')
    if type(value['version']) is not int or isinstance(value['version'], bool) or not 1 <= value['version'] <= 999999999:
        raise AutoWriteRejected('version')
    if type(value['world']) is not str or not _WORLD.fullmatch(value['world']):
        raise AutoWriteRejected('world')
    entries = value['properties']
    if type(entries) is not list or len(entries) > 64 or any(
            type(e) is not dict or set(e) != {'type_name', 'property_name'} or type(e['type_name']) is not str or not _TYPE.fullmatch(e['type_name'])
            or type(e['property_name']) is not str or not _PROPERTY.fullmatch(e['property_name']) for e in entries):
        raise AutoWriteRejected('properties')
    if len({(e['type_name'], e['property_name']) for e in entries}) != len(entries):
        raise AutoWriteRejected('properties repeated')
    return value


def load_manifest(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except ValueError:
        raise AutoWriteRejected('manifest is not JSON') from None
    return validate_manifest(value)


def apply_manifest(manifest, tenant, *, database_url_file):
    """Technical configurator only (SQL refuses any other session)."""
    from nexloop_eios.trusted_configuration import configurator_connection
    validate_manifest(manifest)
    db = configurator_connection(database_url_file)
    with db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_configure_auto_write(%s,%s::jsonb)', (tenant, canonical_payload(manifest))).fetchone()[0]
