"""G-CONTEXT-STRATEGY (docs/release/assembly-dryrun-gaps.md): the owner publishes the tenant's Context strategy the relay names
(--context-strategy recent_plus_required) through the governed Action nexloop.context.strategy.publish, as itself.

Only a human may publish a strategy (0088 refuses services and Agents), so this tool signs in as the owner: the owner's workbench
username and password come from private files (never arguments, never printed), the login is the workbench's own (local account,
the workbench browser application, the browser login rate limit), and the publication runs on the owner's browser business authority
(0045; the owner holds the Action through workbench roles v11, applied by `workbench_roles --apply`). The browser session is revoked
afterwards.

Recipe (synthetic example deploy/examples/nx037/context-strategy-setup.example.json):
  {schema_version: nexloop-context-strategy-setup/1, tenant_id, workbench_application_id, strategy_id, version, request_id}
The definition is the strategy of deploy/configuration/context-strategies.v1.json with that strategy_id and version. The request id
is a stable business-intent id: running the tool again replays the same publication (`replayed: true`), it never publishes twice.
Output: one JSON object of ids and codes. Exit 0 published or replayed, 2 refused.
"""
import argparse
import json
import re
import sys
import uuid
from pathlib import Path

RECIPE_SCHEMA = 'nexloop-context-strategy-setup/1'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class ContextStrategySetupRejected(ValueError):
    pass


def validate_recipe(value):
    if (type(value) is not dict or set(value) != {'schema_version', 'tenant_id', 'workbench_application_id', 'strategy_id', 'version', 'request_id'}
            or value['schema_version'] != RECIPE_SCHEMA or type(value['tenant_id']) is not str or not _UUID.fullmatch(value['tenant_id'])
            or any(type(value[k]) is not str or not _NAME.fullmatch(value[k]) for k in ('workbench_application_id', 'strategy_id', 'request_id'))
            or type(value['version']) is not int or value['version'] < 1):
        raise ContextStrategySetupRejected('recipe')
    return value


def strategy_definition(strategies_file, recipe):
    from nexloop_eios.context_engine.strategy import load_builtins
    found = [s for s in load_builtins(strategies_file) if s['strategy_id'] == recipe['strategy_id'] and s['version'] == recipe['version']]
    if len(found) != 1:
        raise ContextStrategySetupRejected('strategy not declared in the strategies file: ' + recipe['strategy_id'])
    return found[0]


def owner_session(identity_dsn, rate_key, recipe, username, password, origin):
    """The owner's workbench browser session, created exactly as the workbench login route creates it."""
    from eios.identity.errors import CredentialInvalid
    from eios.identity.evidence import AuthenticationEvidenceAuthority
    from eios.identity.local_accounts import LocalAccountService
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    from nexloop_eios.browser_identity import open_browser_identity
    from nexloop_eios.browser_rate_limits import PostgresBrowserRateLimiter, client_digest
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    import contextlib

    @contextlib.contextmanager
    def session():
        tenant, application = recipe['tenant_id'], recipe['workbench_application_id']
        with open_browser_identity(identity_dsn, pool_max=1) as pool:
            store = PostgresBrowserSessionUnitOfWork(pool, tenant_id=tenant, application_id=application)
            op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
            sessions = BrowserSessionService(store, store, application_id=application, operator=op)
            decision = PostgresBrowserRateLimiter(store.pool, identity_operator=True).consume(
                tenant, 'local_login', client_digest(rate_key, canonical_origin=origin, client_identifier='context-strategy-setup'), operator=op)
            if not decision.allowed:
                raise ContextStrategySetupRejected('login rate limited')
            passwords = LocalAccountService(store, AuthenticationEvidenceAuthority(store=store), subject_repository=store, tenant_id=tenant,
                                            application_id=application, operator=op, failure_result_verifier=store.verify_failure_result)
            try:
                auth = passwords.authenticate(tenant, username, password)
            except CredentialInvalid:
                raise ContextStrategySetupRejected('owner login refused') from None
            now = store.current_time()
            members = [m for m in store.list_memberships(auth.subject_id) if m.tenant_id == tenant and m.status == 'active' and m.valid_from <= now
                       and (m.valid_until is None or now < m.valid_until)]
            if len(members) != 1:
                raise ContextStrategySetupRejected('owner login refused')
            issued = sessions.create(tenant, auth.subject_id, members[0].principal_id, auth.evidence)
            try:
                yield issued.session
            finally:
                sessions.revoke(issued.session_token.get_secret_value(), issued.csrf_token.get_secret_value())
    return session()


def publish(recipe, definition, *, database_url_file, identity_database_url_file, signing_key_file, signing_key_id, artifact_root,
            username_file, password_file, browser_rate_key_file, origin):
    from nexloop_eios.backend import open_backend
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.context_engine.strategy import StrategyPublisher
    from nexloop_eios.trusted_configuration import read_private_text
    username = read_private_text(username_file, maximum=1024).strip()
    password = read_private_text(password_file, maximum=1024).strip()
    rate_key = bytes.fromhex(read_private_text(browser_rate_key_file, maximum=256).strip())
    with open_backend(database_url=read_private_text(database_url_file, maximum=16384).strip(), artifact_root=artifact_root,
                      signing_key_file=signing_key_file, signing_key_id=signing_key_id) as api, \
            owner_session(read_private_text(identity_database_url_file, maximum=16384).strip(), rate_key, recipe, username, password, origin) as session:
        human = authenticate_browser_business(api._pool, session, world='real')
        result = StrategyPublisher(api._pool, human, api._signer).publish(definition, request_id=recipe['request_id'])
    return {'tenant_id': recipe['tenant_id'], 'strategy_ref': result['strategy_ref'], 'outcome_id': result['outcome_id'],
            'replayed': bool(result.get('replayed', False))}


def main(argv=None):
    p = argparse.ArgumentParser(description='The owner publishes the tenant Context strategy (G-CONTEXT-STRATEGY)')
    for name in ('--recipe', '--strategies-file', '--database-url-file', '--identity-database-url-file', '--signing-key-file', '--artifact-root',
                 '--username-file', '--password-file', '--browser-rate-key-file'):
        p.add_argument(name, type=Path, required=True)
    p.add_argument('--signing-key-id', required=True)
    p.add_argument('--browser-origin', required=True, help='the workbench origin the login rate limit is bound to')
    a = p.parse_args(argv)
    try:
        recipe = validate_recipe(json.loads(a.recipe.read_text(encoding='utf-8')))
        definition = strategy_definition(a.strategies_file, recipe)
        out = publish(recipe, definition, database_url_file=a.database_url_file, identity_database_url_file=a.identity_database_url_file,
                      signing_key_file=a.signing_key_file, signing_key_id=a.signing_key_id, artifact_root=a.artifact_root, username_file=a.username_file,
                      password_file=a.password_file, browser_rate_key_file=a.browser_rate_key_file, origin=a.browser_origin)
        print(json.dumps(out, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - fixed text only: a database or login error may carry connection details
        print(json.dumps({'ok': False, 'reason': str(error) if isinstance(error, ContextStrategySetupRejected) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
