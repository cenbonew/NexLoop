# 运行单：K4c context-pack v7（计划 Run 带员工回答，stage）

- 适用分支：`l2/k4c-context-v7`，基于第 89 批 57b95cd1（K4a+K4b，迁移 0397）。迁移 0398（临时号）。
- 内容：v7 = v6 加一个 evidence 子区 `human_answers`。每个问题只取最新修订；问题被撤回、到期、被取代或已擦除时不出现。条目是员工的陈述（user_statement），永远不是事实。**只给计划 Run**（负责人 Q2）。
- 开关：
  - 计划重评估服务的环境变量 `NEXLOOP_CONTEXT_V7`，取值 `plan` 打开，默认 off；
  - **并且**策略声明 v7（新的策略版本，`protocol: nexloop.context-pack.v7`）（Q1）。
  - 任何一个不满足，生成的包就是 v6，与今天逐字节相同。
- 执行者：调度员。判定者：L6。证据只记计数、id 前缀和协议串，不展示回答内容。

## 启用顺序（同批启用，按此顺序）

1. **部署本批并迁移 0398**：迁移前先备份。合入后行为不变，两个开关都还关着。
2. **Host 先改成能读 v7**：私有的 `agent-host.runtime-config.json` 中，`context_input_protocol` 由 v6 改为 `nexloop.context-pack.v7`，然后重启 agent-host。v7 配置的 Host 同时接受 v6 和 v7 的包，消息 Run 不受影响。
   - 核对：消息 Run 照常回复；计划 Run 照常运行（这时仍是 v6）。
3. **发布声明 v7 的策略版本**：受治理的策略发布，在当前计划策略的基础上新增一个版本，只把 `protocol` 改为 v7，其余不变。
4. **打开 v7（先让 Run 能读到回答）**：私有 compose env 写 `NEXLOOP_CONTEXT_V7=plan`，然后 `up -d --no-deps plan-reevaluator`。
   - 核对：`ro "select protocol, count(*) from runtime.nexloop_context_packs where tenant_id='<TENANT_ID>' and created_at>now()-interval '1 hour' group by 1"`，在下一次计划 Run 之后出现 v7。
5. **再打开提问**：Host 的 planQuestion 开关（K4a/b）。这一步**必须在第 4 步之后**：否则会有一段时间问了问题，答案却进不了下一次计划 Run。
   - **注意：planQuestion 目前没有任何配置可以打开**（f656e6ce：默认关，没有任何 Host 配置会设置它）。启用 K4 需要另做一个受控的配置入口（代码改动），并另行审查；在那之前第 5 步无法执行，第 1–4 步可以先做，没有问题也就没有回答，v7 包与 v6 只差 schema_version。
6. 与之同批：K4c 读接口（工作台的问题列表）、L9 首页的问题卡片、L6 复审。
7. **只读核对**：
   - 回答一个问题后，下一次该客户的计划 Run 的包里，`evidence` 中有对应的 `human_answers` 条目：
     `ro "select count(*) from runtime.nexloop_context_sources s join runtime.nexloop_context_packs p using(tenant_id,world,context_id) where p.protocol='nexloop.context-pack.v7' and s.ref like 'nexloop:human_answer:%'"`；
   - 消息 Run 的包始终是 v6。

## 回退（关两个开关）

1. 关提问：Host 的 planQuestion 关闭。
2. 关 v7：私有 compose env 删掉 `NEXLOOP_CONTEXT_V7` 或改为 off，然后重建 plan-reevaluator。之后的计划 Run 回到 v6。
3. Host 的 `context_input_protocol` 可以保持 v7（它也读 v6），也可以改回 v6。
4. 已有的问题和回答留在表里，按 7 天到期或计划关闭时清理。策略版本不撤回：v6 包也可以引用声明 v7 的策略。

## 已知取值

- 回答配额（W3）：最多 8 条、最多 1500 个估算 token（Q6）。stage 上统计 `section_quota` 的 omitted 次数后再议是否调整：
  `ro "select count(*) from runtime.nexloop_context_sources s join runtime.nexloop_context_packs p using(tenant_id,world,context_id) where p.protocol='nexloop.context-pack.v7' and s.ref like 'nexloop:human_answer:%' and s.status='omitted' and s.omitted_reason='section_quota'"`
- 估算：按当前估算口径，每条回答连同信封大约 250–400 token，所以实际通常是 token 上限先起作用，一次保留 4–6 条。被裁的记为 omitted（`section_quota`，只用 v6 的原因值）。是否调整，等 stage 观察后再定。
