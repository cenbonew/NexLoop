# NX-041 S6b：模拟组 Host 的效应工具接到替身提交

> **D10 状态（2026-10-21 更新）**：D10 已定案（负责人 2026-10-21 放宽为三凭据：api、worker、agent，每个只绑定这一个模拟世界，删除世界时一并吊销），实现中。下文中“D10 待定”“等 D10 定下来”“D10 定案时”等说法，均指这一定案。

依据：`NX-041-design.md` §3.2、§6。起点 959beadb（S6）。集成时（`int/nx041-s1s5`）按调度员定案丢弃了临时迁移 `0263_nx041_sim_host_tools.sql`：S4 修复（L3）由服务端取世界时间，payload 中出现 `sim_clock` 即拒绝；guard 本来就不传 `sim_clock`，所以本切片不需要迁移。下文提到的 0263 指被丢弃的那份。

## 实现

| 内容 | 位置 |
| --- | --- |
| **Host（simulation 组）**：运行配置 `world_group: simulation` 时，`RuntimeEffectClient` 给出的效应工具名称和参数都与 real 组相同（`nexloop.service.request` / `.find`、`nexloop.incentive.issue`、`nexloop.outreach.send`），但调用的是 guard 的 `/internal/v1/runtime/stand-ins/submit` 或 `/find`，不再走 effects、incentives、outreach 路由。企业交付没有替身，所以不提供。`nexloop.plan.outcome` 不是效应，保持不变 | `runtime-effect-tools.ts`、`runtime-host.ts` |
| **呈现为模拟输出**：Agent 读到的结果是 `{simulated: true, output_kind: "simulated_output", request_id, status, stand_in_version, note}`。note 写明这是替身在模拟世界里的记录：没有发送、发放或送达，也不是关于任何真实客户的事实。不包含摘要或内部字段。Host 只接受 `simulated=true`、`output_kind=simulated_output`、world 和 Run 都与本 Run 一致的回执，其他一律拒绝 | 同上 |
| **real 组**：工具、描述和路由一字未改。`standIn` 在 real 组直接拒绝 | 同上 |
| **guard**：新增 stand-ins/submit 和 stand-ins/find 两条路由，返回固定错误码：invalid_effect_request（400）、effect_intent_unavailable（403）、intent_payload_conflict（409）。`_FreshGuard.submit_run_stand_in` 的处理顺序：<br>1. 先检查 world：guard 的 world 必须是 sim/shadow，且等于命令里的 world，否则在授权和查库之前就拒绝；<br>2. 重新授权当前活跃的 activation（operation 为 tool）；<br>3. 用 guard 自己的模拟世界凭据调用 S4 的 `authz.nexloop_sim_effect_submit`。<br>意图 id 由（Run、Action、业务键）推导，重试得到同一个意图，同一个键换了内容会冲突；业务键在外联是 purpose，在优惠是 offering。find 只读本 Run 回复工具推导出的那个意图 | `runtime_control.py`、`runtime_worker.py`、`stand_in_tools.py` |
| **不传 sim_clock**：guard 从不传 sim_clock。0263 把 S4 的 submit 原地替换为 sim_clock 可选（签名和授权不变），不传时由服务端取世界时间（`runtime.nexloop_sim_world_time`：S5 集成后读世界时钟）。L3 的 `side/nx041-s4-fixes` 也在改这个入口（服务端取值并加盐），集成时以 L3 的版本为准，0263 可以合并或去掉 | 0263 |

## 模拟组签名密钥的登记步骤（部署说明，不改 deploy/）

模拟组使用单独的签名密钥，在 compose 中对应 `NEXLOOP_SIM_SIGNING_KEY_ID` 和 secret `sim_signing_key`。登记沿用 NX-032 的受信配置入口：`authz.nexloop_authority_signing_keys` 只能经 `control.nexloop_configure_manifest` 写入，同一个 key_id 写入不同内容时会被拒绝。

1. 在管理机的私有临时目录（0700）生成 32 字节随机密钥，写出两份（NX-032 F3）：原始字节 `sim-signing.key`（服务读取）和十六进制 `sim-signing.key.hex`（受信配置读取），权限都是 0600。
2. 选一个与 real 不同的 key_id，例如 `sim-<日期>`，不能含冒号，满足 `[A-Za-z0-9][A-Za-z0-9._-]{0,159}`。
3. 用模拟世界的受信配置清单登记密钥。清单包含这个世界的三类服务主体，它们的凭据都只绑定该 world（D10 已定案，负责人 2026-10-21 放宽为三凭据，实现中）：
   `python -m nexloop_eios.trusted_configuration --manifest <模拟世界清单> --apply --database-url-file <配置者 DSN 文件> --signing-key-file sim-signing.key.hex --signing-key-id <SIM_KEY_ID> --service-secrets-file <reference→令牌>`
4. 把 `sim-signing.key` 放入 `NEXLOOP_SIM_SECRET_DIR/sim_signing_key`，在私有 env 中设置 `NEXLOOP_SIM_SIGNING_KEY_ID=<SIM_KEY_ID>`。
5. 删除管理机上的临时目录。不要复用 real 的 `signing.key`；real 组的 key_id 不出现在模拟组的任何文件里。

## 测试

- Host（Node 24，重建 dist 后串行）`test/nx041-sim-host-tools.test.ts`：
  - 模拟组的回复、优惠、外联工具只调用 stand-ins 路由，参数原样传递，不带 sim_clock；
  - 结果以模拟输出呈现；
  - 不提供企业交付；
  - 伪造或越界的回执（未标模拟、事实类、其他 world、多余字段）被拒绝；
  - real 组（默认，或显式 false）的工具和路由不变，也不会调用替身。
- PG `tests/test_nx041_sim_host_tools_pg.py`：
  - 载荷：稳定的意图 id，不含时钟，非法参数被拒绝；
  - guard 路由：参数原样传递，错误映射为固定码；
  - 模拟世界 guard：只写替身意图，real 效应表行数不变；重试是同一意图，改内容冲突；提交时间取服务端世界时间；find 只能读本 Run 的意图，替身执行器跑过之后才能读到；
  - real guard 和 world 不一致的命令：在授权和查库之前就被拒绝。

## 限制

- activation 的重新授权沿用现有 guard 函数（已有测试覆盖）。本次测试只验证它以 operation tool 被调用，没有在 sim 世界跑真实的 activation 链。完整 Run 的端到端仍等 D10 的决定。
- `nexloop.service.request` 的替身固定使用 http provider 类型。模拟 Host 不知道部署里的回复走 HTTP 还是本地 JSON，两种替身的语义相同。
