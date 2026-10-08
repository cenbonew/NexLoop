from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class ActionWriteContext:
    tenant_id: str
    action_name: str
    permit_reference: str


_ACTIVE_ACTION_WRITE: ContextVar[ActionWriteContext | None] = ContextVar(
    "eios_active_ontology_action_write",
    default=None,
)


@contextmanager
def governed_action_write(
    *,
    tenant_id: str,
    action_name: str,
    permit_reference: str,
) -> Iterator[ActionWriteContext]:
    context = ActionWriteContext(
        tenant_id=str(tenant_id).strip(),
        action_name=str(action_name).strip(),
        permit_reference=str(permit_reference).strip(),
    )
    if not all((context.tenant_id, context.action_name, context.permit_reference)):
        raise ValueError("complete Action write authority is required")
    token = _ACTIVE_ACTION_WRITE.set(context)
    try:
        yield context
    finally:
        _ACTIVE_ACTION_WRITE.reset(token)


def current_action_write() -> ActionWriteContext | None:
    return _ACTIVE_ACTION_WRITE.get()


__all__ = ["ActionWriteContext", "current_action_write", "governed_action_write"]
