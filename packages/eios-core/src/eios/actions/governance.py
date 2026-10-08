from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import RLock
from types import MappingProxyType

from eios.actions.models import (
    ActionClaim,
    ActionClaimDisposition,
    ActionClaimRequest,
    ActionClaimResult,
    ActionClaimState,
    ActionExecutionPermit,
    ActionReservationKey,
    ApprovalEvidence,
    ApprovalEvidenceBinding,
    ClaimBindingPayload,
    PolicyEvidenceBinding,
    PolicyEvidenceSet,
    TerminalOutcomeReference,
    canonical_request_digest,
)
from eios.actions.ports import ActionClaimError, ActionClaimPort
from eios.actions.preconditions import (
    evaluate_precondition,
    validate_precondition_expression,
)
from eios.approvals.models import (
    ApprovalRecord,
    ApprovalStatus,
    approval_record_snapshot_digest,
    is_policy_auto_approved,
)
from eios.approvals.ports import (
    ApprovalHumanAuthorityVerifier,
    ApprovalPort,
    InMemoryApprovalHumanAuthorityVerifier,
)
from eios.ontology.definitions import (
    ActionApprovalMode,
    ActionDefinition,
    ActionRiskLevel,
    CapabilityBinding,
    DefinitionReference,
    DefinitionStatus,
    DefinitionType,
)
from eios.ontology.version_resolution import (
    CapabilityBindingError,
    CapabilityContractSnapshot,
    validate_capability_binding,
)


_ACTION_CLAIM_ERROR_CODE_MAP: Mapping[str, str] = MappingProxyType(
    {
        "action_claim_binding_conflict": "action_claim_binding_conflict",
        "action_claim_clock_invalid": "action_claim_clock_invalid",
        "action_claim_clock_regressed": "action_claim_clock_regressed",
        "action_claim_command_invalid": "action_claim_command_invalid",
        "action_claim_lease_expired": "action_claim_lease_expired",
        "action_claim_not_found": "action_claim_not_found",
        "action_claim_outcome_conflict": "action_claim_outcome_conflict",
        "action_claim_revision_conflict": "action_claim_revision_conflict",
        "action_claim_stale_fence": "action_claim_stale_fence",
        "action_claim_time_invalid": "action_claim_time_invalid",
        "action_claim_transition_invalid": "action_claim_transition_invalid",
    }
)
_GENERIC_CLAIM_ERROR_CODE = "action_governance_claim_result_invalid"


class ActionGovernanceError(ValueError):
    """Stable fail-closed Action governance error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_exact_utc_datetime(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is UTC


def _target_system_declared(
    target_system: object, declared: tuple[str, ...]
) -> bool:
    """Whether a claim's recorded target_system is authorized by the scope.

    A single-target Action records one of its declared systems. A multi-target
    Action is served by the coordinator and records the canonical joined key of
    the sorted declared set (deterministic, so it is authorized iff it matches
    that exact set) — the write still fans out only within the declared scope.
    """

    if target_system in declared:
        return True
    return bool(declared) and target_system == "_".join(sorted(declared))


def _reservation_key_snapshot(key: ActionReservationKey) -> tuple[str, str, str]:
    return (key.tenant_id, key.action_stable_name, key.idempotency_key)


def _claim_binding_snapshot(binding: ClaimBindingPayload) -> tuple[object, ...]:
    reference = binding.action_reference
    capability = binding.capability_binding
    return (
        binding.invocation_id,
        (
            reference.tenant_id,
            reference.definition_type.value,
            reference.stable_name,
            reference.version,
            reference.contract_digest,
        ),
        binding.request_digest,
        (
            capability.capability_name,
            capability.capability_version,
            capability.schema_hash,
        ),
        binding.adapter_id,
        binding.target_system,
    )


def required_action_scopes(
    action_definition: ActionDefinition,
    capability_snapshot: CapabilityContractSnapshot,
) -> frozenset[str]:
    """Return the frozen A1 required-scope set."""

    return frozenset(capability_snapshot.required_scopes).union(
        action_definition.required_scopes
    )


class ActionGovernor:
    """Compose immutable governance evidence before one atomic claim."""

    def __init__(
        self,
        *,
        claim_port: ActionClaimPort,
        approval_port: ApprovalPort,
        clock: Callable[[], datetime],
        object_reader: (
            Callable[[str, str, str], Mapping[str, object] | None] | None
        ) = None,
        authority_verifier: ApprovalHumanAuthorityVerifier | None = None,
    ) -> None:
        # object_reader(tenant_id, type_name, object_id) returns
        # {"properties": {...}, "updated_at": <ISO-8601 str>} or None. It
        # powers the stage-5 submission gates; without it, precondition
        # evaluation sees empty properties and the edit fence fails closed.
        self._claim_port = claim_port
        self._approval_port = approval_port
        self._clock = clock
        self._object_reader = object_reader
        self._authority_verifier = (
            authority_verifier or InMemoryApprovalHumanAuthorityVerifier()
        )
        self._clock_lock = RLock()
        self._last_trusted_at: datetime | None = None

    def govern(
        self,
        *,
        tenant_id: str,
        invocation_id: str,
        action_reference: DefinitionReference,
        action_definition: ActionDefinition,
        capability_snapshot: CapabilityContractSnapshot,
        request: Mapping[str, object],
        claim_request: ActionClaimRequest,
        granted_scopes: frozenset[str],
        policy_evidence: PolicyEvidenceSet,
        approval_evidence: ApprovalEvidence | None,
    ) -> ActionExecutionPermit | TerminalOutcomeReference:
        action, reference, command = self._validate_identity(
            tenant_id=tenant_id,
            invocation_id=invocation_id,
            action_reference=action_reference,
            action_definition=action_definition,
            request=request,
            claim_request=claim_request,
        )
        expected_key = _reservation_key_snapshot(command.key)
        expected_binding = _claim_binding_snapshot(command.binding)
        expected_requested_at = command.requested_at
        expected_lease_expires_at = command.lease_expires_at
        port_command, permit_command = self._detach_claim_commands(command)
        if (
            _reservation_key_snapshot(port_command.key) != expected_key
            or _claim_binding_snapshot(port_command.binding) != expected_binding
            or port_command.requested_at != expected_requested_at
            or port_command.lease_expires_at != expected_lease_expires_at
            or _reservation_key_snapshot(permit_command.key) != expected_key
            or _claim_binding_snapshot(permit_command.binding) != expected_binding
            or permit_command.requested_at != expected_requested_at
            or permit_command.lease_expires_at != expected_lease_expires_at
        ):
            self._fail_claim_result()
        # Stage-5 submission gates run BEFORE any evidence, claim, or
        # reservation: a request that fails business criteria or has stale
        # object expectations never burns an idempotency binding.
        self._enforce_submission_criteria(
            tenant_id=tenant_id, action=action, request=request
        )
        self._enforce_edit_fence(tenant_id=tenant_id, request=request)
        snapshot = self._validate_capability(action, capability_snapshot)
        self._validate_granted_scopes(
            action=action,
            snapshot=snapshot,
            granted_scopes=granted_scopes,
        )
        evaluated_at = self._trusted_now()
        policy_bindings = self._validate_policy_evidence(
            tenant_id=tenant_id,
            invocation_id=invocation_id,
            action_reference=reference,
            action=action,
            evidence_set=policy_evidence,
            evaluated_at=evaluated_at,
        )
        approval_binding = self._validate_approval_evidence(
            tenant_id=tenant_id,
            invocation_id=invocation_id,
            action_reference=reference,
            action=action,
            evidence=approval_evidence,
            evaluated_at=evaluated_at,
            request_digest=port_command.binding.request_digest,
        )
        claim_result = self._reserve(port_command)
        if claim_result.disposition is ActionClaimDisposition.REPLAY:
            outcome = claim_result.terminal_outcome
            if outcome is None or not _is_exact_utc_datetime(outcome.finalized_at):
                self._fail_claim_result()
            return outcome
        if claim_result.disposition is ActionClaimDisposition.IN_PROGRESS:
            raise ActionGovernanceError(
                "action_governance_in_progress",
                "Action execution is already in progress",
            )
        if claim_result.disposition is ActionClaimDisposition.CONFLICT:
            raise ActionGovernanceError(
                "action_governance_conflict",
                "Action reservation conflicts with immutable binding",
            )
        claim = claim_result.claim
        invalid_claim = (
            claim is None
            or type(claim) is not ActionClaim
            or type(claim.key) is not ActionReservationKey
            or type(claim.binding) is not ClaimBindingPayload
            or type(claim.binding.action_reference) is not DefinitionReference
            or type(claim.binding.capability_binding) is not CapabilityBinding
        )
        actual_key: tuple[str, str, str] | None = None
        actual_binding: tuple[object, ...] | None = None
        claim_revision: int | None = None
        fencing_token: str | None = None
        if not invalid_claim and claim is not None:
            try:
                actual_key = _reservation_key_snapshot(claim.key)
                actual_binding = _claim_binding_snapshot(claim.binding)
                claim_revision = claim.claim_revision
                fencing_token = claim.fencing_token
            except Exception:
                invalid_claim = True
        if (
            invalid_claim
            or claim is None
            or actual_key != expected_key
            or actual_binding != expected_binding
            or not _is_exact_utc_datetime(claim.lease_expires_at)
            or claim.lease_expires_at != expected_lease_expires_at
            or expected_requested_at > evaluated_at
            or claim.lease_expires_at <= evaluated_at
        ):
            self._fail_claim_result()
        permit_failed = False
        try:
            authorized_claim = ActionClaim(
                key=permit_command.key,
                binding=permit_command.binding,
                state=ActionClaimState.ACTIVE,
                claim_revision=claim_revision,
                fencing_token=fencing_token,
                lease_expires_at=expected_lease_expires_at,
            )
            permit = ActionExecutionPermit(
                claim=authorized_claim,
                policy_bindings=policy_bindings,
                approval_binding=approval_binding,
                issued_at=evaluated_at,
                expires_at=expected_lease_expires_at,
            )
        except Exception:
            permit_failed = True
            permit = None
        if permit_failed or permit is None:
            self._fail_claim_result()
        return permit

    @staticmethod
    def _validate_identity(
        *,
        tenant_id: str,
        invocation_id: str,
        action_reference: DefinitionReference,
        action_definition: ActionDefinition,
        request: Mapping[str, object],
        claim_request: ActionClaimRequest,
    ) -> tuple[ActionDefinition, DefinitionReference, ActionClaimRequest]:
        invalid_contract = (
            type(action_definition) is not ActionDefinition
            or type(action_reference) is not DefinitionReference
            or type(claim_request) is not ActionClaimRequest
        )
        action: ActionDefinition | None = None
        reference: DefinitionReference | None = None
        command: ActionClaimRequest | None = None
        if not invalid_contract:
            try:
                action = ActionDefinition.model_validate(action_definition)
                reference = DefinitionReference.model_validate(action_reference)
                command = ActionClaimRequest.model_validate(claim_request)
            except Exception:
                invalid_contract = True
        if (
            invalid_contract
            or action is None
            or reference is None
            or command is None
            or type(tenant_id) is not str
            or not tenant_id.strip()
            or type(invocation_id) is not str
            or not invocation_id.strip()
            or action.status is not DefinitionStatus.PUBLISHED
            or reference.definition_type is not DefinitionType.ACTION
            or not _is_exact_utc_datetime(action.created_at)
            or not _is_exact_utc_datetime(command.requested_at)
            or not _is_exact_utc_datetime(command.lease_expires_at)
        ):
            raise ActionGovernanceError(
                "action_governance_identity_mismatch",
                "Action governance identity is invalid",
            )

        exact_reference = action.reference()
        binding = command.binding
        if (
            tenant_id != action.tenant_id
            or tenant_id != reference.tenant_id
            or invocation_id != binding.invocation_id
            or reference != exact_reference
            or binding.action_reference != reference
            or command.key.tenant_id != tenant_id
            or command.key.action_stable_name != reference.stable_name
            or not _target_system_declared(
                binding.target_system,
                action.governance.change_scope.target_systems,
            )
        ):
            raise ActionGovernanceError(
                "action_governance_identity_mismatch",
                "Action governance identity is invalid",
            )

        digest_failed = False
        try:
            request_digest = canonical_request_digest(request)
        except Exception:
            digest_failed = True
            request_digest = None
        if digest_failed or request_digest != binding.request_digest:
            raise ActionGovernanceError(
                "action_governance_request_digest_mismatch",
                "Action request digest does not match",
            )

        expected_capability = action.capability_binding
        actual_capability = binding.capability_binding
        comparisons = (
            (
                expected_capability.capability_name,
                actual_capability.capability_name,
                "capability_name_mismatch",
            ),
            (
                expected_capability.capability_version,
                actual_capability.capability_version,
                "capability_version_mismatch",
            ),
            (
                expected_capability.schema_hash,
                actual_capability.schema_hash,
                "capability_schema_hash_mismatch",
            ),
        )
        for expected, actual, code in comparisons:
            if expected != actual:
                raise ActionGovernanceError(
                    code,
                    "Action Capability binding is invalid",
                )

        return action, reference, command

    @staticmethod
    def _validate_capability(
        action: ActionDefinition,
        snapshot: CapabilityContractSnapshot,
    ) -> CapabilityContractSnapshot:
        failure_code: str | None = None
        validated: CapabilityContractSnapshot | None = None
        try:
            validated = validate_capability_binding(action, snapshot)
        except CapabilityBindingError as error:
            failure_code = error.code
        except Exception:
            failure_code = "capability_snapshot_invalid"
        if failure_code is not None or validated is None:
            raise ActionGovernanceError(
                failure_code or "capability_snapshot_invalid",
                "Action Capability contract is invalid",
            )
        return validated

    @staticmethod
    def _validate_granted_scopes(
        *,
        action: ActionDefinition,
        snapshot: CapabilityContractSnapshot,
        granted_scopes: frozenset[str],
    ) -> None:
        if type(granted_scopes) is not frozenset or any(
            type(scope) is not str or not scope.strip() for scope in granted_scopes
        ):
            raise ActionGovernanceError(
                "action_governance_scope_invalid",
                "Granted Action scopes are invalid",
            )
        missing = required_action_scopes(action, snapshot).difference(granted_scopes)
        if missing:
            raise ActionGovernanceError(
                "action_governance_scope_missing",
                "Required Action scope is missing",
            )

    def _trusted_now(self) -> datetime:
        with self._clock_lock:
            failed = False
            regressed = False
            trusted: datetime | None = None
            try:
                value = self._clock()
                if (
                    not isinstance(value, datetime)
                    or value.tzinfo is None
                    or value.utcoffset() is None
                ):
                    failed = True
                else:
                    normalized = value.astimezone(UTC)
                    if not _is_exact_utc_datetime(normalized):
                        failed = True
                    else:
                        trusted = normalized
                        regressed = (
                            self._last_trusted_at is not None
                            and trusted < self._last_trusted_at
                        )
            except Exception:
                failed = True
            if failed or trusted is None:
                raise ActionGovernanceError(
                    "action_governance_clock_invalid",
                    "Action governance trusted clock is invalid",
                )
            if regressed:
                raise ActionGovernanceError(
                    "action_governance_clock_regressed",
                    "Action governance trusted clock regressed",
                )
            self._last_trusted_at = trusted
            return trusted

    @staticmethod
    def _validate_policy_evidence(
        *,
        tenant_id: str,
        invocation_id: str,
        action_reference: DefinitionReference,
        action: ActionDefinition,
        evidence_set: PolicyEvidenceSet,
        evaluated_at: datetime,
    ) -> tuple[PolicyEvidenceBinding, ...]:
        invalid = type(evidence_set) is not PolicyEvidenceSet
        validated: PolicyEvidenceSet | None = None
        if not invalid:
            try:
                validated = PolicyEvidenceSet.model_validate(evidence_set)
            except Exception:
                invalid = True
        expected = action.governance.policy_refs
        if validated is not None:
            actual = tuple(item.policy_reference for item in validated.evidence)
            valid_times = _is_exact_utc_datetime(validated.evaluated_at) and all(
                _is_exact_utc_datetime(item.issued_at)
                and _is_exact_utc_datetime(item.expires_at)
                for item in validated.evidence
            )
            invalid = invalid or (
                not valid_times
                or validated.tenant_id != tenant_id
                or validated.invocation_id != invocation_id
                or validated.action_reference != action_reference
                or validated.required_policy_references != expected
                or actual != expected
                or any(
                    item.issued_at > evaluated_at or item.expires_at <= evaluated_at
                    for item in validated.evidence
                )
            )
        if invalid or validated is None:
            raise ActionGovernanceError(
                "action_governance_policy_invalid",
                "Action policy evidence is invalid",
            )
        bindings_failed = False
        try:
            bindings = validated.bindings()
        except Exception:
            bindings_failed = True
            bindings = None
        if (
            bindings_failed
            or type(bindings) is not tuple
            or any(type(item) is not PolicyEvidenceBinding for item in bindings)
        ):
            raise ActionGovernanceError(
                "action_governance_policy_invalid",
                "Action policy evidence is invalid",
            )
        return bindings

    # -- stage-5 submission gates ------------------------------------------

    @staticmethod
    def _request_targets(request: Mapping[str, object]) -> tuple | None:
        """Parse request.target_objects; None means the shape is invalid."""

        raw = request.get("target_objects", ())
        if raw in ((), []):
            return ()
        if not isinstance(raw, (list, tuple)):
            return None
        targets: list[tuple[str, str]] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                return None
            type_name = entry.get("type")
            object_id = entry.get("object_id")
            if (
                not isinstance(type_name, str)
                or not type_name.strip()
                or not isinstance(object_id, str)
                or not object_id.strip()
            ):
                return None
            targets.append((type_name, object_id))
        return tuple(targets)

    def _read_object(
        self, tenant_id: str, type_name: str, object_id: str
    ) -> Mapping[str, object] | None:
        if self._object_reader is None:
            return None
        try:
            snapshot = self._object_reader(tenant_id, type_name, object_id)
        except Exception:
            return None
        if not isinstance(snapshot, Mapping):
            return None
        return snapshot

    def _enforce_submission_criteria(
        self,
        *,
        tenant_id: str,
        action: ActionDefinition,
        request: Mapping[str, object],
    ) -> None:
        if not action.preconditions:
            return
        targets = self._request_targets(request)
        if targets is None:
            raise ActionGovernanceError(
                "action_submission_criteria_failed",
                "Action target objects are invalid",
            )
        parameters = {
            parameter.name: request[parameter.name]
            for parameter in action.parameters
            if parameter.name in request
        }
        property_sets: list[Mapping[str, object]] = []
        for type_name, object_id in targets:
            snapshot = self._read_object(tenant_id, type_name, object_id)
            properties = snapshot.get("properties") if snapshot is not None else None
            property_sets.append(properties if isinstance(properties, Mapping) else {})
        if not property_sets:
            property_sets.append({})
        for precondition in action.preconditions:
            try:
                validate_precondition_expression(precondition.expression)
            except Exception:
                raise ActionGovernanceError(
                    "action_submission_criteria_failed",
                    "Action precondition expression is not evaluable",
                ) from None
            for properties in property_sets:
                if not evaluate_precondition(
                    precondition.expression,
                    object_properties=properties,
                    request_input=request,
                    parameters=parameters,
                ):
                    raise ActionGovernanceError(
                        "action_submission_criteria_failed",
                        "Action submission criteria are not satisfied",
                    )

    def _enforce_edit_fence(
        self, *, tenant_id: str, request: Mapping[str, object]
    ) -> None:
        raw = request.get("expected_object_revisions", ())
        if raw in ((), []):
            return
        conflict = ActionGovernanceError(
            "action_edit_conflict",
            "Action target objects have drifted from the expected state",
        )
        if not isinstance(raw, (list, tuple)):
            raise conflict
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise conflict
            type_name = entry.get("type")
            object_id = entry.get("object_id")
            expected = entry.get("updated_at")
            if (
                not isinstance(type_name, str)
                or not isinstance(object_id, str)
                or not isinstance(expected, str)
                or not expected.strip()
            ):
                raise conflict
            snapshot = self._read_object(tenant_id, type_name, object_id)
            if snapshot is None:
                raise conflict
            actual = snapshot.get("updated_at")
            if not isinstance(actual, str) or actual != expected:
                raise conflict

    def _validate_approval_evidence(
        self,
        *,
        tenant_id: str,
        invocation_id: str,
        action_reference: DefinitionReference,
        action: ActionDefinition,
        evidence: ApprovalEvidence | None,
        evaluated_at: datetime,
        request_digest: str,
    ) -> ApprovalEvidenceBinding | None:
        required = (
            action.governance.risk_level is not ActionRiskLevel.LOW
            or action.governance.approval_mode is ActionApprovalMode.REQUIRED
        )
        if evidence is None:
            if required:
                raise ActionGovernanceError(
                    "action_governance_approval_required",
                    "Action Approval evidence is required",
                )
            return None

        invalid = type(evidence) is not ApprovalEvidence
        validated: ApprovalEvidence | None = None
        binding: ApprovalEvidenceBinding | None = None
        if not invalid:
            try:
                validated = ApprovalEvidence.model_validate(evidence)
                binding = validated.binding(evaluated_at=evaluated_at)
            except Exception:
                invalid = True
        if validated is not None:
            invalid = invalid or (
                not _is_exact_utc_datetime(validated.decided_at)
                or not _is_exact_utc_datetime(validated.issued_at)
                or not _is_exact_utc_datetime(validated.expires_at)
                or validated.tenant_id != tenant_id
                or validated.invocation_id != invocation_id
                or validated.action_reference != action_reference
            )
        if invalid or validated is None or type(binding) is not ApprovalEvidenceBinding:
            self._fail_approval()

        lookup_failed = False
        record: ApprovalRecord | None = None
        try:
            candidate = self._approval_port.get(
                tenant_id=validated.tenant_id,
                approval_id=validated.approval_id,
            )
        except Exception:
            lookup_failed = True
            candidate = None
        if candidate is not None:
            if type(candidate) is not ApprovalRecord:
                lookup_failed = True
            else:
                try:
                    record = ApprovalRecord.model_validate(candidate)
                except Exception:
                    lookup_failed = True
        if lookup_failed or record is None:
            self._fail_approval()
        try:
            record_digest = approval_record_snapshot_digest(record)
        except Exception:
            self._fail_approval()
        decision = record.decision
        if (
            not _is_exact_utc_datetime(record.requested_at)
            or record.status is not ApprovalStatus.APPROVED
            or record.tenant_id != validated.tenant_id
            or record.approval_id != validated.approval_id
            or record.revision != validated.approved_revision
            or record_digest != validated.approval_record_digest
            or decision is None
            or decision.outcome is not ApprovalStatus.APPROVED
            or not _is_exact_utc_datetime(decision.decided_at)
            or decision.decision_id != validated.decision_id
            or decision.decided_at != validated.decided_at
        ):
            self._fail_approval()
        # Subject hard binding (stage 4): the Approval must state exactly WHAT
        # it endorses — this Action reference and this request digest. A
        # subject-less record (pre-envelope, or created outside the governed
        # path) can never serve as approval evidence.
        subject = record.subject
        if (
            subject is None
            or subject.action_reference != action_reference
            or subject.request_digest != request_digest
        ):
            self._fail_approval()
        # Stage-5 quorum re-verification: an APPROVED record must actually
        # carry the endorsements its own policy demands (defense in depth
        # against records terminalized outside the governed decide path).
        approvers = {
            item.actor
            for item in record.decisions
            if item.outcome is ApprovalStatus.APPROVED
        }
        if not approvers and record.decision is not None:
            approvers = {record.decision.actor}
        if len(approvers) < subject.quorum:
            self._fail_approval()
        if subject.require_distinct_requester and subject.requester_id in approvers:
            self._fail_approval()
        try:
            self._authority_verifier.assert_fresh(record)
            eligible_approver_scopes = frozenset(
                subject.eligible_approver_scopes or ("approvals.decide",)
            )
            approved_decisions = tuple(
                item
                for item in record.decisions
                if item.outcome is ApprovalStatus.APPROVED
            )
            if not approved_decisions and record.decision is not None:
                approved_decisions = (record.decision,)
            # Registered policy approvals carry no human authority by design;
            # assert_fresh above proved them against the database-side policy
            # approver registry instead of a live human session.
            if any(
                not is_policy_auto_approved(item)
                and (
                    item.human_authority is None
                    or item.human_authority.tenant_id != tenant_id
                    or item.human_authority.actor not in approvers
                    or not eligible_approver_scopes.intersection(
                        item.human_authority.eligible_scopes
                    )
                )
                for item in approved_decisions
            ):
                self._fail_approval()
        except Exception:
            self._fail_approval()
        return binding

    @staticmethod
    def _detach_claim_commands(
        command: ActionClaimRequest,
    ) -> tuple[ActionClaimRequest, ActionClaimRequest]:
        failed = False
        port_command: ActionClaimRequest | None = None
        permit_command: ActionClaimRequest | None = None
        try:
            serialized = command.model_dump_json()
            port_command = ActionClaimRequest.model_validate_json(serialized)
            permit_command = ActionClaimRequest.model_validate_json(serialized)
        except Exception:
            failed = True
        commands = (port_command, permit_command)
        if failed or any(
            type(item) is not ActionClaimRequest
            or type(item.key) is not ActionReservationKey
            or type(item.binding) is not ClaimBindingPayload
            or type(item.binding.action_reference) is not DefinitionReference
            or type(item.binding.capability_binding) is not CapabilityBinding
            or not _is_exact_utc_datetime(item.requested_at)
            or not _is_exact_utc_datetime(item.lease_expires_at)
            for item in commands
            if item is not None
        ):
            ActionGovernor._fail_claim_result()
        if port_command is None or permit_command is None:  # pragma: no cover
            ActionGovernor._fail_claim_result()
        if (
            port_command is permit_command
            or port_command.key is permit_command.key
            or port_command.binding is permit_command.binding
            or port_command.binding.action_reference
            is permit_command.binding.action_reference
            or port_command.binding.capability_binding
            is permit_command.binding.capability_binding
        ):
            ActionGovernor._fail_claim_result()
        return port_command, permit_command

    def _reserve(self, command: ActionClaimRequest) -> ActionClaimResult:
        native_failure = False
        claim_failure_code: str | None = None
        try:
            candidate = self._claim_port.reserve(command)
        except ActionClaimError as error:
            try:
                claim_failure_code = _ACTION_CLAIM_ERROR_CODE_MAP.get(
                    error.code,
                    _GENERIC_CLAIM_ERROR_CODE,
                )
            except Exception:
                claim_failure_code = _GENERIC_CLAIM_ERROR_CODE
            candidate = None
        except Exception:
            native_failure = True
            candidate = None
        if claim_failure_code is not None:
            raise ActionGovernanceError(
                claim_failure_code,
                "Action claim operation failed",
            )
        invalid = native_failure or type(candidate) is not ActionClaimResult
        result: ActionClaimResult | None = None
        if not invalid:
            try:
                result = ActionClaimResult.model_validate(candidate)
            except Exception:
                invalid = True
        if invalid or result is None:
            self._fail_claim_result()
        return result

    @staticmethod
    def _fail_approval() -> None:
        raise ActionGovernanceError(
            "action_governance_approval_invalid",
            "Action Approval evidence is invalid",
        )

    @staticmethod
    def _fail_claim_result() -> None:
        raise ActionGovernanceError(
            "action_governance_claim_result_invalid",
            "Action claim result is invalid",
        )
