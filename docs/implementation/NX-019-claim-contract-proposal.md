# NX-019 契约提案：`claim.schema.json`（草案，未进入 packages/contracts）

状态：**提案**。由调度员/负责人决定是否纳入 `packages/contracts/`。本草案描述 NX-019 提取器产出、`authz.nexloop_read_conversation_claims` 返回的单条 Claim，供 NX-020（四层匹配）、NX-045（候选暂存）消费。与 `ontology-mutation.schema.json` 的 `epistemic_kind` 枚举一致。

## 设计要点

- Claim 是候选知识，不是正式对象；没有 `revision`，不可原地修改。状态推进（`resolution_state`）由后续受治理流程负责。
- 提取器产出时 `epistemic_kind` 永远不是 `verified_fact`；该值保留在枚举中只为与 ontology-mutation 对齐，供验证通道（签名商业事件、Action receipt）使用。草案用 `if/then` 约束：`extractor_version` 存在时禁止 `verified_fact`。
- `source` 为 `null` 仅允许 `hypothesis`，此时 `derived_from` 必须非空；显性 Claim 必须有 `source` 且 `derived_from` 为空。
- 时间由服务端解析，`valid_time.status` 区分 `resolved / ambiguous / unresolved / absent`；含糊时间只给窗口，不给编造的时刻。
- `subject.ref` 只由服务端会话推导（consumer 的对象 id）；模型给出的姓名不选择身份。

## Schema 草案

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://nexloop.dev/contracts/claim.schema.json",
  "title": "NexLoop Claim",
  "type": "object",
  "additionalProperties": false,
  "required": ["claim_id","tenant_id","world_id","conversation_id","consumer_id","topic_key","subject","predicate","value",
               "speaker","polarity","modality","condition","time_expression","valid_time","source","derived_from","corrects_claim_id",
               "extractor_version","confidence","epistemic_kind","resolution_state","correlation_key","guard_flags","recorded_at"],
  "properties": {
    "claim_id": {"$ref": "#/$defs/digest"},
    "tenant_id": {"type": "string", "minLength": 1},
    "world_id": {"type": "string", "enum": ["real","simulation","shadow","test"]},
    "conversation_id": {"$ref": "#/$defs/digest"},
    "consumer_id": {"$ref": "#/$defs/digest"},
    "topic_key": {"$ref": "#/$defs/digest"},
    "subject": {
      "type": "object", "additionalProperties": false, "required": ["kind","ref","text"],
      "properties": {
        "kind": {"enum": ["consumer","enterprise","entity"]},
        "ref": {"type": "string", "description": "consumer: server-derived consumer object id; otherwise empty"},
        "text": {"type": "string", "maxLength": 200, "description": "referent words present in the topic window, or empty"}
      },
      "if": {"properties": {"kind": {"const": "consumer"}}}, "then": {"properties": {"ref": {"$ref": "#/$defs/digest"}}},
      "else": {"properties": {"ref": {"const": ""}}}
    },
    "predicate": {"type": "string", "minLength": 1, "maxLength": 120},
    "value": {
      "type": "object", "additionalProperties": false, "required": ["type","value"],
      "properties": {"type": {"enum": ["string","number","boolean","money","none"]}, "value": {}},
      "allOf": [
        {"if": {"properties": {"type": {"const": "string"}}}, "then": {"properties": {"value": {"type": "string", "minLength": 1, "maxLength": 2000}}}},
        {"if": {"properties": {"type": {"const": "number"}}}, "then": {"properties": {"value": {"type": "number"}}}},
        {"if": {"properties": {"type": {"const": "boolean"}}}, "then": {"properties": {"value": {"type": "boolean"}}}},
        {"if": {"properties": {"type": {"const": "money"}}}, "then": {"properties": {"value": {"type": "object", "additionalProperties": false,
          "required": ["amount","currency"], "properties": {"amount": {"type": "number"}, "currency": {"type": "string", "pattern": "^[A-Z]{3}$"}}}}}},
        {"if": {"properties": {"type": {"const": "none"}}}, "then": {"properties": {"value": {"type": "null"}}}}
      ]
    },
    "speaker": {"enum": ["consumer","agent"]},
    "polarity": {"enum": ["affirmed","negated"]},
    "modality": {"enum": ["asserted","conditional","tentative","requested"]},
    "condition": {"type": "string", "maxLength": 400, "description": "verbatim condition clause; required when modality=conditional"},
    "time_expression": {"type": "string", "maxLength": 80},
    "valid_time": {
      "type": "object", "additionalProperties": false,
      "required": ["expression","kind","status","start","end","timezone","anchor"],
      "properties": {
        "expression": {"type": "string"},
        "kind": {"enum": ["none","point","interval","deadline","unparsed","past_reference"]},
        "status": {"enum": ["absent","resolved","ambiguous","unresolved"]},
        "granularity": {"enum": ["minute","hour","day","part_of_day","am_pm_unspecified","period","month"]},
        "start": {"type": ["string","null"], "format": "date-time"},
        "end": {"type": ["string","null"], "format": "date-time"},
        "latest_bound_window": {"type": "array", "items": {"type": "string", "format": "date-time"}, "minItems": 2, "maxItems": 2},
        "timezone": {"type": "string", "description": "IANA tenant timezone"},
        "anchor": {"type": "string", "format": "date-time", "description": "server acceptance time of the cited Message"}
      }
    },
    "source": {
      "oneOf": [
        {"type": "null"},
        {"type": "object", "additionalProperties": false,
         "required": ["message_id","sequence","span_start","span_end","content_hash","quote"],
         "properties": {
           "message_id": {"$ref": "#/$defs/digest"},
           "sequence": {"type": "integer", "minimum": 1},
           "span_start": {"type": "integer", "minimum": 0, "description": "Unicode code point offset"},
           "span_end": {"type": "integer", "minimum": 1},
           "content_hash": {"$ref": "#/$defs/digest", "description": "sha256 of the full Message body (UTF-8)"},
           "quote": {"type": "string", "minLength": 1}
         }}
      ]
    },
    "derived_from": {"type": "array", "items": {"$ref": "#/$defs/digest"}, "uniqueItems": true},
    "corrects_claim_id": {"oneOf": [{"type": "null"}, {"$ref": "#/$defs/digest"}]},
    "extractor_version": {"type": "string", "minLength": 1, "maxLength": 80},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "extractor self-estimate, not calibrated"},
    "epistemic_kind": {"enum": ["user_statement","verified_fact","preference","constraint","intent","need_problem","commitment","hypothesis","correction"]},
    "resolution_state": {"enum": ["unresolved","hypothesis_only","needs_resolution","awaiting_definition","rejected_definition","resolved","superseded"]},
    "correlation_key": {"$ref": "#/$defs/digest", "description": "same consumer+kind+predicate+normalized value+polarity; links repeats, never deduplicates"},
    "guard_flags": {"type": "array", "items": {"type": "string"}, "uniqueItems": true},
    "recorded_at": {"type": "string", "format": "date-time"}
  },
  "allOf": [
    {"if": {"required": ["extractor_version"]}, "then": {"properties": {"epistemic_kind": {"not": {"const": "verified_fact"}}}}},
    {"if": {"properties": {"epistemic_kind": {"const": "hypothesis"}}},
     "then": {"properties": {"resolution_state": {"enum": ["hypothesis_only","superseded"]}},
              "anyOf": [{"properties": {"source": {"type": "object"}}}, {"properties": {"derived_from": {"minItems": 1}}}]},
     "else": {"properties": {"source": {"type": "object"}, "derived_from": {"maxItems": 0}, "resolution_state": {"not": {"const": "hypothesis_only"}}}}},
    {"if": {"properties": {"modality": {"const": "conditional"}}}, "then": {"properties": {"condition": {"minLength": 1}}}},
    {"if": {"properties": {"corrects_claim_id": {"type": "string"}}}, "then": {"properties": {"epistemic_kind": {"const": "correction"}}}},
    {"if": {"properties": {"speaker": {"const": "agent"}}}, "then": {"properties": {"epistemic_kind": {"enum": ["commitment","hypothesis"]}}}},
    {"if": {"properties": {"speaker": {"const": "consumer"}}}, "then": {"properties": {"epistemic_kind": {"not": {"const": "commitment"}}}}}
  ],
  "$defs": {"digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}
}
```

说明：草案的 `source` 对象在存储中对应 `source_message_id / source_sequence / span_start / span_end / source_content_hash / quote` 六列；`subject` 对应 `subject_kind / subject_ref / subject_text`。若纳入契约，读取函数需改为输出该嵌套形状（当前返回扁平列）。`agent` 说话人在当前存储路径上尚不会出现（Agent 外发消息未持久化为 Message，待负责人决定）。

## 示例（合成）

条件续费意向：

```json
{
  "claim_id": "3f1c0d0f6b8a4f5e9a1c2b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6071",
  "tenant_id": "synthetic-a", "world_id": "real",
  "conversation_id": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "consumer_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "topic_key": "8e0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8",
  "subject": {"kind": "consumer", "ref": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "text": ""},
  "predicate": "续费意向", "value": {"type": "boolean", "value": true},
  "speaker": "consumer", "polarity": "affirmed", "modality": "conditional", "condition": "解决后",
  "time_expression": "",
  "valid_time": {"expression": "", "kind": "none", "status": "absent", "start": null, "end": null,
                 "timezone": "Asia/Shanghai", "anchor": "2026-10-08T02:00:00+00:00"},
  "source": {"message_id": "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809",
             "sequence": 1, "span_start": 19, "span_end": 28,
             "content_hash": "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0",
             "quote": "解决后我再考虑续费"},
  "derived_from": [], "corrects_claim_id": null,
  "extractor_version": "nx019-extractor/2", "confidence": 0.8,
  "epistemic_kind": "intent", "resolution_state": "unresolved",
  "correlation_key": "5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c",
  "guard_flags": ["modality_forced_conditional"], "recorded_at": "2026-10-08T02:00:05+00:00"
}
```

企业承诺（含糊截止）只示 `valid_time`：

```json
{"expression": "明天下午前", "kind": "deadline", "status": "ambiguous", "granularity": "part_of_day",
 "start": "2026-10-08T10:01:00+08:00", "end": "2026-10-09T18:00:00+08:00",
 "latest_bound_window": ["2026-10-09T12:00:00+08:00", "2026-10-09T18:00:00+08:00"],
 "timezone": "Asia/Shanghai", "anchor": "2026-10-08T02:01:00+00:00"}
```

隐性推断（无自身原文）：`"epistemic_kind": "hypothesis", "resolution_state": "hypothesis_only", "source": null, "derived_from": ["<显性claim_id>"], "confidence": 0.6`。

更正：`"epistemic_kind": "correction", "corrects_claim_id": "<被更正claim_id>"`，原 Claim 保留不删除。
