"""ADR-023 §3 / NX-026 D6: declared effect categories (deploy/configuration/action-effect-categories.v*.json).

A contact restriction stops only effects that reach the customer. Each effect Action declares customer_contact or
non_contact_service (with the input parameters that carry a customer notification). The declaration is applied by
trusted configuration only (``nexloop_configurator``) into control.nexloop_action_effect_categories (0112), frozen by
the digest of the tenant's currently published definition; anything undeclared is treated as customer_contact.
"""
import argparse
import json
from pathlib import Path
import re
import sys

from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.trusted_configuration import configurator_connection

SCHEMA='nexloop-action-effect-categories/1'
TOP={'schema_version','manifest_version','decision','actions'}
ENTRY={'stable_name','version','category','notification_parameters','purpose'}


class EffectCategoriesRejected(ValueError):
    pass


def validate(value):
    if type(value) is not dict or set(value)!=TOP or value['schema_version']!=SCHEMA or type(value['manifest_version']) is not int or value['manifest_version']<1:
        raise EffectCategoriesRejected('manifest shape')
    if type(value['decision']) is not str or not value['decision'].strip():raise EffectCategoriesRejected('decision')
    if type(value['actions']) is not list or not 1<=len(value['actions'])<=256:raise EffectCategoriesRejected('actions')
    seen=set()
    for item in value['actions']:
        if type(item) is not dict or set(item)!=ENTRY:raise EffectCategoriesRejected('entry shape')
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,159}',str(item['stable_name'])) is None or type(item['version']) is not int or item['version']<1:
            raise EffectCategoriesRejected('action identity')
        if (item['stable_name'],item['version']) in seen:raise EffectCategoriesRejected('duplicate action')
        seen.add((item['stable_name'],item['version']))
        if item['category'] not in ('customer_contact','non_contact_service'):raise EffectCategoriesRejected('category')
        params=item['notification_parameters']
        if (type(params) is not list or len(params)>32 or params!=sorted(set(params))
                or any(type(p) is not str or re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',p) is None for p in params)):
            raise EffectCategoriesRejected('notification parameters')
        if item['category']=='customer_contact' and params:raise EffectCategoriesRejected('customer_contact Actions are notifications as a whole')
        if type(item['purpose']) is not str or not item['purpose'].strip():raise EffectCategoriesRejected('purpose')
    return value


def load(path):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))


def apply(manifest,tenant,*,database_url_file,world='real'):
    """Trusted configuration only; refuses any session other than the technical configurator."""
    validate(manifest)
    db=configurator_connection(database_url_file)
    with db,db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_configure_effect_categories(%s,%s,%s::jsonb)',(tenant,world,canonical_payload(manifest))).fetchone()[0]


def main(argv=None):
    p=argparse.ArgumentParser(description='Apply declared effect categories (ADR-023 §3) through trusted configuration')
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--tenant',required=True);p.add_argument('--database-url-file',type=Path)
    mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--check',action='store_true');mode.add_argument('--apply',action='store_true')
    a=p.parse_args(argv)
    try:
        manifest=load(a.manifest)
        if a.apply:
            if a.database_url_file is None:raise EffectCategoriesRejected('database url file required')
            result=apply(manifest,a.tenant,database_url_file=a.database_url_file)
        else:result={'checked':True,'actions':len(manifest['actions'])}
    except Exception as error:
        print(json.dumps({'ok':False,'error':type(error).__name__}),file=sys.stdout);return 2
    print(canonical_payload(result));return 0


if __name__=='__main__':raise SystemExit(main())
