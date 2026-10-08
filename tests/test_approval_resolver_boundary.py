from datetime import UTC, datetime

import pytest

from eios.composition.action_approval import ApprovalEvidenceResolver
from nexloop_eios.action_governor import UnavailableApprovalPort


def test_approval_resolver_has_no_implicit_authority_verifier():
    with pytest.raises(TypeError, match='authority_verifier'):
        ApprovalEvidenceResolver(UnavailableApprovalPort(), clock=lambda:datetime.now(UTC))


@pytest.mark.parametrize('verifier',[None,object()])
def test_approval_resolver_rejects_unusable_explicit_verifier(verifier):
    with pytest.raises(TypeError, match='trusted Approval authority verifier is required'):
        ApprovalEvidenceResolver(UnavailableApprovalPort(), clock=lambda:datetime.now(UTC),
            authority_verifier=verifier)
