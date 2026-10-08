"""Mandatory access markings: 唯一一处"这一行调用方看不看得见"的判据。

0021/0022 把 marking 建成了行级强制访问控制(``ontology.objects.markings``
是 ``text[]``,空数组=未标记=人人可见),并在 0022 的注释里写明
「marking enforcement is layered in the parameterized read SQL」。

单点读(``object_detail``)与子图(``object_subgraph``)照做了;**列举路径没有**
—— ``read_object_page`` / 变更流 / ``ObjectListCapability`` 的不分页分支
一路直通 ``store.list_objects``,而 store 层只把 markings 当字段选出来。
结果是:只要持 ``ontology.object.read``,``GET /api/ontology/objects
?type_name=X`` 就能把带 marking 的行**连属性**一起列出来,单点读那道闸在
这条路上形同虚设(2026-08-18 实证)。

判据只在这里定义一次,两条路共用同一个函数 —— 各写各的 ``issubset`` 是
第二种语义的开始,而两种语义就意味着两者迟早不一致。
"""

from __future__ import annotations

from typing import Iterable


def visible_to(
    record_markings: Iterable[str] | None,
    caller_markings: frozenset[str],
) -> bool:
    """行的 marking 全部被调用方的 clearance 覆盖时可见。

    空 markings(未标记)对任何人可见,包括 clearance 为空的调用方 —— 这是
    0022 的既有语义,不在本次修复里改动。
    """

    return set(record_markings or ()).issubset(caller_markings)


def visible_records(records, caller_markings: frozenset[str]) -> list:
    """按同一判据过滤一批记录(顺序不变)。"""

    return [
        record
        for record in records
        if visible_to(getattr(record, "markings", ()), caller_markings)
    ]
