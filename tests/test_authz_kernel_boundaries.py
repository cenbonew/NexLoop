"""Equality boundaries of the vendored eios.authz intersection that the frozen upstream suite leaves unpinned.

A boundary mutation run over tests/upstream (EIOS capability gap C3) kept every upstream test green when the
clearance-rank check went from ``>=`` to ``>`` or the application/clearance expiry checks from ``<=`` to ``<``.
Rank equality is decided only here. For the expiry instants the fact resolver already refuses an application or
grant at its expiry, so these checks are defence in depth; they are pinned so they cannot drift silently.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from eios.authz import intersection
from eios.authz.applications import ApplicationMode
from eios.authz.facts import ControlDominance, ControlKind

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def clearance(rank, valid_until=None):
    return SimpleNamespace(kind=ControlKind.CLASSIFICATION, namespace='sensitivity', value='internal', rank=rank,
                           dominates=frozenset(), valid_until=valid_until)


def rank_requirement(minimum_rank):
    return SimpleNamespace(kind=ControlKind.CLASSIFICATION, namespace='sensitivity', value='internal',
                           dominance=ControlDominance.RANK, minimum_rank=minimum_rank)


def test_clearance_rank_equal_to_the_minimum_satisfies_and_one_below_does_not():
    assert intersection._control_satisfied(clearance(3), rank_requirement(3), NOW)
    assert intersection._control_satisfied(clearance(4), rank_requirement(3), NOW)
    assert not intersection._control_satisfied(clearance(2), rank_requirement(3), NOW)


def test_clearance_ending_at_now_no_longer_satisfies():
    assert intersection._control_satisfied(clearance(3, NOW + timedelta(seconds=1)), rank_requirement(3), NOW)
    assert not intersection._control_satisfied(clearance(3, NOW), rank_requirement(3), NOW)


def application(expires_at):
    resource = SimpleNamespace(tenant_id='tenant-a', resource_type=SimpleNamespace(value='object'), resource_id='o-1')
    app = SimpleNamespace(eligible=True, application_status='active', version_status='published', expires_at=expires_at,
                          mode=ApplicationMode.RESTRICTED, break_glass=False,
                          allowed_resource_keys=frozenset({('tenant-a', 'object', 'o-1')}),
                          allowed_operations=frozenset({'read'}))
    return app, resource


def test_application_expiring_at_now_is_not_allowed():
    app, resource = application(NOW + timedelta(seconds=1))
    assert intersection._application_allows(app, resource, 'read', NOW)
    app, resource = application(NOW)
    assert not intersection._application_allows(app, resource, 'read', NOW)
