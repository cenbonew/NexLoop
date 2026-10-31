# 运行单：作废测试账号的无期限承诺（I36，stage，2026-10-31）

适用分支 `l2/i36-commitment-cancel`（基于 main 80b1009b）。第一步无迁移：只新增一次性工具和总览卡片的读口径修正。

## 背景（诊断，调度员 10-31 在 DATA_HOST 只读执行 C1–C6）
- 未结束 155 条（conditional 47、open 108），全部 nx019-extractor/2，全部没有 due，全部是测试账号：customer-1/2/3（44/33/31，授权已撤销）、已擦除的旧 consumer（44，made_to=erasure:*）、s6c-2/3（2/1）。
- 卡片标签：no_active_plan 156、no_due_date 152、source_deleted 45（都是已擦除那位，来源是 consumer_erasure 删除的消息）、made_under_contact_restriction 1。
- 负责人决定（10-31，经调度员转达）：155 条全部作废（含 s6c-2/3），走 `Commitment.cancel`，负责人一条命令执行；擦除缺口另修（第二步迁移）。

## 变更
| 项 | 内容 |
|---|---|
| 工具 | `scripts/deploy/commitment_cancel.py`：负责人登录工作台，逐条调用 `cancel_commitment`（`Commitment.cancel:1`，human_owner、无审批）；不连 PostgreSQL；重跑重放；`--dry-run` 不登录；输出只有承诺 id、outcome id 和拒绝码 |
| 示例 | `deploy/examples/commitment-cancel.example.json` |
| 总览卡片 | `workbench_reads.py` overview：标签只计未结束承诺的异常（加上尚未登记的 claim 异常），和承诺页 open 组同一口径（F05/F09）。已结束承诺的异常仍保留在历史里（承诺页"全部"） |

## 规则
- SQL 只接受 conditional / open / in_progress 的取消，写 `cancel_requested` 事件；commitment keeper 下一轮按账本把状态推进为 `cancelled`（0111）。fulfilled / breached / cancelled 返回 409 `not_allowed_in_state`。
- operator / reviewer 没有 `Commitment.cancel` 授权（workbench-roles），返回 403。
- 取消不发送任何消息，不调用效果；已擦除 consumer 的承诺（made_to 已是擦除引用）同样可取消。

## 步骤（stage）
1. 部署本批（无迁移，只更新 backend 镜像）。
2. 在 DATA_HOST 用 ro() 取清单（**C7**，只读）：
   ```sql
   select r.commitment_id from runtime.nexloop_commitments r join ontology.objects o on o.tenant_id=r.tenant_id and o.world=r.world
     and o.type_name='Commitment' and o.object_id=r.commitment_id
   where r.tenant_id=:'T' and r.world='real' and o.properties->>'status' in ('conditional','open','in_progress')
   order by r.prepared_at,r.commitment_id;
   ```
   预期 155 行。
3. 按示例写私有配置（0600），`confirmation_ref` 用 `i36:20261031-1`，`reason` 固定为“I36 负责人 2026-10-31：测试账号对话提取的无期限承诺，统一作废”，`commitments` 放 C7 的 155 个 id。先 `--dry-run`，再正式执行：
   ```
   security find-generic-password -s <keychain service> -a <account> -w | \
     python3 scripts/deploy/commitment_cancel.py --origin https://<APP_HOST_NAME>:<PORT> --username-file F --password-stdin --config F
   ```
   预期 155 条全部为 `cancel_requested`，退出码 0；重跑应全部为 `replayed`。**`cancel_requested` 只表示取消请求已被受理**：SQL 只在账本里写 `cancel_requested` 事件，承诺此时仍是原状态；状态变成 `cancelled` 要等 commitment keeper 推进（第 4 步）。（L6 I36-a）
4. 等 commitment keeper 处理完 cancel_requested（它的下一轮），再跑一次 C7，应为 **0 行**；`status='cancelled'` 的应多出 155。
   - **其中 44 条属于已擦除的旧 consumer（made_to=erasure:*）**（L6 I36-b）。stage 上它们的登记行还在（那次擦除完成于 0306 之前），keeper 应能推进，C7 为 0。
   - 如果 keeper 推进不了这 44 条（C7 剩 44 行，keeper 对它们报 `port_unavailable` 重试），**就此停下**，等第二步迁移（0388，见 `docs/release/run-erasure-cancels-undated-commitments.md`）处理。**不要当作失败重跑工具，也不要手工改库**：重跑只会重放，取消请求已经记在账本里。
   - 其余 111 条（customer-1/2/3、s6c-2/3）必须推进为 cancelled；C7 里如果剩下非擦除 consumer 的行，按失败处理并报告。
5. 工作台总览的承诺卡片：未结束 0、已违约 0，四个标签（受限期间作出、没有进行中的计划、没有期限、来源已删除）都不再显示。**这一步依赖那 44 条也已 cancelled**：来源已删除 45 全部属于已擦除那位，如果第 4 步剩 44 行，卡片会剩未结束 44 和它们的标签，等第二步后再核对。

## 证据
C7 前后行数、工具两次输出（cancel_requested / replayed 计数）、卡片截图；不写令牌、DSN、主机地址或用户名。
