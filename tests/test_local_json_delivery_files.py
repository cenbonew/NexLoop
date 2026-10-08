"""Real filesystem delivery/journal tests, not EIOS or provider authorization.

No PG fixtures, registered migration, synthetic allowed authority or production
resources are used. Actual HTTPS/PG successful delivery remains a separate gate.
"""
import hashlib
import multiprocessing
import os
import signal
import stat
import uuid

import pytest
from nexloop_eios.local_json_delivery import JsonExportStore, DeliveryConflict, DeliveryUnavailable, FORMAT, _parameters
from nexloop_eios.postgres_artifacts import canonical_payload

PARAMETERS = {'message': 'A real durable export product.\n第二行'}
DIGEST = hashlib.sha256(canonical_payload(PARAMETERS).encode()).hexdigest()


@pytest.fixture
def delivery_root(tmp_path):
    root = tmp_path / 'delivery'; root.mkdir(mode=0o700)
    return root


def test_materializes_actual_export_with_private_permissions_and_readback(delivery_root):
    intent = str(uuid.uuid4()); store = JsonExportStore(delivery_root)
    try:
        assert store.query(intent, DIGEST)['state'] == 'not_found'
        result = store.deliver(intent, DIGEST, PARAMETERS)
        assert result['state'] == 'fulfilled'
        product = delivery_root / (intent + '.export.json')
        value = __import__('json').loads(product.read_bytes())
        assert value['document']['body'] == PARAMETERS['message'] and value['intent_id'] == intent
        assert stat.S_IMODE(product.stat().st_mode) == 0o600
        assert store.query(intent, DIGEST) == result
        before = product.stat().st_ino
        assert store.deliver(intent, DIGEST, PARAMETERS) == result
        assert product.stat().st_ino == before
    finally: store.close()


def test_reopen_preserves_product_and_conflicting_digest_cannot_overwrite(delivery_root):
    intent = str(uuid.uuid4()); first = JsonExportStore(delivery_root)
    first.deliver(intent, DIGEST, PARAMETERS); first.close()
    reopened = JsonExportStore(delivery_root)
    try:
        product = delivery_root / (intent + '.export.json'); before = product.read_bytes()
        assert reopened.query(intent, DIGEST)['state'] == 'fulfilled'
        other = {'message': 'Cannot overwrite'}
        digest = hashlib.sha256(canonical_payload(other).encode()).hexdigest()
        with pytest.raises(DeliveryConflict): reopened.deliver(intent, digest, other)
        assert product.read_bytes() == before
    finally: reopened.close()


def test_tampered_real_product_is_not_fulfilled(delivery_root):
    intent = str(uuid.uuid4()); store = JsonExportStore(delivery_root)
    try:
        store.deliver(intent, DIGEST, PARAMETERS)
        (delivery_root / (intent + '.export.json')).write_bytes(b'{"state":"fulfilled"}')
        with pytest.raises(DeliveryUnavailable): store.query(intent, DIGEST)
    finally: store.close()


def test_manifest_without_product_is_accepted_and_orphan_product_is_denied(delivery_root):
    intent = str(uuid.uuid4()); store = JsonExportStore(delivery_root)
    try:
        store.deliver(intent, DIGEST, PARAMETERS)
        (delivery_root / (intent + '.export.json')).unlink()
        assert store.query(intent, DIGEST)['state'] == 'accepted'
        (delivery_root / (intent + '.manifest.json')).unlink()
        (delivery_root / (intent + '.export.json')).write_bytes(b'orphan')
        with pytest.raises(DeliveryUnavailable): store.query(intent, DIGEST)
    finally: store.close()


@pytest.mark.parametrize('name', ['.manifest.json', '.export.json', '.lock'])
def test_symlink_named_product_journal_or_lock_never_traversed(delivery_root, tmp_path, name):
    intent = str(uuid.uuid4()); outside = tmp_path / 'outside'; outside.write_bytes(b'unchanged')
    (delivery_root / (intent + name)).symlink_to(outside)
    store = JsonExportStore(delivery_root)
    try:
        with pytest.raises((DeliveryUnavailable, OSError)): store.deliver(intent, DIGEST, PARAMETERS)
        assert outside.read_bytes() == b'unchanged'
    finally: store.close()


def test_world_readable_or_replaced_root_refused(delivery_root):
    delivery_root.chmod(0o755)
    with pytest.raises(DeliveryUnavailable): JsonExportStore(delivery_root)
    delivery_root.chmod(0o700); store = JsonExportStore(delivery_root)
    try:
        old = delivery_root.with_name('old'); delivery_root.rename(old); delivery_root.mkdir(mode=0o700)
        with pytest.raises(DeliveryUnavailable): store.deliver(str(uuid.uuid4()), DIGEST, PARAMETERS)
        assert list(delivery_root.iterdir()) == []
    finally: store.close()


def test_path_fields_and_large_escaped_document_refused():
    with pytest.raises(DeliveryUnavailable): _parameters({**PARAMETERS, 'path': '/tmp/arbitrary'})
    with pytest.raises(DeliveryUnavailable): _parameters({'message': '\x01' * 65536})


def _pause_after_durable_manifest(root, intent, pipe):
    store = JsonExportStore(root); original = store._immutable
    def pause(name, body):
        if name.endswith('.export.json'):
            pipe.send('manifest_durable'); pipe.recv()
        original(name, body)
    store._immutable = pause
    try: store.deliver(intent, DIGEST, PARAMETERS)
    finally: store.close()


def test_actual_sigkill_before_product_keeps_accepted_not_fabricated_fulfillment(delivery_root):
    context = multiprocessing.get_context('spawn'); parent, child = context.Pipe()
    intent = str(uuid.uuid4()); process = context.Process(target=_pause_after_durable_manifest, args=(delivery_root, intent, child))
    process.start(); child.close()
    try:
        assert parent.poll(10) and parent.recv() == 'manifest_durable'
        process.kill(); process.join(10); assert process.exitcode == -signal.SIGKILL
        store = JsonExportStore(delivery_root)
        try:
            assert store.query(intent, DIGEST)['state'] == 'accepted'
            assert not (delivery_root / (intent + '.export.json')).exists()
        finally: store.close()
    finally:
        if process.is_alive(): process.kill(); process.join(10)
        parent.close()


def _pause_after_link(root, intent, suffix, pipe):
    store = JsonExportStore(root); original = os.link
    def pause(source, target, **options):
        original(source, target, **options)
        if target.endswith(suffix):
            pipe.send('link_published'); pipe.recv()
    os.link = pause
    try: store.deliver(intent, DIGEST, PARAMETERS)
    finally: store.close()


@pytest.mark.parametrize('suffix,expected', [('.manifest.json', 'accepted'), ('.export.json', 'fulfilled')])
def test_actual_sigkill_link_before_unlink_recovers_only_exact_owned_staging_inode(delivery_root, suffix, expected):
    context = multiprocessing.get_context('spawn'); parent, child = context.Pipe()
    intent = str(uuid.uuid4()); process = context.Process(target=_pause_after_link, args=(delivery_root, intent, suffix, child))
    process.start(); child.close()
    try:
        assert parent.poll(10) and parent.recv() == 'link_published'
        process.kill(); process.join(10); assert process.exitcode == -signal.SIGKILL
        published = delivery_root / (intent + suffix)
        assert published.stat().st_nlink == 2
        store = JsonExportStore(delivery_root)
        try:
            assert store.query(intent, DIGEST)['state'] == expected
            assert published.stat().st_nlink == 2 and list(delivery_root.glob('.pending-*'))
            # Cleanup is a write-side replay operation, never a GET effect.
            store.deliver(intent, DIGEST, PARAMETERS)
            assert published.stat().st_nlink == 1 and not list(delivery_root.glob('.pending-*'))
        finally: store.close()
    finally:
        if process.is_alive(): process.kill(); process.join(10)
        parent.close()


def test_arbitrary_hardlink_never_allowed_or_deleted(delivery_root):
    intent = str(uuid.uuid4()); store = JsonExportStore(delivery_root)
    try:
        store.deliver(intent, DIGEST, PARAMETERS)
        product = delivery_root / (intent + '.export.json'); unknown = delivery_root / 'unknown-hardlink'
        os.link(product, unknown)
        with pytest.raises(DeliveryUnavailable): store.query(intent, DIGEST)
        assert unknown.exists() and product.stat().st_nlink == 2
    finally: store.close()

PROFILE = 'a' * 64


def _kill_after_recovery_manifest(root, intent):
    store = JsonExportStore(root)
    original = store._immutable
    def publish(name, body):
        original(name, body)
        if name.endswith('.manifest.json'):
            os.kill(os.getpid(), signal.SIGKILL)
    store._immutable = publish
    store.deliver(intent, DIGEST, PARAMETERS, PROFILE)


def test_actual_kill_recovery_manifest_has_exact_parameters_profile_and_get_is_read_only(delivery_root):
    intent = str(uuid.uuid4())
    process = multiprocessing.get_context('fork').Process(target=_kill_after_recovery_manifest, args=(delivery_root, intent))
    process.start(); process.join(5)
    assert process.exitcode == -signal.SIGKILL
    store = JsonExportStore(delivery_root)
    try:
        manifest = store.recovery_candidates(PROFILE)
        assert len(manifest) == 1 and manifest[0]['parameters'] == PARAMETERS
        assert manifest[0]['provider_profile_digest'] == PROFILE
        assert store.query(intent, DIGEST)['state'] == 'accepted'
        assert not (delivery_root / (intent + '.export.json')).exists()
        # File publication below is a storage test, not an invented EIOS permit.
        store.deliver(intent, DIGEST, manifest[0]['parameters'], PROFILE)
        assert store.query(intent, DIGEST)['state'] == 'fulfilled'
        assert store.recovery_candidates(PROFILE) == []
    finally: store.close()


def test_recovery_scan_does_not_guess_legacy_manifest_or_another_profile(delivery_root):
    store = JsonExportStore(delivery_root)
    try:
        legacy = str(uuid.uuid4()); other = str(uuid.uuid4())
        store.deliver(legacy, DIGEST, PARAMETERS)
        store.deliver(other, DIGEST, PARAMETERS, 'b' * 64)
        for intent in (legacy, other): (delivery_root / (intent + '.export.json')).unlink()
        assert store.recovery_candidates(PROFILE) == []
        assert store.query(legacy, DIGEST)['state'] == 'accepted'
    finally: store.close()


def test_recovery_scan_rejects_tampered_parameters_without_product_rebuild(delivery_root):
    store = JsonExportStore(delivery_root); intent = str(uuid.uuid4())
    try:
        store.deliver(intent, DIGEST, PARAMETERS, PROFILE)
        (delivery_root / (intent + '.export.json')).unlink()
        path = delivery_root / (intent + '.manifest.json')
        import json
        value = json.loads(path.read_bytes()); value['parameters'] = {'message': 'tampered'}
        path.write_bytes(canonical_payload(value).encode())
        assert store.recovery_candidates(PROFILE) == []
        assert not (delivery_root / (intent + '.export.json')).exists()
    finally: store.close()


def test_recovery_scan_is_bounded_and_cursor_visits_later_original_intents(delivery_root):
    store = JsonExportStore(delivery_root)
    try:
        intents = [str(uuid.uuid4()) for unused in range(21)]
        for intent in intents:
            store.deliver(intent, DIGEST, PARAMETERS, PROFILE)
            (delivery_root / (intent + '.export.json')).unlink()
        seen = set()
        for unused in range(6):
            page = store.recovery_candidates(PROFILE)
            assert len(page) <= 4
            seen.update(row['intent_id'] for row in page)
        assert seen == set(intents)
        for invalid in (0, 5, True, 1.5):
            with pytest.raises(DeliveryUnavailable): store.recovery_candidates(PROFILE, limit=invalid)
    finally: store.close()


def _snapshot(root):
    # Access time may be changed by reads by the operating system; all durable
    # content, namespace, permissions/inode/link count and mtime must stay exact.
    result = {'directory': (root.stat().st_ino, root.stat().st_mtime_ns)}
    for path in root.iterdir():
        info = path.lstat()
        result[path.name] = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                             info.st_size, info.st_mtime_ns, path.read_bytes())
    return result


def test_query_not_found_creates_no_lock_or_other_files(delivery_root):
    store = JsonExportStore(delivery_root)
    try:
        before = _snapshot(delivery_root)
        assert store.query(str(uuid.uuid4()), DIGEST)['state'] == 'not_found'
        assert _snapshot(delivery_root) == before
    finally: store.close()


@pytest.mark.parametrize('suffix,expected', [('.manifest.json', 'accepted'), ('.export.json', 'fulfilled')])
def test_query_real_sigkill_staging_hardlink_changes_no_durable_files(delivery_root, suffix, expected):
    context = multiprocessing.get_context('spawn'); parent, child = context.Pipe()
    intent = str(uuid.uuid4()); process = context.Process(target=_pause_after_link, args=(delivery_root, intent, suffix, child))
    process.start(); child.close()
    try:
        assert parent.poll(10) and parent.recv() == 'link_published'
        process.kill(); process.join(10); assert process.exitcode == -signal.SIGKILL
        store = JsonExportStore(delivery_root)
        try:
            before = _snapshot(delivery_root)
            assert store.query(intent, DIGEST)['state'] == expected
            assert _snapshot(delivery_root) == before
            assert (delivery_root / (intent + suffix)).stat().st_nlink == 2
        finally: store.close()
    finally:
        if process.is_alive(): process.kill(); process.join(10)
        parent.close()


def test_query_orphan_without_lock_is_denied_without_creating_lock(delivery_root):
    intent = str(uuid.uuid4())
    path = delivery_root / (intent + '.export.json'); path.write_bytes(b'orphan'); path.chmod(0o600)
    store = JsonExportStore(delivery_root)
    try:
        before = _snapshot(delivery_root)
        with pytest.raises(DeliveryUnavailable): store.query(intent, DIGEST)
        assert _snapshot(delivery_root) == before
    finally: store.close()
