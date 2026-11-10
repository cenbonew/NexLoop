"""Raise (or set) an existing EffectControl's budget through the governed Actions only (stage 2026-11-10, customer-5).

    python -m nexloop_eios.effect_control_budget --database-url-file F --signing-key-file F --artifact-root D \
        --owner-credential-file F --editor-credential-file F --executor-credential-file F --recipe-file F [--dry-run]

Recipe (private file): {"schema_version": "1.0", "control_id": <64 hex>, "from_revision": <the object's current revision>,
"budget_units": <new cumulative cap>, "intent_id": <stable id>, "valid_until": <optional, future UTC>}.

Order: (1) the owner reads its own control and ledger (control.nexloop_effect_control_owner_read, 0448); (2) the editor runs
EffectControl.edit:1 at expected revision from_revision, intent intent_id (object revision from_revision + 1); (3) the owner runs
nexloop.effect.configure_control:1 at that revision with the executor credential (the ledger takes budget_units, control_revision and
valid_until; reserved_units is kept); (4) the owner reads again and checks object and ledger agree.

Three credentials. The owner is the control's owner_principal and must be the very token that first configured the control: the
configure updates the ledger only while owner_digest and executor_digest equal the stored ones (0446), so a rotated owner or executor
credential can never configure this control again. The editor is any service principal holding EffectControl.edit:1 and EDIT on the
object and on budget_units (and valid_until when the recipe sets it); it may be the owner, but giving the owner object or property
EDIT changes its requested scopes, which on stage means a rotation (credential_rotation_required) - hence a separate editor.

budget_units is a cumulative cap: remaining = budget_units - reserved_units. A value below the ledger's reserved_units or below its current cap is refused
before anything is written (the ledger's check would refuse the configure after the edit). The edit and the configure are two
governed Actions, two transactions: between them every bind of the consumer is refused (the ledger revision is behind the object).
That gap is closed by running the same recipe again: the edit is then a replay of the same intent (the claim returns its outcome,
nothing is edited twice) and the configure runs; a configure that already ran is a replay too. Any other state (the object moved on
under another intent, the ledger behind by more than this edit) is refused with a reason and nothing is written.

--dry-run reads only and prints the plan. Output: one JSON line; credentials, DSN and tokens are never printed.
"""
import argparse
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import sys

from eios.identity.models import SubjectKind
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.backend import open_backend
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text

EDIT = 'EffectControl.edit'
READ_SQL = 'select control.nexloop_effect_control_owner_read(%s,%s,%s)'
_ID = re.compile('[a-f0-9]{64}')
_INTENT = re.compile('[A-Za-z0-9._:-]{8,160}')
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)')
KEYS = {'schema_version', 'control_id', 'from_revision', 'budget_units', 'intent_id'}


class EffectControlBudgetUnavailable(RuntimeError):
    """reason is one fixed code (never a credential or a database message)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def strict_recipe(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError()
            out[key] = value
        return out

    def constant(unused):
        raise ValueError()
    try:
        recipe = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if type(recipe) is not dict or not KEYS <= set(recipe) <= KEYS | {'valid_until'} or recipe['schema_version'] != '1.0':
            raise ValueError()
        if type(recipe['control_id']) is not str or not _ID.fullmatch(recipe['control_id']):
            raise ValueError()
        if type(recipe['from_revision']) is not int or not 1 <= recipe['from_revision'] <= 9007199254740990:
            raise ValueError()
        if type(recipe['budget_units']) is not int or not 1 <= recipe['budget_units'] <= 1000000:
            raise ValueError()
        if type(recipe['intent_id']) is not str or not _INTENT.fullmatch(recipe['intent_id']):
            raise ValueError()
        if 'valid_until' in recipe:
            stamp = recipe['valid_until']
            if type(stamp) is not str or not _STAMP.fullmatch(stamp) or _instant(stamp) <= datetime.now(UTC):
                raise ValueError()
        return recipe
    except Exception:
        raise EffectControlBudgetUnavailable('recipe_invalid') from None


def _instant(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00'))


def _service(backend, path):
    service = backend.authenticate(read_private_text(path, maximum=16384), world='real')
    session = service._session
    if session.world != 'real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:
        raise ValueError()
    return service


def owner_read(backend, service, control_id):
    """The owner's read of its own control and ledger (0448); never a write."""
    with backend._lock:
        backend._assert_open()
        with backend._pool.connection() as db:
            verify_application_role(db)
            return db.execute(READ_SQL, (service._session.token_digest, 'real', control_id)).fetchone()[0]


def _same_valid_until(state, recipe):
    return 'valid_until' not in recipe or (state['valid_until'] is not None and _instant(state['valid_until']) == _instant(recipe['valid_until']))


def plan(state, recipe):
    """The phase from the current state: edit (first run), configure (the edit of this recipe ran, the configure did not), done
    (both ran: a replay), or a refusal reason."""
    start, end, ledger = recipe['from_revision'], recipe['from_revision'] + 1, state['ledger']
    if ledger is None:
        raise EffectControlBudgetUnavailable('control_not_configured')
    if recipe['budget_units'] < ledger['reserved_units']:
        raise EffectControlBudgetUnavailable('below_reserved')
    if state['object_revision'] == start and recipe['budget_units'] < ledger['budget_units']:
        # reserved_units can grow between this read and the configure, but never past the current cap: a value at or above the
        # cap can never fail the ledger's check after the edit committed. Lowering is not this tool's job.
        raise EffectControlBudgetUnavailable('below_current_cap')
    if state['object_revision'] == start:
        if ledger['control_revision'] != start:
            raise EffectControlBudgetUnavailable('ledger_behind')
        if state['budget_units'] == recipe['budget_units'] and _same_valid_until(state, recipe):
            raise EffectControlBudgetUnavailable('unchanged')
        return 'edit'
    if state['object_revision'] == end and state['budget_units'] == recipe['budget_units'] and _same_valid_until(state, recipe):
        if ledger['control_revision'] == start:
            return 'configure'
        if ledger['control_revision'] == end:
            return 'done'
        raise EffectControlBudgetUnavailable('ledger_behind')
    raise EffectControlBudgetUnavailable('revision_moved')


def _summary(state, recipe, phase):
    ledger = state['ledger']
    return {'control_id': state['control_id'], 'from_revision': recipe['from_revision'], 'to_revision': recipe['from_revision'] + 1,
            'object_revision': state['object_revision'], 'ledger_revision': ledger['control_revision'],
            'budget_units': ledger['budget_units'], 'reserved_units': ledger['reserved_units'],
            'remaining': ledger['budget_units'] - ledger['reserved_units'], 'phase': phase}


def raise_budget(backend, *, owner_credential_file, editor_credential_file, executor_credential_file, recipe, dry_run=False):
    recipe = strict_recipe(canonical_payload(recipe))
    try:
        owner = _service(backend, owner_credential_file)
        tenant = owner._session.authentication.tenant_id
        if any(_service(backend, path)._session.authentication.tenant_id != tenant for path in (editor_credential_file, executor_credential_file)):
            raise ValueError()
    except Exception:
        raise EffectControlBudgetUnavailable('credential_unavailable') from None
    try:
        before = owner_read(backend, owner, recipe['control_id'])
    except Exception:
        raise EffectControlBudgetUnavailable('control_owner_unavailable') from None
    phase = plan(before, recipe)
    if dry_run:
        return {**_summary(before, recipe, phase), 'dry_run': True, 'planned_budget_units': recipe['budget_units'],
                'planned_remaining': recipe['budget_units'] - before['ledger']['reserved_units']}
    end = recipe['from_revision'] + 1
    properties = {'budget_units': recipe['budget_units']}
    if 'valid_until' in recipe:
        properties['valid_until'] = recipe['valid_until']
    try:
        # A replay when the edit of this intent already committed: the claim returns its outcome, nothing is edited twice.
        edited = _service(backend, editor_credential_file).edit_object(action_name=EDIT, action_version=1, intent_id=recipe['intent_id'],
            type_name='EffectControl', object_id=recipe['control_id'], expected_revision=recipe['from_revision'], properties=properties)
        if edited.get('object_id') != recipe['control_id'] or edited.get('revision') != end:
            raise ValueError()
    except Exception:
        raise EffectControlBudgetUnavailable('edit_unavailable') from None
    try:
        _service(backend, owner_credential_file).configure_effect_control(control_id=recipe['control_id'], control_revision=end,
            executor_token=read_private_text(executor_credential_file, maximum=16384))
    except Exception:
        # The object is at the new revision and the ledger is not: run the same recipe again (phase configure).
        raise EffectControlBudgetUnavailable('configure_unavailable') from None
    try:
        after = owner_read(backend, owner, recipe['control_id'])
    except Exception:
        raise EffectControlBudgetUnavailable('control_owner_unavailable') from None
    ledger = after['ledger']
    if after['object_revision'] != end or ledger is None or ledger['control_revision'] != end or ledger['budget_units'] != recipe['budget_units']:
        raise EffectControlBudgetUnavailable('verify_failed')
    return {**_summary(after, recipe, phase), 'replayed': phase != 'edit'}


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'Effect control budget configuration unavailable\n')


def main(argv=None):
    parser = _Parser(description=__doc__)
    for name in ('database-url-file', 'signing-key-file', 'owner-credential-file', 'editor-credential-file', 'executor-credential-file', 'recipe-file',
                 'artifact-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    logger = logging.getLogger('psycopg.pool')
    disabled = logger.disabled
    logger.disabled = True
    try:
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}', args.signing_key_id):
            raise EffectControlBudgetUnavailable('configuration_invalid')
        try:
            text = read_private_text(args.recipe_file, maximum=32768)
        except Exception:
            raise EffectControlBudgetUnavailable('recipe_invalid') from None
        recipe = strict_recipe(text)
        try:
            backend_context = open_backend(database_url=read_private_text(args.database_url_file, maximum=16384),
                artifact_root=args.artifact_root, signing_key_file=args.signing_key_file, signing_key_id=args.signing_key_id)
        except Exception:
            raise EffectControlBudgetUnavailable('backend_unavailable') from None
        with backend_context as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db) != 'nexloop_api':
                    raise EffectControlBudgetUnavailable('backend_unavailable')
            result = raise_budget(backend, owner_credential_file=args.owner_credential_file, editor_credential_file=args.editor_credential_file,
                                  executor_credential_file=args.executor_credential_file, recipe=recipe, dry_run=args.dry_run)
        print(canonical_payload(result))
        return 0
    except EffectControlBudgetUnavailable as error:
        print('Effect control budget unavailable: ' + error.reason, file=sys.stderr)
        return 1
    except Exception:
        print('Effect control budget unavailable: backend_unavailable', file=sys.stderr)
        return 1
    finally:
        logger.disabled = disabled


if __name__ == '__main__':
    raise SystemExit(main())
