# 操作单：标记测试人设并删除合成顾客（迁移 0217，临时编号）

用途：AT-050 第二次恢复演练需要在 stage 上删除一个合成顾客，并在恢复后的重放里看到这次删除。负责人用“私有配置文件 + 一条命令”完成，口令从 Keychain 经管道传入，不使用浏览器控制台。

依据：ADR-034（测试人设标记，这里提前实施其最小一步）；NX-029 删除流程；调度员 2026-10-18 选定方案 A。

## 机制

- **persona**：每个消费者有一个人设值 `real`、`simulated_agent` 或 `synthetic`。没有记录即为 `real`。只能经负责人 Action `nexloop.consumer.set_persona:1` 修改，每次修改有修订号和一条只追加的事件（原值、新值、执行人、请求、理由）。改回 `real` 即撤销。
- **合成删除**：负责人 Action `nexloop.consumer.erase_synthetic:1`。SQL 先读 persona，为 `real` 时拒绝（工作台返回 409，`details.reason` 为 `persona_real`）；否则调用原有的 NX-029 删除处理函数，阻断立即生效，清除由保留期 keeper 完成。
- **不变的部分**：真实顾客的删除请求仍走原有的 `nexloop.consumer.erase:1`，不受 persona 影响。恢复后的删除重放（0135）直接调用原处理函数，不再检查 persona。
- **只读预查**：`GET /api/v1/workbench/erasure-precheck/<consumer_id>`，仅负责人可用。返回 persona 及其修订号、删除请求的状态（不含理由），以及仍保留的数量：会话、消息、仍带正文的消息、Claim、值尚未清除的 Claim、数据导出。不返回姓名或正文。

## 顺序

| 步 | 内容 | 由谁 | 类别 |
|---|---|---|---|
| 1 | 部署含 0217 的版本；`stage_secrets publish` 发布 business-actions（新增两个 Action）；按 workbench-roles 清单 v14 应用成员与授权 | 调度员 | B 类 |
| 2 | `mark`：从私有配方补标测试账号为 `simulated_agent`（先 `--dry-run`） | 负责人 | — |
| 3 | AT-050：记录恢复点 | 调度员 | — |
| 4 | `erase --dry-run`：核对 persona 与将删除的数量 | 负责人 | — |
| 5 | `erase`：执行删除 | 负责人 | — |
| 6 | keeper 跑完后再次 `erase --dry-run`，作为事后核对 | 负责人 | — |
| 7 | AT-050：恢复到第 3 步的恢复点，执行删除重放；再做一次第 6 步，应看到删除已重放 | 调度员、负责人 | — |

第 2 步要放在恢复点之前：恢复后的数据库里仍保留 persona 标记，与恢复前一致。重放本身不依赖 persona。

## 命令

```bash
security find-generic-password -s <keychain 服务名> -a <账号> -w | \
  python3 scripts/deploy/erase_synthetic_consumer.py mark --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> \
  --username-file <用户名文件> --password-stdin --config <persona 配置> --recipe <webchat-consumers 私有配方> --dry-run
```

```bash
security find-generic-password -s <keychain 服务名> -a <账号> -w | \
  python3 scripts/deploy/erase_synthetic_consumer.py erase --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> \
  --username-file <用户名文件> --password-stdin --config <删除配置> --dry-run
```

去掉 `--dry-run` 即正式执行。配置示例见 `deploy/examples/consumer-persona.example.json` 与 `deploy/examples/erase-synthetic-consumer.example.json`；配置文件和用户名文件须为 0600，并且属于执行者本人。

- `mark` 只接受私有配方中 persona 为 `simulated_agent` 且未撤销的登录主体；写入的 persona 取自配方。
- `erase` 对预查显示为 `real` 的顾客不发送任何请求；服务端同样会拒绝。
- 同一配置可以重跑：幂等键由顾客和 `confirmation_ref` 派生，重跑即重放。

## 输出与退出码

输出一个 JSON 对象，只含 ID、persona、状态和数量，不含姓名、用户名、口令、cookie、CSRF 令牌或理由。

| 退出码 | 含义 |
|---|---|
| 0 | 全部完成；或 dry-run 中每个顾客都可以执行 |
| 1 | 有顾客被拒绝或不符合条件 |
| 2 | 输入被拒 |

`result` 取值：

| 子命令 | 取值 |
|---|---|
| `mark` | `would_mark`、`marked`、`already_marked`、`unchanged`、`not_in_recipe`、`refused` |
| `erase` | `would_erase`、`erasing`、`replayed`、`already_erasing`、`already_erased`、`persona_real`、`consumer_unavailable`、`refused` |

## 事后核对

删除完成后，预查应满足以下全部条件：

- `erasure.state` 为 `completed`；
- `held.messages_with_body` 为 0；
- `held.claims_not_erased` 为 0；
- `held.data_exports` 为 0；
- `consumer_object` 为 false。

`persona` 标记仍然保留，其中不含顾客数据。

数据导出文件如果已经下载到 Fileshare 或本机，不在数据库可见范围内，需要按导出记录另行清理。

## 限制

- 配置里 `consumer_id` 与配方中登录主体的对应关系由负责人自己保证，服务端不核验。服务端只保证：未标记或标为 `real` 的顾客不能经合成删除路径删除。
- ADR-034 的其余部分（Consumer 对象属性、渠道绑定检查、报表与导出按 persona 排除）尚未实施。
