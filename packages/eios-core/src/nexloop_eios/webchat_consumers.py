"""G-WEBCHAT-AUTH (docs/release/assembly-dryrun-gaps.md): the webchat consumer role -> browser business authority, through trusted
configuration only (the 0050 manifest, technical configurator), the way workbench_roles grants staff.

A webchat Human's API requests are authorized through the browser business authority snapshot (0045,
authz.nexloop_browser_identity_snapshot): it needs a control.nexloop_browser_business_applications row for the browser application
(caller application, version, requested scopes), that caller application's `application` fact, and the Human's own grant facts.
This module compiles all three from

* public: deploy/authorization/webchat-consumer-role.v<N>.json, the Actions and Functions a consumer executes (open a conversation,
  write a message, read it, read a message's receipt and scope denial); only the CONSUMER_RESOURCES below may appear;
* private recipe (never committed; synthetic example deploy/examples/nx037/webchat-consumers.example.json):
  {schema_version: nexloop-webchat-consumers/1, tenant_id, browser_application_id, caller_application_id, consumers: [{principal_id,
  subject_id, persona}]}. Consumers are existing browser Humans the identity operator created (0050 refuses a Human grant without a
  browser membership); nothing here creates a Human. `persona` is the trial's marker: v0.1 only allows simulated_agent (the customers
  are AI agents the owner operates); it stays in the recipe and the output, the database has no persona marker (NX-037 trial guards).

Applying sends one 0050 manifest: authority_facts (the caller application restricted to the role's resources, execute only, and each
consumer's facts from workbench_roles.human_facts) and browser_business_applications (one row). Same input, same state: facts are
upserted only when they differ and the application row is replaced with the same values (authority_revision still advances, as for
every 0050 publication). To remove a consumer, apply the recipe with `revoke: true` for it (empty grant sets, never a delete).
"""
import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from eios.authz import facts as F
from eios.authz.applications import ApplicationMode, OperationRestriction, ResourceRestriction
from eios.authz.grants import GrantSubjectKind
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType

from nexloop_eios.authorization import WITNESS
from nexloop_eios.workbench_roles import human_facts, publish_human_authority

ROLE_SCHEMA = 'nexloop-webchat-consumer-role/1'
CONSUMERS_SCHEMA = 'nexloop-webchat-consumers/1'
APPLICATION_VERSION = '1'
SCOPES = ('action.execute', 'function.execute')
PERSONAS = ('simulated_agent',)
# What a webchat consumer may ever be granted (web_chat_http: create a conversation, send and read messages, a message's receipt and
# scope denial). A role file naming anything else is refused: staff and owner Actions are the workbench's.
CONSUMER_RESOURCES = {'eios:action:Conversation.create:1': ResourceType.ACTION, 'eios:action:Message.create:1': ResourceType.ACTION,
                      'eios:action:nexloop.conversation.read:1': ResourceType.ACTION,
                      'eios:function:nexloop.conversation.service_receipt:1': ResourceType.FUNCTION,
                      'eios:function:nexloop.conversation.scope_denial:1': ResourceType.FUNCTION}
ROLE_ID = 'nexloop-webchat-role:consumer'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class WebchatConsumersRejected(ValueError):
    pass


def validate_role(value):
    if (type(value) is not dict or set(value) != {'schema_version', 'manifest_version', 'decision', 'resources'} or value['schema_version'] != ROLE_SCHEMA
            or type(value['manifest_version']) is not int or value['manifest_version'] < 1 or type(value['decision']) is not str or not value['decision'].strip()):
        raise WebchatConsumersRejected('role manifest')
    resources = value['resources']
    if (type(resources) is not list or not resources or len(set(resources)) != len(resources) or resources != sorted(resources)
            or any(r not in CONSUMER_RESOURCES for r in resources)):
        raise WebchatConsumersRejected('role resources (sorted, unique, consumer resources only)')
    return value


def validate_recipe(value):
    if (type(value) is not dict or set(value) != {'schema_version', 'tenant_id', 'browser_application_id', 'caller_application_id', 'consumers'}
            or value['schema_version'] != CONSUMERS_SCHEMA or type(value['tenant_id']) is not str or not _UUID.fullmatch(value['tenant_id'])
            or type(value['browser_application_id']) is not str or not _NAME.fullmatch(value['browser_application_id'])
            or type(value['caller_application_id']) is not str or not value['caller_application_id'].startswith('eios:application:')
            or not _NAME.fullmatch(value['caller_application_id'].removeprefix('eios:application:'))
            or type(value['consumers']) is not list or not 1 <= len(value['consumers']) <= 1000):
        raise WebchatConsumersRejected('consumers recipe')
    seen = set()
    for c in value['consumers']:
        if (type(c) is not dict or not {'principal_id', 'subject_id', 'persona'} <= set(c) <= {'principal_id', 'subject_id', 'persona', 'revoke'}
                or any(type(c[k]) is not str or not _NAME.fullmatch(c[k]) for k in ('principal_id', 'subject_id')) or c['persona'] not in PERSONAS
                or c.get('revoke', False) not in (True, False) or c['principal_id'] in seen):
            raise WebchatConsumersRejected('consumer')
        seen.add(c['principal_id'])
    return value


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def application_fact(role, tenant, caller_application_id, *, valid_from):
    """The webchat caller application: restricted to the role's resources, execute only."""
    resources = role['resources']
    digest = F.canonical_authority_digest({'schema': ROLE_SCHEMA, 'application': caller_application_id, 'version': str(role['manifest_version']),
                                           'resources': resources})
    return ('application', [caller_application_id, APPLICATION_VERSION], F.ApplicationFacts(tenant_id=tenant, repository_witness=WITNESS,
        application_id=caller_application_id, application_revision=1, version=APPLICATION_VERSION, version_revision=role['manifest_version'],
        version_digest=digest, record_digest=digest, application_status='active', version_status='published', eligible=True,
        mode=ApplicationMode.RESTRICTED, break_glass=False,
        resources=tuple(ResourceRestriction(tenant_id=tenant, resource_type=CONSUMER_RESOURCES[r].value, resource_id=r) for r in resources),
        operations=(OperationRestriction(operation=Operation.EXECUTE),), published_at=valid_from, revoked_at=None, expires_at=None))


def compile_recipe(role, recipe, *, valid_from=None):
    """{'authority_facts': [...], 'browser_business_applications': [one row]} for the 0050 manifest. Deterministic."""
    validate_role(role)
    validate_recipe(recipe)
    valid_from = valid_from or datetime(2026, 10, 15, tzinfo=UTC)
    tenant = recipe['tenant_id']
    resources = [(r, CONSUMER_RESOURCES[r]) for r in role['resources']]
    facts = [application_fact(role, tenant, recipe['caller_application_id'], valid_from=valid_from)]
    for c in recipe['consumers']:
        if c.get('revoke'):
            facts += [('grants', [c['principal_id'], rid], F.GrantFacts(tenant_id=tenant, repository_witness=WITNESS, subject_kind=GrantSubjectKind.HUMAN,
                       principal_id=c['principal_id'], grants=(), valid_until=None, complete=True, next_cursor=None, revision=1)) for rid, _ in resources]
            continue
        facts += human_facts(tenant, c['principal_id'], c['subject_id'], ROLE_ID, role['manifest_version'], resources, grant_prefix='webchat-consumer',
                             binding_id='webchat-route', clearance_id='webchat-clearance', policy_set_id='webchat-world', valid_from=valid_from,
                             catalog_scopes=frozenset(SCOPES))
    out = {}
    for kind, key, fact in facts:
        out[(kind, tuple(key))] = {'kind': kind, 'key': key, 'payload': fact.model_dump(mode='json')}
    return {'authority_facts': [out[k] for k in sorted(out)],
            'browser_business_applications': [{'application_id': recipe['browser_application_id'], 'caller_application_id': recipe['caller_application_id'],
                                               'application_version': APPLICATION_VERSION, 'requested_scopes': list(SCOPES)}]}


def summary(recipe):
    return {'tenant_id': recipe['tenant_id'], 'browser_application_id': recipe['browser_application_id'],
            'granted': sum(1 for c in recipe['consumers'] if not c.get('revoke')), 'revoked': sum(1 for c in recipe['consumers'] if c.get('revoke')),
            'personas': sorted({c['persona'] for c in recipe['consumers']})}


def apply(role, recipe, *, database_url_file, signing_key_file, signing_key_id):
    """One 0050 manifest through the technical configurator (trusted_configuration refuses any other session). The tenant and the
    browser application must exist (register, browser), and every consumer must be an existing browser Human (0050)."""
    compiled = compile_recipe(role, recipe)
    revision = publish_human_authority(recipe['tenant_id'], authority_facts=compiled['authority_facts'],
                                       business_application=compiled['browser_business_applications'][0],
                                       humans=[c['principal_id'] for c in recipe['consumers']], label='webchat-consumers', database_url_file=database_url_file,
                                       signing_key_file=signing_key_file, signing_key_id=signing_key_id, refused=WebchatConsumersRejected)
    return {'configured': True, 'authority_revision': revision, **summary(recipe)}


def main(argv=None, *, window=None):
    """--check / --compile-facts / --apply. `window` (scripts/deploy/webchat_consumers.py) maps --bootstrap-dsn-file to a configurator
    DSN file for the duration of the apply; without it --apply needs --configurator-dsn-file."""
    p = argparse.ArgumentParser(description='Webchat consumer role -> browser business authority (G-WEBCHAT-AUTH), trusted configuration only')
    p.add_argument('--role', type=Path, required=True)
    p.add_argument('--recipe', type=Path)
    p.add_argument('--configurator-dsn-file', type=Path)
    p.add_argument('--bootstrap-dsn-file', type=Path)
    p.add_argument('--signing-key-file', type=Path)
    p.add_argument('--signing-key-id')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--compile-facts', action='store_true')
    mode.add_argument('--apply', action='store_true')
    a = p.parse_args(argv)
    try:
        role = validate_role(load(a.role))
        if a.check:
            if a.recipe is not None:
                validate_recipe(load(a.recipe))
            print(json.dumps({'valid': True, 'manifest_version': role['manifest_version']}))
            return 0
        if a.recipe is None:
            raise WebchatConsumersRejected('recipe required')
        recipe = validate_recipe(load(a.recipe))
        if a.compile_facts:
            print(json.dumps(compile_recipe(role, recipe), ensure_ascii=False, sort_keys=True))
            return 0
        if not (a.signing_key_file and a.signing_key_id) or (a.configurator_dsn_file is None) == (a.bootstrap_dsn_file is None):
            raise WebchatConsumersRejected('apply needs one of --configurator-dsn-file / --bootstrap-dsn-file, --signing-key-file and --signing-key-id')
        def run(dsn):
            return apply(role, recipe, database_url_file=dsn, signing_key_file=a.signing_key_file, signing_key_id=a.signing_key_id)
        if a.bootstrap_dsn_file is not None:
            if window is None:
                raise WebchatConsumersRejected('--bootstrap-dsn-file: use scripts/deploy/webchat_consumers.py')
            with window(a.bootstrap_dsn_file) as dsn:
                out = run(dsn)
        else:
            out = run(a.configurator_dsn_file)
        print(json.dumps(out, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'valid': False, 'reason': str(error) if isinstance(error, WebchatConsumersRejected) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
