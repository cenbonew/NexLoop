# 执行单：NX-054 P3 verify 只读复核（P5 前提），以及 relay 写类 p95 的判定

P5 的前提之一是 P3 回填的 verify 平衡（`docs/design/relation-read-p4.md` §1 的 P5 行；`docs/implementation/NX-054-stage-publish.md` §10 第 1 步）。本单在 stage 上只读地重跑一次 verify，判读结果，并给出阳性对照。不写库，不新建凭据，也不改授权。

- **执行者**：调度员。**判定者**：L6。负责人不需要操作。
- **按 main 83ce06b9 核对过**。main 前进后，执行前重新核对 §1 列出的出处。

## 1. 入口、身份与是否写库（结论）

| 项 | 结论 | 出处 |
|---|---|---|
| verify 的实质 | `control.nexloop_relation_backfill_verify(tenant)`：对每个已发布、带源属性的派生（`control.nexloop_relation_derivations` 中 `source_property is not null`）和每个 world，输出 `a`、`b`、`dangling`、`erasing`、`extra`，以及全局的 `skipped_remaining` | 0349:445–483 |
| 是否写库 | **不写**。函数声明为 STABLE，只读。签名端口对 `verify` 在记录 intent **之前**就返回，所以不会写 `nexloop_relation_backfill_intents` | 0349:447；0350:37–39 |
| 端口入口 | `scripts/deploy/relation_backfill.py verify`，签名端口 `authz.nexloop_relation_backfill_command`（`eios:action:nexloop.relation.backfill:1`，数据库角色 `nexloop_domain_worker`）。端口要求服务主体 `relation_backfiller` 持有有效凭据和这条授权 | `nexloop_eios/relation_backfill.py`；0350:26–29 |
| `relation_backfiller` 现在还在吗 | **很可能已经撤销**。service-grants v21 让它退役（`retired_in: 21`），main 现在是 v24；stage 按 v22 及之后的版本 register 过，register 就会撤销它的授权。NX-054 §8 第 14 步（10-28）也执行了这次撤销 | `deploy/authorization/service-grants.v1.json` 中 `relation_backfiller` 一项；`run-nx057-remediation-stage.md` 第 174 行 |
| 本单用哪条路 | **只读 owner 会话直接调用同一个函数**（§3）。端口只是在签名鉴权之后调用这个函数，判读的数据完全相同。不需要恢复 `relation_backfiller`，不需要新凭据，也不需要负责人操作 | — |

如果坚持走端口（不推荐）：需要 service-grants 出一个新版本，重新授权一个回填主体，再 register、发凭据。这属于部署配置变更，按 ack-policy 处理，需要 L5 准备、调度员 ack；用完还要再出一版撤销。只为一次只读核对做这些不值得。

先确认授权现状（只读，判断上表第 4 行）：按 `run-nx057-remediation-stage.md` §2.1 的 `relation_backfiller` 授权查询执行，或者执行 OPERATIONS §8a.11a 的只读差异。结果为空，说明已撤销，与本单的做法一致。

## 2. 前提

| # | 前提 | 怎么核对 |
|---|---|---|
| V1 | 变量与 `run-nx057-remediation-stage.md` §0.2、§0.3 相同：`TENANT_ID`、`RUN`、`PG_CONTAINER`、`DB`、`ro` | `ro "select 1"` 输出两行：租户 ID 和 `1` |
| V2 | 没有正在运行的回填 run | `ro "select backfill_run,state from control.nexloop_relation_backfill_runs order by started_at"`，没有 `running` 或 `reverting`。如果有，就停下，先按 `run-nx054-s8-backfill.md` 用 `status` 查明情况 |
| V3 | 已发布目录的派生映射行数 | `ro "select count(*) filter (where source_property is not null),count(*) from control.nexloop_relation_derivations where tenant_id='$TENANT_ID'"`。stage 目录 v1 推迟了 `assessment_evidence`（NX-054 §8 第 4 步），所以已发布的是 **25** 个关系类型，不是 26 个；补发目录 v2 之后才是 26。记下这两个数 |

## 3. 步骤

### 3.1 dry-run（只读）

```
ro "select control.nexloop_relation_backfill_preflight('$TENANT_ID')->'blocking'"
ro "select r->>'relation',r->>'world',r->>'hex' from jsonb_array_elements(control.nexloop_relation_backfill_preflight('$TENANT_ID')->'report') r where (r->>'hex')::bigint>0 order by 1,2" | tail -n +2 > "$RUN/p3-preflight-hex.txt"
```

- 预期：`blocking` 为 `[]`。`report` 每项对应一组（关系，world），只列出源类型在该 world 有对象的组合。第二条只列出 `hex` > 0 的组合，也就是 verify 必须出现的那些行，留给 §3.3 对照。
- preflight 也是 STABLE，只读。

### 3.2 verify（只读）

```
ro "select control.nexloop_relation_backfill_verify('$TENANT_ID')::text" | tail -1 > "$RUN/p3-verify.json"
head -c 20 "$RUN/p3-verify.json"; echo
python3 -c "import ast,json,sys;t=open(sys.argv[1]).read().strip();v=json.loads(t) if t.startswith('{\"') else ast.literal_eval(t);bad=[r for r in v['relations'] if r['a']!=r['b'] or r['extra']!=0];print(json.dumps({'rows':len(v['relations']),'relations':len({r['relation'] for r in v['relations']}),'unbalanced':len(bad),'skipped_remaining':v['skipped_remaining'],'a_total':sum(r['a'] for r in v['relations']),'dangling':sum(r['dangling'] for r in v['relations']),'erasing':sum(r['erasing'] for r in v['relations'])}));[print(r['relation'],r['world'],r['a'],r['b'],r['extra']) for r in bad]" "$RUN/p3-verify.json"
```

- `ro` 输出的第一行是 `set_config` 返回的租户 ID，所以用 `tail -1` 取结果。
- **输出格式**（stage 2026-11-07 实际执行时发现）：某些执行环境下，不加 `::text` 时输出的是 Python 字面量格式（单引号，例如 `{'relations': [{'a': 119, ...`），`json.load` 会报 JSONDecodeError。所以 SQL 里转成 `::text` 再输出。`head -c 20` 那一行用来确认格式：开头是 `{"relations"` 就是标准 JSON；如果仍是单引号，解析会自动改用 `ast.literal_eval`，判读不受影响。证据记录时写明实际是哪一种。
- 对照用的一行（读 §3.1 的 `p3-preflight-hex.txt`；`-At` 输出的字段两侧可能带空格，例如 `goal_of | real`，所以按 `|` 切分后要 strip，否则每一组都会被判成缺失）：

  ```
  python3 -c "import ast,json,sys;t=open(sys.argv[2]).read().strip();v=json.loads(t) if t.startswith('{\"') else ast.literal_eval(t);seen={(r['relation'],r['world']) for r in v['relations']};want=[tuple(x.strip() for x in l.split('|')[:2]) for l in open(sys.argv[1]) if l.strip()];miss=[w for w in want if w not in seen];print(json.dumps({'preflight_hex_groups':len(want),'missing_in_verify':len(miss),'verify_only':len(seen-set(want))}));[print('missing',*w) for w in miss]" "$RUN/p3-preflight-hex.txt" "$RUN/p3-verify.json"
  ```

  `missing_in_verify` 必须为 0。
- 输出只有关系名、world 名和计数，没有对象 id 或属性值。`$RUN/p3-verify.json` 留作证据；取回管理机 `local-only/nx054/` 之后，在 DATA_HOST 上删除；`p3-preflight-hex.txt` 同样处理。

### 3.3 判读

- **通过条件**：
  - `unbalanced` 为 0，即每一行都满足 `a == b` 且 `extra == 0`；
  - `skipped_remaining` 为 0；
  - `relations` 中的关系都属于已发布的 25 个，没有 `assessment_evidence`。
- **行数不是固定的**：
  - 只列出有 hex 源值、且所在 world 不在清除中的（关系，world）组合；
  - 所以关系数可以少于 25。10-27 那次 stage verify 是 20 个，preflight 是 22 个（`run-nx054-s8-backfill.md` "verify 怎么看"）；
  - 对照 §3.1：第二条命令列出的每一组（关系，world），都必须在 verify 中出现（清除中的 world 除外）。缺了就是 verify 漏行，判失败。
  - 反过来不要求：verify 可以比 preflight 的 `hex` > 0 组多出一些行（上面输出的 `verify_only`），这些行在 preflight 中 `hex` 为 0。只要它们本身平衡，就不算异常。stage 2026-11-07 多出 `assignment_control` 一组，平衡。
- `dangling`（目标不存在）和 `erasing`（属于擦除中的 Consumer）照实记录，不计入 `a`，所以不影响平衡。
- **不平衡怎么处理**（按顺序，前几步都是只读）：
  1. 对不平衡的那一行：
     - `a > b`：说明有对象带着源属性却没有关系。可能是派生触发器漏了，也可能是回填之后有对象以 replica 方式写入。
     - `extra > 0`：说明有派生出的关系不再对应任何属性。可能是属性改了，而关系没有跟着改。
  2. 对照每日缺口核对：只看归属类关系的话，缺口应该同样不为 0。缺口为 0 而 verify 不平，说明问题出在非归属类关系（例如 `step_in_goal`）上。P5 只依赖归属类，但 §10 的条件要求全部平衡，所以仍然判不通过。
  3. 补齐：新开一个回填 run。这需要恢复回填主体，见 §1 最后一段，属于部署配置变更，交 L5 准备；或者由派生修复迁移处理。本单不做任何写入。
  4. P5 和 P4b 都停下，直到重新 verify 平衡。

### 3.4 阳性对照（证明 verify 确实会报不平衡，不是空跑通过）

只读环境里无法在 stage 上人为制造一个不平衡，所以用三层对照：

1. **非空**：`a_total` > 0，并且行数大于 0。否则说明 verify 什么也没读到，判**无数据**，不算通过。
2. **独立复算一行**（只读）：从 verify 输出中挑一行 `erasing` 为 0 的归属关系，优先选 `goal_of` 的 real 行。用不经过 verify 函数的 SQL 分别复算 `a` 和 `b`，两个数都必须与 verify 输出一致：

   ```
   ro "select count(*) from ontology.objects o join ontology.objects t on t.tenant_id=o.tenant_id and t.world=o.world and t.type_name='Consumer' and t.object_id=o.properties->>'consumer_id' where o.tenant_id='$TENANT_ID' and o.world='real' and o.type_name='Goal'"
   ro "select count(*) from ontology.objects o join ontology.objects t on t.tenant_id=o.tenant_id and t.world=o.world and t.type_name='Consumer' and t.object_id=o.properties->>'consumer_id' where o.tenant_id='$TENANT_ID' and o.world='real' and o.type_name='Goal' and exists(select 1 from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.relation_name='goal_of' and r.source_type='Goal' and r.source_object_id=o.object_id and r.target_object_id=t.object_id)"
   ```

   这证明 verify 读的是真实数据，而不是恒等地报出 `a == b`。
3. **不平衡确实会被报出**（不在 stage 上重做）：
   - stage 上已有记录：NX-054 §8 第 12 步的 revert 演练（10-28），`revert` 之后再 `verify`，`b` 回到回填前的值，`a == b` 不成立。引用当时的证据文件（`run-nx054-s8-backfill.md` 第 12 步的 jsonl）。
   - CI 上的对照：`tests/test_relation_backfill_port_pg.py`（回填前 `b == 0`、`a > 0`，回填后 `a == b`）和 `tests/test_relation_backfill_engine_pg.py`（同一个函数）。这两个测试覆盖的正是"不平衡时 verify 会报出来"。
   - 如果第 12 步没有留下证据，就记为"阳性对照只有 CI"，由 L6 判断是否足够。如果要在 stage 上重做，只能在一次性的 test world 里做（写入后回滚），那是写操作，需要另开一单并 ack，本单不包含。

## 4. 证据（记入 NX-054；不含 DSN 或对象 id）

- 执行者（调度员）和判定者（L6）分开写。
- V2、V3 的输出；§3.1 中 `hex` > 0 的（关系，world）组数；§3.2 的汇总行和不平衡行（如有）；§3.4 第 2 步的两个数和对应的 verify 行；§3.4 第 3 步引用的证据文件名，或"只有 CI"的说明。
- 执行时间取自 `date -u`。

## 5. 24 h relay 写类 p95 怎样判定

- **这是不是 P5 的前提**：不是。P5 的前提是：缺口连续 7 天为 0（已满足，起算日 10-28）、每日 verify 为 0（即本单）、全库擦除测试在只读关系模式下通过、缺口告警在线（`relation-read-p4.md` §1）。relay p95 是**目录发布和回填**那一步的门槛（NX-054 §2 第 3 点、§4 的门槛表）。
- **一次调用都没有时**：
  - 门槛表写明，发布前后两边写类 `calls` 都要不少于 200，否则记为"不可判定"，**不阻塞**（NX-054-stage-publish.md 第 117 行）；
  - relay 的 `relation_relay_p95`（0336）只统计有调用的分钟（`calls > 0`），没有调用时 `p95_ms_median` 为空，样本数为 0。
- **能不能用合成调用补测**：可以，而且做法在仓库里已有。
  - `relay_step` 样本由 message relay 在处理入站消息时写入（`message_relay_cli.py`，每分钟每步一条）；
  - 用 `scripts/acceptance/synthetic_customer.py` 驱动一个 simulated_agent 合成顾客发言，就会经过真实的 relay，产生 `goal_create`、`step_create`、`assignment_create` 等步骤的样本（NX-054 §2 第 2 点用的就是这个做法，需要负责人工作台会话里的合成顾客凭据）；
  - 要凑够 200 次调用，需要几十轮对话，按 §2 的节奏分散到 24 小时内，不能集中在一个小时里。
  - 读法与 `run-nx054-s8-owner-steps.md` §3 相同：`relation_derive_timing.py --dsn-file /run/metrics-dsn relay --since <ISO> --until <ISO>`。
- **建议**：P5 不等这一项。如果 L6 仍要一个数，就按上面的做法用合成顾客补测，记为"合成调用"，与真实流量分开写。
