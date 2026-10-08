from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from pydantic import ValidationError

from eios.actions.models import (
    ActionClaim,
    ActionClaimDisposition,
    ActionClaimFinalizeCommand,
    ActionClaimRequest,
    ActionClaimResult,
    ActionClaimRetryableCommand,
    ActionClaimState,
    ActionReservationKey,
)
from eios.actions.ports import (
    ActionClaimBindingConflictError,
    ActionClaimContractError,
    ActionClaimNotFoundError,
    ActionClaimOutcomeConflictError,
    ActionClaimRevisionConflictError,
    ActionClaimStaleFenceError,
    ActionClaimTransitionError,
)
from eios.actions.write_attempts import (
    ExternalWriteAttempt,
    ExternalWriteAttemptContractError,
    ExternalWriteAttemptKey,
    ExternalWriteAttemptNotFound,
    ExternalWriteAttemptStartCommand,
    ExternalWriteAttemptState,
    ExternalWriteAttemptTransitionCommand,
    validate_transition,
)


class InMemoryActionClaimStore:
    """Thread-safe reference ledger for one atomic Action reservation call."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str, str], ActionClaim] = {}
        self._issued_fences: set[str] = set()
        self._clock = clock if clock is not None else self._system_utc_now
        self._last_trusted_at: datetime | None = None

    def reserve(self, command: ActionClaimRequest) -> ActionClaimResult:
        validated = self._validate_reserve(command)
        storage_key = self._storage_key(validated.key)
        with self._lock:
            existing = self._records.get(storage_key)
            if existing is not None:
                if existing.binding != validated.binding:
                    return ActionClaimResult(
                        disposition=ActionClaimDisposition.CONFLICT,
                    )
                if existing.state is ActionClaimState.TERMINAL:
                    return ActionClaimResult(
                        disposition=ActionClaimDisposition.REPLAY,
                        terminal_outcome=existing.terminal_outcome,
                    )

            trusted_now = self._trusted_now()
            self._validate_reserve_time(validated, trusted_now)
            if existing is None:
                claimed = self._new_claim(validated, claim_revision=1)
                self._records[storage_key] = claimed
                return ActionClaimResult(
                    disposition=ActionClaimDisposition.CLAIMED,
                    claim=claimed,
                )

            if (
                existing.state is ActionClaimState.RETRYABLE
                or trusted_now >= existing.lease_expires_at
            ):
                claimed = self._new_claim(
                    validated,
                    claim_revision=existing.claim_revision + 1,
                )
                self._records[storage_key] = claimed
                return ActionClaimResult(
                    disposition=ActionClaimDisposition.CLAIMED,
                    claim=claimed,
                )

            return ActionClaimResult(
                disposition=ActionClaimDisposition.IN_PROGRESS,
            )

    def mark_retryable(self, command: ActionClaimRetryableCommand) -> ActionClaim:
        validated = self._validate_retryable(command)
        storage_key = self._storage_key(validated.key)
        with self._lock:
            trusted_now = self._trusted_now()
            existing = self._require_owned_claim(
                storage_key=storage_key,
                binding=validated.binding,
                expected_claim_revision=validated.expected_claim_revision,
                fencing_token=validated.fencing_token,
            )
            self._validate_event_time(validated.marked_at, trusted_now)
            if existing.state is not ActionClaimState.ACTIVE:
                raise ActionClaimTransitionError(
                    "action_claim_transition_invalid",
                    "only an active Action claim can become retryable",
                )
            if trusted_now >= existing.lease_expires_at:
                raise ActionClaimTransitionError(
                    "action_claim_lease_expired",
                    "an expired Action claim cannot become retryable",
                )
            retryable = existing.model_copy(
                update={"state": ActionClaimState.RETRYABLE}
            )
            self._records[storage_key] = retryable
            return retryable

    def finalize(self, command: ActionClaimFinalizeCommand) -> ActionClaim:
        validated = self._validate_finalize(command)
        storage_key = self._storage_key(validated.key)
        with self._lock:
            trusted_now = self._trusted_now()
            existing = self._require_owned_claim(
                storage_key=storage_key,
                binding=validated.binding,
                expected_claim_revision=validated.expected_claim_revision,
                fencing_token=validated.fencing_token,
            )
            self._validate_event_time(validated.outcome.finalized_at, trusted_now)
            if existing.state is ActionClaimState.TERMINAL:
                if existing.terminal_outcome == validated.outcome:
                    return existing
                raise ActionClaimOutcomeConflictError(
                    "action_claim_outcome_conflict",
                    "terminal Action claim is bound to a different outcome",
                )
            if existing.state is not ActionClaimState.ACTIVE:
                raise ActionClaimTransitionError(
                    "action_claim_transition_invalid",
                    "only an active Action claim can be finalized",
                )
            if trusted_now >= existing.lease_expires_at:
                raise ActionClaimTransitionError(
                    "action_claim_lease_expired",
                    "an expired Action claim cannot be finalized",
                )
            terminal = existing.model_copy(
                update={
                    "state": ActionClaimState.TERMINAL,
                    "terminal_outcome": validated.outcome,
                }
            )
            self._records[storage_key] = terminal
            return terminal

    @staticmethod
    def _system_utc_now() -> datetime:
        return datetime.now(UTC)

    def _trusted_now(self) -> datetime:
        try:
            value = self._clock()
        except Exception as error:
            raise ActionClaimContractError(
                "action_claim_clock_invalid",
                "trusted Action claim clock failed",
            ) from error
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ActionClaimContractError(
                "action_claim_clock_invalid",
                "trusted Action claim clock must return an aware datetime",
            )
        trusted = value.astimezone(UTC)
        if self._last_trusted_at is not None and trusted < self._last_trusted_at:
            raise ActionClaimContractError(
                "action_claim_clock_regressed",
                "trusted Action claim clock must not move backwards",
            )
        self._last_trusted_at = trusted
        return trusted

    @staticmethod
    def _validate_reserve_time(
        command: ActionClaimRequest,
        trusted_now: datetime,
    ) -> None:
        if command.requested_at > trusted_now:
            raise ActionClaimContractError(
                "action_claim_time_invalid",
                "Action claim requested_at cannot be later than trusted time",
            )
        if command.lease_expires_at <= trusted_now:
            raise ActionClaimContractError(
                "action_claim_time_invalid",
                "Action claim lease must extend beyond trusted time",
            )

    @staticmethod
    def _validate_event_time(event_at: datetime, trusted_now: datetime) -> None:
        if event_at > trusted_now:
            raise ActionClaimContractError(
                "action_claim_time_invalid",
                "Action claim event time cannot be later than trusted time",
            )

    def _new_claim(
        self,
        command: ActionClaimRequest,
        *,
        claim_revision: int,
    ) -> ActionClaim:
        return ActionClaim(
            key=command.key,
            binding=command.binding,
            state=ActionClaimState.ACTIVE,
            claim_revision=claim_revision,
            fencing_token=self._new_fencing_token(),
            lease_expires_at=command.lease_expires_at,
        )

    def _new_fencing_token(self) -> str:
        while True:  # pragma: no branch - UUID collision defense
            candidate = uuid4().hex
            if candidate not in self._issued_fences:
                self._issued_fences.add(candidate)
                return candidate

    def _require_owned_claim(
        self,
        *,
        storage_key: tuple[str, str, str],
        binding: object,
        expected_claim_revision: int,
        fencing_token: str,
    ) -> ActionClaim:
        existing = self._records.get(storage_key)
        if existing is None:
            raise ActionClaimNotFoundError(
                "action_claim_not_found",
                "no Action claim exists for the exact reservation key",
            )
        if existing.binding != binding:
            raise ActionClaimBindingConflictError(
                "action_claim_binding_conflict",
                "Action claim is bound to different immutable execution content",
            )
        if existing.fencing_token != fencing_token:
            raise ActionClaimStaleFenceError(
                "action_claim_stale_fence",
                "fencing token is not the current Action claim owner",
            )
        if existing.claim_revision != expected_claim_revision:
            raise ActionClaimRevisionConflictError(
                "action_claim_revision_conflict",
                "expected revision does not equal the current Action claim revision",
            )
        return existing

    @staticmethod
    def _storage_key(key: ActionReservationKey) -> tuple[str, str, str]:
        return (key.tenant_id, key.action_stable_name, key.idempotency_key)

    @staticmethod
    def _validate_reserve(command: ActionClaimRequest) -> ActionClaimRequest:
        if type(command) is not ActionClaimRequest:
            raise ActionClaimContractError(
                "action_claim_command_invalid",
                "reserve requires the exact ActionClaimRequest contract",
            )
        try:
            return ActionClaimRequest.model_validate(command)
        except ValidationError as error:
            raise ActionClaimContractError(
                "action_claim_command_invalid",
                "Action claim request is invalid",
            ) from error

    @staticmethod
    def _validate_retryable(
        command: ActionClaimRetryableCommand,
    ) -> ActionClaimRetryableCommand:
        if type(command) is not ActionClaimRetryableCommand:
            raise ActionClaimContractError(
                "action_claim_command_invalid",
                "mark_retryable requires the exact ActionClaimRetryableCommand contract",
            )
        try:
            return ActionClaimRetryableCommand.model_validate(command)
        except ValidationError as error:
            raise ActionClaimContractError(
                "action_claim_command_invalid",
                "Action claim retryable command is invalid",
            ) from error

    @staticmethod
    def _validate_finalize(
        command: ActionClaimFinalizeCommand,
    ) -> ActionClaimFinalizeCommand:
        if type(command) is not ActionClaimFinalizeCommand:
            raise ActionClaimContractError(
                "action_claim_command_invalid",
                "finalize requires the exact ActionClaimFinalizeCommand contract",
            )
        try:
            return ActionClaimFinalizeCommand.model_validate(command)
        except ValidationError as error:
            raise ActionClaimContractError(
                "action_claim_command_invalid",
                "Action claim finalize command is invalid",
            ) from error


class InMemoryExternalWriteAttemptStore:
    """Thread-safe reference ledger for governed external write attempts.

    Transition legality and CAS live in
    :func:`eios.actions.write_attempts.validate_transition`, shared with the
    PostgreSQL store so both engines run the same state machine.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str, str], ExternalWriteAttempt] = {}

    def start(
        self, command: ExternalWriteAttemptStartCommand
    ) -> ExternalWriteAttempt:
        validated = self._validate(
            ExternalWriteAttemptStartCommand, command, operation="start"
        )
        storage_key = (
            validated.key.tenant_id,
            validated.key.request_digest,
            validated.key.target_system,
        )
        with self._lock:
            existing = self._records.get(storage_key)
            if existing is not None:
                return existing.model_copy(deep=True)
            attempt = ExternalWriteAttempt(
                key=validated.key,
                action_stable_name=validated.action_stable_name,
                attempt_kind=validated.attempt_kind,
                target_system=validated.target_system,
                marker=validated.marker,
                request_target_identity=dict(validated.request_target_identity),
                external_units=validated.external_units,
                facility_block_id=validated.facility_block_id,
                retry_preconditions=dict(validated.retry_preconditions),
                state=ExternalWriteAttemptState.STARTED,
                attempt_revision=1,
                retry_count=0,
                detected_at=validated.started_at,
                updated_at=validated.started_at,
            )
            self._records[storage_key] = attempt
            return attempt.model_copy(deep=True)

    def get(self, key: ExternalWriteAttemptKey) -> ExternalWriteAttempt | None:
        validated_key = self._validate(
            ExternalWriteAttemptKey, key, operation="get"
        )
        with self._lock:
            existing = self._records.get(
                (
                    validated_key.tenant_id,
                    validated_key.request_digest,
                    validated_key.target_system,
                )
            )
            return None if existing is None else existing.model_copy(deep=True)

    def find_by_facility_block(
        self,
        tenant_id: str,
        facility_block_id: str,
        *,
        action_stable_name: str | None = None,
    ) -> ExternalWriteAttempt | None:
        clean_tenant = str(tenant_id or "").strip()
        clean_block = str(facility_block_id or "").strip()
        if not clean_tenant or not clean_block:
            raise ExternalWriteAttemptContractError(
                "external_write_attempt_command_invalid",
                "find_by_facility_block requires tenant and block ids",
            )
        with self._lock:
            # Both the creating action's row and any release attempt carry
            # the block id; the optional action filter lets a caller resolve
            # the CREATING attempt deterministically.
            matches = sorted(
                (
                    item
                    for item in self._records.values()
                    if item.key.tenant_id == clean_tenant
                    and item.facility_block_id == clean_block
                    and (
                        action_stable_name is None
                        or item.action_stable_name == action_stable_name
                    )
                ),
                key=lambda item: item.key.request_digest,
            )
            return None if not matches else matches[0].model_copy(deep=True)

    def find_creators_by_target_window(
        self,
        tenant_id: str,
        *,
        action_stable_name: str,
        target_system: str,
        start_at: datetime,
        court_id: str,
    ) -> tuple[ExternalWriteAttempt, ...]:
        """按「打向哪个系统 + 起始时刻」找创建写行 —— **交出全部候选,不挑**。

        与 PG 实现逐条同形(见那一份的文档):同一时刻可能有多个场地都被
        我方镜像锁,``limit 1`` 会悄悄挑一条,而认错锁就等于解错锁。
        判「恰好一条」是调用方的事。

        ⚠️ 时刻比的是**瞬间不是字符串**:锁存 ``+08:00``、请求可能传 ``+00:00``,
        同一瞬间而字符串不等。
        """

        clean_tenant = str(tenant_id or "").strip()
        clean_action = str(action_stable_name or "").strip()
        clean_target = str(target_system or "").strip()
        clean_court = str(court_id or "").strip()
        if not clean_tenant or not clean_action or not clean_target or not clean_court:
            raise ExternalWriteAttemptContractError(
                "external_write_attempt_command_invalid",
                "find_creators_by_target_window requires tenant, action, target and court",
            )
        with self._lock:
            matches = []
            for item in self._records.values():
                if (
                    item.key.tenant_id != clean_tenant
                    or item.action_stable_name != clean_action
                    or item.key.target_system != clean_target
                    or item.request_target_identity.get("court_id") != clean_court
                ):
                    continue
                stored = item.request_target_identity.get("start_at")
                if not isinstance(stored, str):
                    continue
                try:
                    moment = datetime.fromisoformat(stored)
                except ValueError:
                    continue
                if moment == start_at:
                    matches.append(item)
            matches.sort(key=lambda item: item.key.request_digest)
            return tuple(item.model_copy(deep=True) for item in matches)

    def complete(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.COMPLETED)

    def reject(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.REJECTED)

    def mark_unknown(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.UNKNOWN)

    def mark_safe_to_retry(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.SAFE_TO_RETRY)

    def claim_retry(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.RETRY_CLAIMED)

    def mark_retry_exhausted(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.RETRY_EXHAUSTED)

    def mark_retry_unknown(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.RETRY_UNKNOWN)

    def mark_retry_conflict(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.RETRY_CONFLICT)

    def mark_orphan_conflict(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt:
        return self._transition(command, ExternalWriteAttemptState.ORPHAN_CONFLICT)

    def _transition(
        self,
        command: ExternalWriteAttemptTransitionCommand,
        target: ExternalWriteAttemptState,
    ) -> ExternalWriteAttempt:
        validated = self._validate(
            ExternalWriteAttemptTransitionCommand, command, operation=target.value
        )
        storage_key = (
            validated.key.tenant_id,
            validated.key.request_digest,
            validated.key.target_system,
        )
        with self._lock:
            current = self._records.get(storage_key)
            if current is None:
                raise ExternalWriteAttemptNotFound(
                    "external_write_attempt_not_found",
                    "no write attempt exists for the exact attempt key",
                )
            update = validate_transition(current, validated, target=target)
            stored = current.model_copy(update=update, deep=True)
            self._records[storage_key] = stored
            return stored.model_copy(deep=True)

    @staticmethod
    def _validate(model_type, command, *, operation: str):
        if type(command) is not model_type:
            raise ExternalWriteAttemptContractError(
                "external_write_attempt_command_invalid",
                f"{operation} requires the exact {model_type.__name__} contract",
            )
        try:
            return model_type.model_validate(command)
        except ValidationError as error:
            raise ExternalWriteAttemptContractError(
                "external_write_attempt_command_invalid",
                f"external write attempt {operation} command is invalid",
            ) from error
