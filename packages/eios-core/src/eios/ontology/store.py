from __future__ import annotations

import logging

from datetime import UTC, date, datetime, timedelta
import threading
from collections import Counter
from typing import Any, Protocol, Sequence, TypeVar
import uuid

from pydantic import BaseModel, Field

from .models import (
    ObjectTypeDefinition,
    RelationCardinality,
    validate_event_payload,
    validate_object_properties,
    validate_relation_properties,
)
from .atomic_projection import (
    AtomicObjectUpsert,
    ObjectProjectionRevisionConflict,
    validate_atomic_projection_batch,
)
from eios.kernel.ports.repositories import OntologyRegistry

from .markings import visible_to
from .semantics import natural_object_id, payload_digest, require_idempotency_key
from .action_write_context import current_action_write


# 删除墓碑的滚动保留期。prune 每轮回收更早的墓碑,因此以更早的水位增量
# 拉取会漏删——读路径据此 fail-closed 报 ``watermark_expired``。与 0129/0132
# 的 Postgres prune 函数中的 ``interval '30 days'`` 同语义,改动须同步。
TOMBSTONE_RETENTION_DAYS = 30


class OntologyStoreError(RuntimeError):
    code = "ontology_store_error"


class ObjectNotFound(OntologyStoreError):
    code = "object_not_found"


class RelationEndpointMismatch(OntologyStoreError):
    code = "relation_endpoint_mismatch"


class RelationCardinalityViolation(OntologyStoreError):
    code = "relation_cardinality_violation"


class RelationNotFound(OntologyStoreError):
    code = "relation_not_found"


class IdempotencyConflict(OntologyStoreError):
    code = "idempotency_conflict"


class ObjectWriteRequiresAction(OntologyStoreError):
    code = "object_write_requires_action"


class ObjectRecord(BaseModel):
    tenant_id: str
    type_name: str
    object_id: str
    schema_version: int
    properties: dict[str, Any] = Field(default_factory=dict)
    source_system: str = ""
    source_ref: str = ""
    created_at: datetime
    updated_at: datetime
    # Stage 9: mandatory access markings on the row (empty = unmarked).
    markings: tuple[str, ...] = ()


class ObjectTombstone(BaseModel):
    """滚动窗口 prune 留下的删除墓碑(增量同步的删除感知)。

    对象是硬删的;墓碑是删除留下的唯一可观测痕迹,供 ``updated_since``
    增量拉取以 ``deleted: true`` 条目并入变更流。墓碑本身有
    ``TOMBSTONE_RETENTION_DAYS`` 天保留期(prune 每轮顺带回收),断联超期的
    拉取方必须全量重同步(读路径显式报 ``watermark_expired``,不静默漏删)。

    ``court_id``/``start_at`` 是删除瞬间的最小属性快照(0132):没有它,
    属性过滤后的变更流会静默漏删,``updated_since`` 就只能与属性过滤互斥。
    删除时对象无该属性则为空串。
    """

    tenant_id: str
    type_name: str
    object_id: str
    deleted_at: datetime
    court_id: str = ""
    start_at: str = ""


class RelationRecord(BaseModel):
    tenant_id: str
    relation_id: str
    relation_name: str
    schema_version: int
    source_type: str
    source_object_id: str
    target_type: str
    target_object_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # Link lifecycle (stage 6): soft deletion so the world model can state
    # "this relationship has ended" without erasing history.
    status: str = "active"
    deleted_at: datetime | None = None
    # Bitemporal valid time (stage 12, additive): valid_from inclusive,
    # valid_to exclusive (None = still valid). None valid_from reads as the
    # relation's created_at, so pre-stage-12 rows are "valid from creation,
    # open-ended". Independent of system time (created_at/deleted_at).
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def is_valid_at(self, instant: datetime) -> bool:
        start = self.valid_from or self.created_at
        if instant < start:
            return False
        return self.valid_to is None or instant < self.valid_to


def _intervals_overlap(
    a_start: datetime,
    a_end: datetime | None,
    b_start: datetime,
    b_end: datetime | None,
) -> bool:
    """Half-open [start, end) overlap; a None end is open-ended (+inf).

    Two Links conflict on cardinality only when their valid intervals actually
    overlap — the same endpoint may hold sequentially over disjoint windows.
    """

    if a_end is not None and a_end <= b_start:
        return False
    if b_end is not None and b_end <= a_start:
        return False
    return True


class EventRecord(BaseModel):
    tenant_id: str
    event_id: str
    event_type: str
    object_type: str
    object_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_id: str = ""
    sequence: int
    created_at: datetime


class ObjectProjection(BaseModel):
    object: ObjectRecord
    outgoing_relations: tuple[RelationRecord, ...] = ()
    incoming_relations: tuple[RelationRecord, ...] = ()
    events: tuple[EventRecord, ...] = ()


class SubgraphNode(BaseModel):
    """One object reached while walking the relation neighbourhood."""

    type_name: str
    object_id: str
    # Hops from the root along the shortest walk; the root itself is 0.
    depth: int


class SubgraphEdge(BaseModel):
    """One directed relation edge inside the reached neighbourhood."""

    relation_name: str
    source_type: str
    source_object_id: str
    target_type: str
    target_object_id: str


class ObjectSubgraph(BaseModel):
    """The bounded relation neighbourhood around a root object.

    ``nodes`` is every object reachable within ``max_depth`` undirected hops
    (the root at depth 0); ``edges`` is the induced subgraph — every active
    relation whose both endpoints are in ``nodes``. Topology only: it exposes
    type/id/relation identifiers, never object properties.
    """

    root_type: str
    root_object_id: str
    max_depth: int
    nodes: tuple[SubgraphNode, ...] = ()
    edges: tuple[SubgraphEdge, ...] = ()


class OntologyStore(Protocol):
    def get_object(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        as_of: datetime | None = None,
    ) -> ObjectProjection: ...

    def read_object_subgraph(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        max_depth: int,
        as_of: datetime | None = None,
    ) -> ObjectSubgraph: ...


T = TypeVar("T", bound=BaseModel)


def object_request_payload(
    *,
    type_name: str,
    object_id: str | None,
    properties: Any,
    source_system: str,
    source_ref: str,
    markings: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """对象 upsert 的请求指纹载荷 —— **只此一处构造**。

    原子投影路径与普通 upsert 路径各自拼过一遍这个 dict,加 markings 时只
    改了一处,重放当场 IdempotencyConflict(首写记的 digest 含 marking、
    重放算的不含)。同一个语义写两遍,迟早各走各的;PG 适配器的
    ``_object_request_digest`` 也复用它,两实现的指纹字段集因此不可能漂移。
    """

    return {
        "type_name": type_name,
        "object_id": object_id,
        "properties": properties,
        "source_system": source_system,
        "source_ref": source_ref,
        # markings 是请求的一部分:不进指纹的话,"同键、属性没变、只加了
        # marking"会命中幂等记录原样返回,marking 不落地也不报错。
        "markings": sorted(set(markings)),
    }


#: 旧口径摘要命中次数(进程内累计)。**双口径是止血不是终态** —— 靠这个
#: 计数才能看出存量还剩多少、什么时候可以做一次性摘要重算迁移把双口径退役。
#: 不打这个点,双口径会悄悄变成永久状态,而没人知道它还在被用。
_LOGGER = logging.getLogger(__name__)

OLD_DIGEST_MATCHES = Counter()


def legacy_object_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """2026-08-21 23:19(3bfe7b70)之前的载荷形状:**没有 `markings` 这个键**。

    注意不是「markings 为空」而是「键不存在」—— `json.dumps` 下这两者摘要
    完全不同。存量 41591 条(87.5%)记录都是这个形状。
    """

    return {k: v for k, v in payload.items() if k != "markings"}


def object_request_digest_matches(
    recorded_digest: str, payload: dict[str, Any]
) -> bool:
    """重放校验:新口径不匹配时,**有条件地**回退比对旧口径。

    ## 为什么需要
    `3bfe7b70` 把 `markings` 加进请求指纹(修的是真缺陷:补 marking 会命中
    幂等记录原样返回,marking 不落地也不报错)。但存量幂等记录是按旧口径写的
    ⇒ 同键算出不同摘要 ⇒ `IdempotencyConflict` 且**永不自愈**。
    2026-08-24 生产实测:41591/47553 条(87.5%)旧口径,横跨 10+ 通道;
    按需采集因为天天重放同一批稳定键,08-22 起 95% 失败、**两天零告警**。

    ## 🔴 为什么只在 markings 为空时才回退
    回退的本质是「忽略 markings 这个维度」。如果新请求**带着**非空 markings
    还允许回退,那么「同键、属性没变、只加了 marking」又会命中旧记录原样返回
    —— **正好把 3bfe7b70 修的那个缺陷原样放回去**。
    所以:无 marking 的重放放行(绝大多数,即本案),带 marking 的重放**仍然
    显式冲突**。判据 test_*_markings_non_empty_never_falls_back 钉死这一条。

    ## 这是止血,不是终态
    旧口径命中打 ``OLD_DIGEST_MATCHES`` 计数,让存量消耗可观测;归零之后
    才谈得上做一次性摘要重算迁移、把双口径退役。
    """

    if recorded_digest == payload_digest(payload):
        return True
    if payload.get("markings"):
        # 带 marking 的请求绝不回退 —— 见上。
        return False
    if recorded_digest == payload_digest(legacy_object_request_payload(payload)):
        OLD_DIGEST_MATCHES["object.upsert"] += 1
        # 🔴 进程内计数**没有出口**:读不到、进程一重启就归零 —— 而它的用途
        # 恰恰是「看存量还剩多少、什么时候能退役双口径」,那是跨周、跨重启的
        # 判断。docstring 里写着「不打这个点没人知道」,只打这个点也照样没人
        # 知道。(与「兜底组件的状态不能只活在进程内存里」同型,这次在遥测面。)
        # ⇒ 同时写一条结构化日志行:journal 可查、可跨重启统计。
        _LOGGER.info(
            "ontology.idempotency.legacy_digest_match",
            extra={
                "event": "ontology.idempotency.legacy_digest_match",
                "operation": "object.upsert",
                "type_name": str(payload.get("type_name") or ""),
                "process_total": OLD_DIGEST_MATCHES["object.upsert"],
            },
        )
        return True
    return False


class InMemoryOntologyStore:
    def __init__(self, registry: OntologyRegistry) -> None:
        self._registry = registry
        self._objects: dict[tuple[str, str, str], ObjectRecord] = {}
        self._relations: dict[tuple[str, str], RelationRecord] = {}
        # (tenant, type, object_id) -> (deleted_at, court_id, start_at):
        # 值里的属性快照是删除瞬间抄下的,墓碑因此可以参与属性过滤。
        self._tombstones: dict[tuple[str, str, str], tuple[datetime, str, str]] = {}
        self._events: dict[tuple[str, str, str], list[EventRecord]] = {}
        self._idempotency: dict[
            tuple[str, str, str], tuple[str, BaseModel, datetime]
        ] = {}
        self._lock = threading.RLock()

    def _idempotent_result(
        self,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        digest: str = "",
        payload: dict[str, Any] | None = None,
    ) -> BaseModel | None:
        """``payload`` 给了就走双口径判定(见 ``object_request_digest_matches``)。

        ⚠️ 只有**读侧**(重放校验)走双口径;``_remember_idempotency`` 那条
        **写侧**路径一律用新口径,一个字不能动 —— 否则新写入的记录也变成旧
        口径,双口径就永远退役不掉。
        只有对象 upsert 传 ``payload``:别的 operation 的载荷从来没有过
        `markings` 这个键,不存在旧口径,传了反而给它们开一道不该有的回退。
        """

        contract = (tenant_id, operation, require_idempotency_key(idempotency_key))
        existing = self._idempotency.get(contract)
        if existing is None:
            return None
        existing_digest, result, _ = existing
        matched = (
            object_request_digest_matches(existing_digest, payload)
            if payload is not None
            else existing_digest == digest
        )
        if not matched:
            raise IdempotencyConflict(
                f"idempotency key reused with different payload: {idempotency_key}"
            )
        return result.model_copy(deep=True)

    def _remember_idempotency(
        self,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        digest: str,
        result: BaseModel,
    ) -> None:
        self._idempotency[
            (tenant_id, operation, require_idempotency_key(idempotency_key))
        ] = (
            digest,
            result.model_copy(deep=True),
            datetime.now(UTC),
        )

    @staticmethod
    def _object_key(
        tenant_id: str, type_name: str, object_id: str
    ) -> tuple[str, str, str]:
        return (str(tenant_id), str(type_name), str(object_id))

    def _load_object(
        self, tenant_id: str, type_name: str, object_id: str
    ) -> ObjectRecord:
        item = self._objects.get(self._object_key(tenant_id, type_name, object_id))
        if item is None:
            raise ObjectNotFound(f"object not found: {type_name}/{object_id}")
        return item

    def upsert_object(
        self,
        *,
        tenant_id: str,
        type_name: str,
        properties: dict[str, Any],
        idempotency_key: str,
        object_id: str | None = None,
        source_system: str = "",
        source_ref: str = "",
        actor_id: str = "",
        markings: tuple[str, ...] = (),
    ) -> ObjectRecord:
        schema = self._registry.get_object_type(tenant_id, type_name)
        self._assert_object_write_authorized(tenant_id, schema)
        derived_id = natural_object_id(schema, properties)
        if derived_id is not None and object_id not in (None, derived_id):
            raise IdempotencyConflict(
                "object_id conflicts with the declared natural key"
            )
        resolved_id = str(object_id or derived_id or f"obj_{uuid.uuid4().hex}")
        request_payload = object_request_payload(
            type_name=type_name,
            object_id=resolved_id,
            properties=properties,
            source_system=source_system,
            source_ref=source_ref,
            markings=markings,
        )
        digest = payload_digest(request_payload)
        with self._lock:
            reused = self._idempotent_result(
                tenant_id=tenant_id,
                operation="object.upsert",
                idempotency_key=idempotency_key,
                payload=request_payload,
            )
            if reused is not None:
                return ObjectRecord.model_validate(reused)
            key = self._object_key(tenant_id, type_name, resolved_id)
            current = self._objects.get(key)
            if current is None:
                normalized = validate_object_properties(schema, properties)
                now = datetime.now(UTC)
                stored = ObjectRecord(
                    tenant_id=tenant_id,
                    type_name=type_name,
                    object_id=resolved_id,
                    schema_version=schema.version,
                    properties=normalized,
                    source_system=source_system,
                    source_ref=source_ref,
                    created_at=now,
                    updated_at=now,
                    markings=tuple(sorted(set(markings))),
                )
                event_type = "ObjectCreated"
                event_payload = {"properties": normalized}
            else:
                merged = dict(current.properties)
                merged.update(properties)
                normalized = validate_object_properties(schema, merged)
                changed_fields = sorted(
                    name
                    for name in properties
                    if current.properties.get(name) != normalized.get(name)
                )
                # 无实变的重放(merge 后与现存全等、来源/版本/标记均未变)
                # 直接返回现状:不写行、不发 ObjectUpdated、不记幂等——
                # no-op 天然幂等,同键重试仍会落到这里。与 Postgres 版一致。
                normalized_markings = tuple(sorted(set(markings)))
                if (
                    normalized == current.properties
                    and schema.version == current.schema_version
                    and source_system in ("", current.source_system)
                    and source_ref in ("", current.source_ref)
                    and (
                        not normalized_markings
                        or normalized_markings == current.markings
                    )
                ):
                    return current.model_copy(deep=True)
                stored = current.model_copy(
                    update={
                        "schema_version": schema.version,
                        "properties": normalized,
                        "source_system": source_system or current.source_system,
                        "source_ref": source_ref or current.source_ref,
                        "updated_at": datetime.now(UTC),
                        "markings": (
                            tuple(sorted(set(markings)))
                            if markings
                            else current.markings
                        ),
                    },
                    deep=True,
                )
                event_type = "ObjectUpdated"
                event_payload = {"changed_fields": changed_fields}
            self._objects[key] = stored
            self._append_event_record(
                tenant_id=tenant_id,
                event_type=event_type,
                object_type=type_name,
                object_id=resolved_id,
                payload=event_payload,
                actor_id=actor_id,
            )
            self._remember_idempotency(
                tenant_id=tenant_id,
                operation="object.upsert",
                idempotency_key=idempotency_key,
                digest=digest,
                result=stored,
            )
            return stored.model_copy(deep=True)

    @staticmethod
    def _assert_object_write_authorized(
        tenant_id: str, schema: ObjectTypeDefinition
    ) -> None:
        if not schema.only_edit_via_actions:
            return
        context = current_action_write()
        if context is None or context.tenant_id != str(tenant_id):
            raise ObjectWriteRequiresAction(
                f"object type {schema.type_name} requires a valid Action execution permit"
            )

    def upsert_objects_atomically(
        self,
        items: Sequence[AtomicObjectUpsert],
    ) -> tuple[ObjectRecord, ...]:
        batch = validate_atomic_projection_batch(items)
        with self._lock:
            objects_before = {
                key: value.model_copy(deep=True) for key, value in self._objects.items()
            }
            events_before = {
                key: [event.model_copy(deep=True) for event in value]
                for key, value in self._events.items()
            }
            idempotency_before = {
                key: (digest, result.model_copy(deep=True), recorded_at)
                for key, (digest, result, recorded_at) in self._idempotency.items()
            }
            try:
                records: list[ObjectRecord] = []
                for item in batch:
                    schema = self._registry.get_object_type(
                        item.tenant_id,
                        item.type_name,
                    )
                    derived_id = natural_object_id(schema, item.properties)
                    if derived_id is not None and item.object_id != derived_id:
                        raise IdempotencyConflict(
                            "object_id conflicts with the declared natural key"
                        )
                    reused = self._idempotent_result(
                        tenant_id=item.tenant_id,
                        operation="object.upsert",
                        idempotency_key=item.idempotency_key,
                        payload=(
                            object_request_payload(
                                type_name=item.type_name,
                                object_id=item.object_id,
                                properties=item.properties,
                                source_system=item.source_system,
                                source_ref=item.source_ref,
                                markings=item.markings,
                            )
                        ),
                    )
                    if reused is not None:
                        records.append(ObjectRecord.model_validate(reused))
                        continue
                    current = self._objects.get(
                        self._object_key(
                            item.tenant_id,
                            item.type_name,
                            item.object_id,
                        )
                    )
                    self._check_projection_revision(item, current)
                    records.append(
                        self.upsert_object(
                            tenant_id=item.tenant_id,
                            type_name=item.type_name,
                            object_id=item.object_id,
                            properties=item.properties,
                            idempotency_key=item.idempotency_key,
                            source_system=item.source_system,
                            source_ref=item.source_ref,
                            actor_id=item.actor_id,
                            markings=item.markings,
                        )
                    )
                return tuple(records)
            except Exception:
                self._objects = objects_before
                self._events = events_before
                self._idempotency = idempotency_before
                raise

    @staticmethod
    def _check_projection_revision(
        item: AtomicObjectUpsert,
        current: ObjectRecord | None,
    ) -> None:
        if item.expected_revision == 0:
            matches = current is None
        else:
            revision = None if current is None else current.properties.get("revision")
            matches = type(revision) is int and revision == item.expected_revision
        if not matches:
            raise ObjectProjectionRevisionConflict(
                "ontology object projection revision does not match "
                f"{item.type_name}/{item.object_id}"
            )

    def get_object(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        as_of: datetime | None = None,
    ) -> ObjectProjection:
        with self._lock:
            record = self._load_object(tenant_id, type_name, object_id).model_copy(
                deep=True
            )

            def _visible(item: RelationRecord) -> bool:
                if item.status != "active":
                    return False
                # Stage 12: an as-of read narrows active relations to those
                # whose valid interval contains the instant; without as_of the
                # live active set is returned unchanged.
                return as_of is None or item.is_valid_at(as_of)

            outgoing = tuple(
                item.model_copy(deep=True)
                for (_, _), item in sorted(self._relations.items())
                if item.tenant_id == tenant_id
                and item.source_type == type_name
                and item.source_object_id == object_id
                and _visible(item)
            )
            incoming = tuple(
                item.model_copy(deep=True)
                for (_, _), item in sorted(self._relations.items())
                if item.tenant_id == tenant_id
                and item.target_type == type_name
                and item.target_object_id == object_id
                and _visible(item)
            )
            events = tuple(
                item.model_copy(deep=True)
                for item in self._events.get(
                    self._object_key(tenant_id, type_name, object_id), []
                )
            )
            return ObjectProjection(
                object=record,
                outgoing_relations=outgoing,
                incoming_relations=incoming,
                events=events,
            )

    def read_object_subgraph(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        max_depth: int,
        as_of: datetime | None = None,
    ) -> ObjectSubgraph:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        with self._lock:
            # Root must exist; a missing root is ObjectNotFound (→ 404), never
            # an empty graph.
            self._load_object(tenant_id, type_name, object_id)

            def _visible(item: RelationRecord) -> bool:
                if item.tenant_id != tenant_id or item.status != "active":
                    return False
                return as_of is None or item.is_valid_at(as_of)

            active = [item for item in self._relations.values() if _visible(item)]

            # Breadth-first over undirected hops; depth records the shortest
            # walk and doubles as the visited set.
            root = (type_name, object_id)
            depth: dict[tuple[str, str], int] = {root: 0}
            frontier = [root]
            for hop in range(max_depth):
                nxt: list[tuple[str, str]] = []
                for node in frontier:
                    node_type, node_id = node
                    for item in active:
                        neighbour: tuple[str, str] | None = None
                        if (
                            item.source_type == node_type
                            and item.source_object_id == node_id
                        ):
                            neighbour = (item.target_type, item.target_object_id)
                        elif (
                            item.target_type == node_type
                            and item.target_object_id == node_id
                        ):
                            neighbour = (item.source_type, item.source_object_id)
                        if neighbour is not None and neighbour not in depth:
                            depth[neighbour] = hop + 1
                            nxt.append(neighbour)
                frontier = nxt
                if not frontier:
                    break

            node_set = set(depth)
            # Induced edges: every active relation with both endpoints reached.
            # Deduplicate identical (name, endpoints) tuples so parallel records
            # collapse to one topological edge.
            edge_keys = sorted(
                {
                    (
                        item.relation_name,
                        item.source_type,
                        item.source_object_id,
                        item.target_type,
                        item.target_object_id,
                    )
                    for item in active
                    if (item.source_type, item.source_object_id) in node_set
                    and (item.target_type, item.target_object_id) in node_set
                }
            )
            edges = tuple(
                SubgraphEdge(
                    relation_name=name,
                    source_type=st,
                    source_object_id=si,
                    target_type=tt,
                    target_object_id=ti,
                )
                for (name, st, si, tt, ti) in edge_keys
            )
            nodes = tuple(
                SubgraphNode(type_name=t, object_id=i, depth=depth[(t, i)])
                for (t, i) in sorted(node_set)
            )
            return ObjectSubgraph(
                root_type=type_name,
                root_object_id=object_id,
                max_depth=max_depth,
                nodes=nodes,
                edges=edges,
            )

    @staticmethod
    def _matches_type(
        item_type: str,
        type_name: str | None,
        type_names: tuple[str, ...] | None,
    ) -> bool:
        """单值与多值类型过滤(多值即 SQL 侧的 ``type_name = any(...)``)。"""

        if type_names is not None:
            return item_type in type_names
        return type_name is None or item_type == type_name

    @staticmethod
    def _matches_snapshot_filters(
        court_id_value: str,
        start_at_value: str,
        *,
        court_id: str | None,
        starts_after: str | None,
        starts_before: str | None,
    ) -> bool:
        """墓碑属性快照上的同一套过滤语义(空串 = 删除时无该属性 = 不命中)。"""

        if court_id is not None and court_id_value != court_id:
            return False
        if starts_after is not None or starts_before is not None:
            if not start_at_value:
                return False
            if starts_after is not None and start_at_value < starts_after:
                return False
            if starts_before is not None and start_at_value >= starts_before:
                return False
        return True

    @staticmethod
    def _matches_property_filters(
        record: ObjectRecord,
        *,
        court_id: str | None,
        starts_after: str | None,
        starts_before: str | None,
    ) -> bool:
        """属性过滤与 Postgres 的 ``properties->>...`` 文本比较同语义。

        start_at 是全库统一 ``+08:00`` 偏移的 ISO 字符串,同格式下字典序即
        时间序;缺 start_at 的对象在任一时间范围条件下都不命中(与 SQL 中
        NULL 比较为假一致)。starts_after 含端点,starts_before 不含(半开区间)。
        """

        if court_id is not None and record.properties.get("court_id") != court_id:
            return False
        if starts_after is not None or starts_before is not None:
            start_at = record.properties.get("start_at")
            if type(start_at) is not str:
                return False
            if starts_after is not None and start_at < starts_after:
                return False
            if starts_before is not None and start_at >= starts_before:
                return False
        return True

    def list_objects(
        self,
        tenant_id: str,
        *,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        limit: int | None = None,
        after: tuple[str, str] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        caller_markings: frozenset[str] | None = None,
    ) -> list[ObjectRecord]:
        """``caller_markings`` 为 None 时不做 marking 过滤。

        None 是**内部编排**用的(物化、seed、对账那些没有外部调用方的路径);
        任何面向调用方的列举入口都必须显式传一个 frozenset —— 传空集就是
        "没有任何 clearance",而不是"不过滤"。
        """

        with self._lock:
            # sorted() on the (tenant, type, id) keys yields (type_name,
            # object_id) order within a tenant, matching the Postgres store's
            # ``order by type_name, object_id`` so keyset cursors agree.
            items = [
                item.model_copy(deep=True)
                for (item_tenant, item_type, item_id), item in sorted(
                    self._objects.items()
                )
                if item_tenant == tenant_id
                and self._matches_type(item_type, type_name, type_names)
                and (after is None or (item_type, item_id) > after)
                and self._matches_property_filters(
                    item,
                    court_id=court_id,
                    starts_after=starts_after,
                    starts_before=starts_before,
                )
                # marking 过滤必须在 limit 截断**之前** —— 先截断再滤会让
                # 一页凭空变短甚至变空,分页方据此以为到底了。
                and (
                    caller_markings is None
                    or visible_to(item.markings, caller_markings)
                )
            ]
            if limit is not None:
                items = items[:limit]
            return items

    def list_objects_by_ids(
        self,
        tenant_id: str,
        type_name: str,
        object_ids: tuple[str, ...],
    ) -> list[ObjectRecord]:
        wanted = set(object_ids)
        with self._lock:
            return [
                item.model_copy(deep=True)
                for (item_tenant, item_type, item_id), item in sorted(
                    self._objects.items()
                )
                if item_tenant == tenant_id
                and item_type == type_name
                and item_id in wanted
            ]

    def count_objects(
        self,
        tenant_id: str,
        *,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        caller_markings: frozenset[str] | None = None,
    ) -> int:
        with self._lock:
            return sum(
                1
                for (item_tenant, item_type, _), item in self._objects.items()
                if item_tenant == tenant_id
                and self._matches_type(item_type, type_name, type_names)
                and self._matches_property_filters(
                    item,
                    court_id=court_id,
                    starts_after=starts_after,
                    starts_before=starts_before,
                )
                # total 也要按可见行算:虚高的总数就是存在性泄漏 ——
                # "你看不见,但我告诉你有 12 条"。
                and (
                    caller_markings is None
                    or visible_to(item.markings, caller_markings)
                )
            )

    def list_changed_objects(
        self,
        tenant_id: str,
        *,
        updated_since: datetime,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        limit: int | None = None,
        after: tuple[datetime, str, str] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        caller_markings: frozenset[str] | None = None,
    ) -> list[ObjectRecord]:
        """``updated_at`` 严格晚于 ``updated_since`` 的对象,按
        ``(updated_at, type_name, object_id)`` 升序(增量拉取的变更流序)。

        ``caller_markings`` 语义同 :meth:`list_objects`。变更流是第三条列举
        入口 —— 单点读那道闸同样管不到它。"""

        with self._lock:
            items = sorted(
                (
                    item.model_copy(deep=True)
                    for (item_tenant, item_type, _), item in self._objects.items()
                    if item_tenant == tenant_id
                    and self._matches_type(item_type, type_name, type_names)
                    and item.updated_at > updated_since
                    and self._matches_property_filters(
                        item,
                        court_id=court_id,
                        starts_after=starts_after,
                        starts_before=starts_before,
                    )
                    and (
                        caller_markings is None
                        or visible_to(item.markings, caller_markings)
                    )
                ),
                key=lambda item: (item.updated_at, item.type_name, item.object_id),
            )
            if after is not None:
                items = [
                    item
                    for item in items
                    if (item.updated_at, item.type_name, item.object_id) > after
                ]
            if limit is not None:
                items = items[:limit]
            return items

    def list_object_tombstones(
        self,
        tenant_id: str,
        *,
        deleted_since: datetime,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        limit: int | None = None,
        after: tuple[datetime, str, str] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
    ) -> list[ObjectTombstone]:
        """``deleted_at`` 严格晚于 ``deleted_since`` 的删除墓碑,排序键与
        ``list_changed_objects`` 一致,两条流可按同一游标归并。"""

        with self._lock:
            items = sorted(
                (
                    ObjectTombstone(
                        tenant_id=item_tenant,
                        type_name=item_type,
                        object_id=item_id,
                        deleted_at=snapshot[0],
                        court_id=snapshot[1],
                        start_at=snapshot[2],
                    )
                    for (
                        item_tenant,
                        item_type,
                        item_id,
                    ), snapshot in self._tombstones.items()
                    if item_tenant == tenant_id
                    and self._matches_type(item_type, type_name, type_names)
                    and snapshot[0] > deleted_since
                    and self._matches_snapshot_filters(
                        snapshot[1],
                        snapshot[2],
                        court_id=court_id,
                        starts_after=starts_after,
                        starts_before=starts_before,
                    )
                ),
                key=lambda item: (item.deleted_at, item.type_name, item.object_id),
            )
            if after is not None:
                items = [
                    item
                    for item in items
                    if (item.deleted_at, item.type_name, item.object_id) > after
                ]
            if limit is not None:
                items = items[:limit]
            return items

    def list_relations(
        self,
        tenant_id: str,
        *,
        relation_name: str | None = None,
        limit: int | None = None,
        after: str | None = None,
    ) -> list[RelationRecord]:
        # Active-only, relation_id order: mirrors the Postgres store so keyset
        # cursors agree and the link-precheck sees the same predicate as
        # link_relation's exact-row short-circuit.
        with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in sorted(
                    self._relations.values(), key=lambda item: item.relation_id
                )
                if item.tenant_id == tenant_id
                and item.status == "active"
                and (relation_name is None or item.relation_name == relation_name)
                and (after is None or item.relation_id > after)
            ]
            if limit is not None:
                items = items[:limit]
            return items

    def link_relation(
        self,
        *,
        tenant_id: str,
        relation_name: str,
        source_type: str,
        source_object_id: str,
        target_type: str,
        target_object_id: str,
        idempotency_key: str,
        actor_id: str = "",
        metadata: dict[str, Any] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> RelationRecord:
        if valid_from is not None and valid_to is not None and valid_to <= valid_from:
            raise RelationEndpointMismatch("valid_to must be after valid_from")
        schema = self._registry.get_relation_type(tenant_id, relation_name)
        if schema.source_type != source_type or schema.target_type != target_type:
            raise RelationEndpointMismatch(
                f"relation {relation_name} requires {schema.source_type}->{schema.target_type}"
            )
        metadata = validate_relation_properties(schema, metadata or {})
        request_payload = {
            "relation_name": relation_name,
            "source_type": source_type,
            "source_object_id": source_object_id,
            "target_type": target_type,
            "target_object_id": target_object_id,
            "metadata": metadata,
        }
        digest = payload_digest(request_payload)
        with self._lock:
            reused = self._idempotent_result(
                tenant_id=tenant_id,
                operation="relation.link",
                idempotency_key=idempotency_key,
                digest=digest,
            )
            if reused is not None:
                return RelationRecord.model_validate(reused)
            self._load_object(tenant_id, source_type, source_object_id)
            self._load_object(tenant_id, target_type, target_object_id)
            relevant = [
                item
                for item in self._relations.values()
                if item.tenant_id == tenant_id
                and item.relation_name == relation_name
                and item.status == "active"
            ]
            exact = next(
                (
                    item
                    for item in relevant
                    if item.source_object_id == source_object_id
                    and item.target_object_id == target_object_id
                ),
                None,
            )
            if exact is not None:
                stored = exact
            else:
                # Stage 12: cardinality conflicts only when the new Link's valid
                # interval overlaps an existing one for the same endpoint — the
                # same source/target may hold sequentially over disjoint windows.
                new_start = valid_from or datetime.now(UTC)

                def _overlaps(item: RelationRecord) -> bool:
                    return _intervals_overlap(
                        new_start,
                        valid_to,
                        item.valid_from or item.created_at,
                        item.valid_to,
                    )

                source_used = any(
                    item.source_object_id == source_object_id and _overlaps(item)
                    for item in relevant
                )
                target_used = any(
                    item.target_object_id == target_object_id and _overlaps(item)
                    for item in relevant
                )
                if (
                    schema.cardinality
                    in {RelationCardinality.ONE_TO_ONE, RelationCardinality.MANY_TO_ONE}
                    and source_used
                ):
                    raise RelationCardinalityViolation(
                        f"source already linked for {relation_name}"
                    )
                if (
                    schema.cardinality
                    in {RelationCardinality.ONE_TO_ONE, RelationCardinality.ONE_TO_MANY}
                    and target_used
                ):
                    raise RelationCardinalityViolation(
                        f"target already linked for {relation_name}"
                    )
                stored = RelationRecord(
                    tenant_id=tenant_id,
                    relation_id=f"rel_{uuid.uuid4().hex}",
                    relation_name=relation_name,
                    schema_version=schema.version,
                    source_type=source_type,
                    source_object_id=source_object_id,
                    target_type=target_type,
                    target_object_id=target_object_id,
                    metadata=metadata or {},
                    created_at=datetime.now(UTC),
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
                self._relations[(tenant_id, stored.relation_id)] = stored
                payload = {
                    "relation_id": stored.relation_id,
                    "relation_name": relation_name,
                    "source": f"{source_type}/{source_object_id}",
                    "target": f"{target_type}/{target_object_id}",
                }
                self._append_event_record(
                    tenant_id=tenant_id,
                    event_type="RelationLinked",
                    object_type=source_type,
                    object_id=source_object_id,
                    payload=payload,
                    actor_id=actor_id,
                )
                self._append_event_record(
                    tenant_id=tenant_id,
                    event_type="RelationLinked",
                    object_type=target_type,
                    object_id=target_object_id,
                    payload=payload,
                    actor_id=actor_id,
                )
            self._remember_idempotency(
                tenant_id=tenant_id,
                operation="relation.link",
                idempotency_key=idempotency_key,
                digest=digest,
                result=stored,
            )
            return stored.model_copy(deep=True)

    def unlink_relation(
        self,
        *,
        tenant_id: str,
        relation_name: str,
        source_type: str,
        source_object_id: str,
        target_type: str,
        target_object_id: str,
        idempotency_key: str,
        actor_id: str = "",
    ) -> RelationRecord:
        schema = self._registry.get_relation_type(tenant_id, relation_name)
        if schema.source_type != source_type or schema.target_type != target_type:
            raise RelationEndpointMismatch(
                f"relation {relation_name} requires "
                f"{schema.source_type}->{schema.target_type}"
            )
        request_payload = {
            "operation": "relation.unlink",
            "relation_name": relation_name,
            "source_type": source_type,
            "source_object_id": source_object_id,
            "target_type": target_type,
            "target_object_id": target_object_id,
        }
        digest = payload_digest(request_payload)
        with self._lock:
            reused = self._idempotent_result(
                tenant_id=tenant_id,
                operation="relation.unlink",
                idempotency_key=idempotency_key,
                digest=digest,
            )
            if reused is not None:
                return RelationRecord.model_validate(reused)
            active = next(
                (
                    item
                    for item in self._relations.values()
                    if item.tenant_id == tenant_id
                    and item.relation_name == relation_name
                    and item.source_object_id == source_object_id
                    and item.target_object_id == target_object_id
                    and item.status == "active"
                ),
                None,
            )
            if active is None:
                raise RelationNotFound(f"active relation not found: {relation_name}")
            stored = active.model_copy(
                update={
                    "status": "unlinked",
                    "deleted_at": datetime.now(UTC),
                },
                deep=True,
            )
            self._relations[(tenant_id, stored.relation_id)] = stored
            payload = {
                "relation_id": stored.relation_id,
                "relation_name": relation_name,
                "source": f"{source_type}/{source_object_id}",
                "target": f"{target_type}/{target_object_id}",
            }
            for object_type, object_id in (
                (source_type, source_object_id),
                (target_type, target_object_id),
            ):
                self._append_event_record(
                    tenant_id=tenant_id,
                    event_type="RelationUnlinked",
                    object_type=object_type,
                    object_id=object_id,
                    payload=payload,
                    actor_id=actor_id,
                )
            self._remember_idempotency(
                tenant_id=tenant_id,
                operation="relation.unlink",
                idempotency_key=idempotency_key,
                digest=digest,
                result=stored,
            )
            return stored.model_copy(deep=True)

    @staticmethod
    def _snapshot_property(record: ObjectRecord, name: str) -> str:
        value = record.properties.get(name)
        return value if type(value) is str else ""

    def delete_objects(
        self,
        *,
        tenant_id: str,
        type_name: str,
        object_ids: tuple[str, ...],
    ) -> int:
        """物理删除对象及其关系与事件账本行(滚动窗口 prune 专用)。

        与 upsert 的演进语义不同,这是为"已滚出观测窗口的快照对象"准备的
        回收路径——对象、以其为端点的关系、其事件行一并移除,防止滚动窗口
        类型无界累积。绝不用于治理性退役(那是 owner 迁移的职责)。
        """

        if isinstance(object_ids, str):
            raise ValueError("object_ids must be a sequence of ids")
        ids = tuple(item for item in object_ids if item)
        if not ids:
            return 0
        wanted = set(ids)
        deleted = 0
        with self._lock:
            deleted_at = datetime.now(UTC)
            for object_id in ids:
                key = self._object_key(tenant_id, type_name, object_id)
                doomed = self._objects.pop(key, None)
                if doomed is not None:
                    deleted += 1
                    # 硬删前落墓碑:增量拉取(updated_since)对删除的唯一
                    # 感知来源,与 Postgres 侧 0132 的 prune 函数同语义。
                    # 属性快照当场抄下——对象行马上就没了。
                    self._tombstones[(tenant_id, type_name, object_id)] = (
                        deleted_at,
                        self._snapshot_property(doomed, "court_id"),
                        self._snapshot_property(doomed, "start_at"),
                    )
                self._events.pop((tenant_id, type_name, object_id), None)
            relation_keys = [
                key
                for key, record in self._relations.items()
                if key[0] == tenant_id
                and (
                    (
                        record.source_type == type_name
                        and record.source_object_id in wanted
                    )
                    or (
                        record.target_type == type_name
                        and record.target_object_id in wanted
                    )
                )
            ]
            for key in relation_keys:
                del self._relations[key]
        return deleted

    def prune_object_update_events(
        self,
        *,
        tenant_id: str,
        object_types: tuple[str, ...],
        before: datetime,
    ) -> int:
        """删除高频类型早于 ``before`` 的 ObjectUpdated 事件行(账本 TTL)。

        只删 ObjectUpdated:对象 upsert 的幂等结果自包含(result_type=object,
        replay 从不回读事件行),这类事件没有任何重放依赖;ObjectCreated /
        RelationLinked / 领域事件保留——领域事件的幂等 replay 会按 event_id
        回读事件行,删了会 fail-closed,且时间线保留创建与连边骨架。
        """

        wanted = set(object_types)
        removed = 0
        with self._lock:
            for (tenant, type_name, _), events in self._events.items():
                if tenant != tenant_id or type_name not in wanted:
                    continue
                kept = [
                    event
                    for event in events
                    if event.event_type != "ObjectUpdated"
                    or event.created_at >= before
                ]
                removed += len(events) - len(kept)
                events[:] = kept
        return removed

    def prune_expired_inventory_objects(
        self, *, tenant_id: str, window_start: date
    ) -> dict[str, int]:
        """Reference implementation of the fixed-type 0072 inventory prune."""

        allowed = (
            "TennisSlotObservation",
            "TennisInventorySlot",
            "TennisOperatingDay",
        )
        # 墓碑滚动保留(与 0132 的 Postgres prune 函数同语义):断联超期的
        # 增量拉取方按契约必须全量重同步,读路径会显式报 watermark_expired。
        retention_floor = datetime.now(UTC) - timedelta(
            days=TOMBSTONE_RETENTION_DAYS
        )
        with self._lock:
            self._tombstones = {
                key: snapshot
                for key, snapshot in self._tombstones.items()
                if key[0] != tenant_id or snapshot[0] >= retention_floor
            }
        result: dict[str, int] = {}
        for type_name in allowed:
            expired: list[str] = []
            for record in self.list_objects(tenant_id, type_name=type_name):
                raw = record.properties.get("business_date")
                if type(raw) is not str:
                    continue
                try:
                    business_date = date.fromisoformat(raw)
                except ValueError:
                    continue
                if business_date < window_start:
                    expired.append(record.object_id)
            result[type_name] = self.delete_objects(
                tenant_id=tenant_id,
                type_name=type_name,
                object_ids=tuple(expired),
            )
        return result

    def prune_idempotency_records(
        self,
        *,
        tenant_id: str,
        key_prefixes: tuple[str, ...],
        before: datetime,
    ) -> int:
        """删除白名单前缀、早于 ``before`` 的幂等记录(重放窗口 TTL)。

        只删调用方点名的前缀:这些写路径重执行天然安全(内容 digest /周期键
        的 upsert 撞上无实变短路;link 撞上既有关系直接复用)。绝不全表删——
        例如 ``tennis-seed:*`` 的 replay 是「不覆盖运行期状态」的守卫,删了
        重跑会把封场打回种子可售态。
        """

        if not key_prefixes:
            return 0
        with self._lock:
            expired = [
                key
                for key, (_, _, recorded_at) in self._idempotency.items()
                if key[0] == tenant_id
                and recorded_at < before
                and any(key[2].startswith(prefix) for prefix in key_prefixes)
            ]
            for key in expired:
                del self._idempotency[key]
        return len(expired)

    def append_event(
        self,
        *,
        tenant_id: str,
        event_type: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor_id: str = "",
    ) -> EventRecord:
        schema = self._registry.get_event_type(tenant_id, event_type)
        if schema.object_type != object_type:
            raise RelationEndpointMismatch(
                f"event {event_type} requires object type {schema.object_type}"
            )
        normalized = validate_event_payload(schema, payload)
        request_payload = {
            "event_type": event_type,
            "object_type": object_type,
            "object_id": object_id,
            "payload": normalized,
        }
        digest = payload_digest(request_payload)
        with self._lock:
            reused = self._idempotent_result(
                tenant_id=tenant_id,
                operation="event.append",
                idempotency_key=idempotency_key,
                digest=digest,
            )
            if reused is not None:
                return EventRecord.model_validate(reused)
            self._load_object(tenant_id, object_type, object_id)
            stored = self._append_event_record(
                tenant_id=tenant_id,
                event_type=event_type,
                object_type=object_type,
                object_id=object_id,
                payload=normalized,
                actor_id=actor_id,
            )
            self._remember_idempotency(
                tenant_id=tenant_id,
                operation="event.append",
                idempotency_key=idempotency_key,
                digest=digest,
                result=stored,
            )
            return stored.model_copy(deep=True)

    def _append_event_record(
        self,
        *,
        tenant_id: str,
        event_type: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any],
        actor_id: str,
    ) -> EventRecord:
        key = self._object_key(tenant_id, object_type, object_id)
        events = self._events.setdefault(key, [])
        record = EventRecord(
            tenant_id=tenant_id,
            event_id=f"evt_{uuid.uuid4().hex}",
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            payload=payload,
            actor_id=actor_id,
            sequence=len(events) + 1,
            created_at=datetime.now(UTC),
        )
        events.append(record)
        return record
