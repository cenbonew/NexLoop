from datetime import UTC, datetime, timedelta

import pytest

from eios.authz.audit import AuthorizationAuditRecord, digest_identifier


def _record(**changes: object) -> AuthorizationAuditRecord:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "decision_id": "d-1", "request_id": "r-1", "trace_id": "t-1",
        "tenant_id": "tenant-1", "subject_digest": digest_identifier("subject-1"),
        "credential_digest": digest_identifier("credential-secret"),
        "application_digest": "a" * 64, "agent_chain_digest": "b" * 64,
        "target_digest": "c" * 64, "dependency_digest": "d" * 64,
        "operation": "read", "requested_scopes_digest": "e" * 64,
        "server_scopes_digest": "f" * 64,
        "intersection_results": {"subject": True, "application": True},
        "policy_evidence": (), "revision_vector_digest": "1" * 64,
        "outcome": "allow", "cache_status": "miss", "authoritative_now": now,
        "authoritative_expires_at": now + timedelta(seconds=5),
    }
    fields.update(changes)
    return AuthorizationAuditRecord(**fields)


def test_audit_record_is_strict_frozen_and_contains_no_raw_identity() -> None:
    record = _record()
    assert record.subject_digest != "subject-1"
    assert record.credential_digest != "credential-secret"
    with pytest.raises(Exception):
        AuthorizationAuditRecord(**{**record.model_dump(), "secret": "leak"})
    with pytest.raises(Exception):
        record.outcome = "deny"  # type: ignore[misc]


def test_audit_record_rejects_noncanonical_digest_and_time() -> None:
    with pytest.raises(Exception):
        _record(target_digest="A" * 64)
    with pytest.raises(Exception):
        _record(authoritative_now=datetime.now())
