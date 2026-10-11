"""NX-035 §6.3 (restore effect window): per-channel readers that list, from the channel's own durable records outside the
database, the external references of a time window. The restore tool hands their entries to runtime.nexloop_restore_reconcile
(one entry per tenant, world and action type: {tenant_id, world, action_type, time_queryable, external_intent_ids}).

A channel that cannot be queried by time, or whose records cannot be read completely and exactly, is reported with
time_queryable false and no references: the reconciliation then keeps its action type paused (NX-022) until the owner handles
it. A reader never guesses and never repairs the channel's records.
"""
import os
import re
import stat
from datetime import UTC, datetime

from nexloop_eios.local_json_delivery import DeliveryUnavailable, JsonExportStore, _decode

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_MANIFEST = re.compile(_UUID.pattern + r'\.manifest\.json')
MAXIMUM_ENTRIES = 100000


def not_queryable(*, tenant_id, world, action_type):
    """The entry of a channel without a time query: no references; the reconciliation pauses its action type."""
    return {'tenant_id': tenant_id, 'world': world, 'action_type': action_type, 'time_queryable': False, 'external_intent_ids': []}


def _window(window_start, window_end):
    for value in (window_start, window_end):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError('aware window bounds required')
    if window_end < window_start:
        raise ValueError('window end before start')
    return window_start.timestamp(), window_end.timestamp()


def local_json_evidence(root, *, tenant_id, world, action_type, window_start, window_end):
    """Local JSON delivery (NX-016): one immutable, fsynced manifest per delivered intent, published atomically (its
    modification time is the publication). Every manifest published in [window_start, window_end] is a reference. An entry
    that is not a regular private file of this owner, or a manifest that does not name its own intent, makes the channel
    not queryable (fail closed)."""
    start, end = _window(window_start, window_end)
    try:
        store = JsonExportStore(root)
    except (OSError, DeliveryUnavailable):
        return not_queryable(tenant_id=tenant_id, world=world, action_type=action_type)
    try:
        found = []
        with os.scandir(store.fd) as entries:
            names = []
            for entry in entries:
                if len(names) >= MAXIMUM_ENTRIES:
                    raise DeliveryUnavailable()
                names.append(entry.name)
        for name in sorted(names):
            if not _MANIFEST.fullmatch(name):
                continue
            info = os.stat(name, dir_fd=store.fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink not in (1, 2):
                raise DeliveryUnavailable()
            if not start <= info.st_mtime <= end:
                continue
            manifest = _decode(store._read(name, pending=True))
            intent = name.removesuffix('.manifest.json')
            if type(manifest) is not dict or manifest.get('intent_id') != intent:
                raise DeliveryUnavailable()
            found.append(intent)
        store._check()
    except (OSError, ValueError, DeliveryUnavailable):
        return not_queryable(tenant_id=tenant_id, world=world, action_type=action_type)
    finally:
        store.close()
    return {'tenant_id': tenant_id, 'world': world, 'action_type': action_type, 'time_queryable': True, 'external_intent_ids': found}


def incentive_ledger_evidence(directory, *, tenant_id, world, window_start, window_end, action_type='nexloop.incentive.issue'):
    """The AT-043 loopback incentive ledger (incentives.LocalIncentiveLedger): one fsynced JSON record per issued intent, named
    by the sha256 of its intent id and replaced atomically (its modification time is the issue). Every record written in the
    window is a reference. A record that is not a private regular file, is not valid, or does not match its own name makes
    the channel not queryable (fail closed)."""
    import hashlib
    import json
    start, end = _window(window_start, window_end)
    root = os.fspath(directory)
    try:
        info = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('ledger directory')
        found = []
        names = sorted(os.listdir(root))
        if len(names) > MAXIMUM_ENTRIES:
            raise ValueError('ledger too large')
        for name in names:
            if name.endswith('.tmp'):
                continue  # a write that never completed was never issued
            if not re.fullmatch(r'[0-9a-f]{64}\.json', name):
                raise ValueError('unexpected ledger entry')
            path = os.path.join(root, name)
            entry = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.geteuid() or entry.st_nlink != 1 or entry.st_size > 65536:
                raise ValueError('ledger record')
            if not start <= entry.st_mtime <= end:
                continue
            with open(path, encoding='utf-8') as handle:
                record = json.load(handle)
            intent = record.get('intent_id') if type(record) is dict else None
            if type(intent) is not str or hashlib.sha256(intent.encode()).hexdigest() + '.json' != name:
                raise ValueError('ledger record name')
            found.append(intent)
    except (OSError, ValueError):
        return not_queryable(tenant_id=tenant_id, world=world, action_type=action_type)
    return {'tenant_id': tenant_id, 'world': world, 'action_type': action_type, 'time_queryable': True, 'external_intent_ids': sorted(found)}


def utc(value):
    """Parse an ISO time as an aware UTC datetime (command line helper)."""
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('time zone required')
    return parsed.astimezone(UTC)
