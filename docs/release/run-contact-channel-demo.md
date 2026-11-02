# 联系偏好演示客户：建立、使用、退役（S-CLAIM §8.4）

用途：演示“客户在对话里说出渠道偏好 → 提取 → 0228 偏好链路 → 工作台客户详情‘这位客户自己说过的沟通偏好’”。
stage 上现有的测试客户都是 `simulated_agent` 人设，重抽和外联闸门都会跳过它们，所以演示用一个 **persona 为 real 的合成客户**（L6 2026-11-02 同意）。
这种客户能通过外联闸门（0231），可能产生真实外发，因此本单把它的所有联系路径限定在 WebChat 和参考端点，并规定到期和退役。

只写角色和占位符，不写主机名、地址、口令。

## 0. 约束（建立前确认，任何一条不满足就不建）

| 约束 | 做法 | 依据 |
|---|---|---|
| 不登记真实电话或邮箱 | 身份账号的 `verified_email` 用保留域名 `.invalid` 下的合成地址（RFC 2606，无法投递），不填电话。Consumer 的 `external_id_refs`、`contact_preferences` 只能是空数组 | `consumer_profile.py` 的创建 schema（`maxItems: 0`） |
| 主动外发只走 WebChat | 0238 的外发记录 `channel` 只允许 `native.webchat`，没有电话、短信、邮件通道 | `0238_outreach_send.sql` |
| 企业交付只到参考端点 | 租户的企业 provider 只配置参考端点（`deploy/stage/reference-endpoint`），不配置其他外部端点 | NX-037 租户装配 |
| 有到期日 | 这个客户自己的 EffectControl：`budget_units` 取 1，`valid_until` 不超过建立后 7 天 | run-entry-tickets §2 第 3 步（S81 测试客户同一做法） |
| 标记 | 配方 `marker` 写 `contact-channel-demo`，用户名 `contact-demo-consumer` | — |

## 1. 建立（受治理路径，做法同 run-entry-tickets §2，但不标记 persona）

1. 身份操作员按 identity-human 配方建号（用户名 `contact-demo-consumer`，`verified_email` 为 `.invalid` 合成地址）。口令用 python secrets 生成并检查长度，直接存入 Keychain（`nexloop/…/contact-demo-consumer`），经管道装到主机，不回显、不落文件。
2. `python -m nexloop_eios.webchat_consumers --apply`：配方里 `persona` 仍写 `simulated_agent`（这个字段只是试运行标记，v0.1 只允许这个值；数据库里的 persona 不由它决定）。
3. `business_setup`（`business-recipe.example.json`，`human_principal_id` 填新 principal）：建 Consumer、它自己的 EffectControl（`budget_units` 1，`valid_until` ≤ 7 天）、ConsumerOwnership。
4. **不执行** `erase_synthetic_consumer.py mark`：数据库里没有 persona 行即为 `real`（ADR-034），这正是演示需要的。
5. 只读核对：

```bash
ro "select runtime.nexloop_consumer_persona('<TENANT_ID>','real','<CONSUMER_ID>')"
```

预期 `real`。

```bash
ro "select properties->'external_id_refs', properties->'contact_preferences' from ontology.objects where tenant_id='<TENANT_ID>' and type_name='Consumer' and object_id='<CONSUMER_ID>'"
```

预期两项都是 `[]`。

## 2. 演示

1. 以 `contact-demo-consumer` 登录 WebChat，发一句“以后我只想通过站内消息收到通知”。不要发“可以用微信联系我”这类允许性说法：它不会成为偏好（提取为 `contact_channel_allowed`，0228 不读），这是设计如此。
2. 等抽取完成（新消息由当前版本 `nx019-extractor/4` 直接抽取，不需要重抽）。
3. 只读核对：

```bash
ro "select runtime.nexloop_contact_preferences('<TENANT_ID>','real','<CONSUMER_ID>')"
```

预期包含 `{"dimension":"channel","polarity":"affirmed","value":"native.webchat","dismissed":false,...}`。

4. 负责人在工作台打开这个客户的详情页，在“运营人设与沟通偏好 › 这位客户自己说过的沟通偏好”中看到“联系渠道：站内消息”。

## 3. 退役（演示结束当天，最迟在 EffectControl 到期前）

real 人设的 Consumer 不能用 `consumer.erase_synthetic` 擦除（SQL 拒绝 `persona_real`），所以先改人设、再擦除：

1. `erase_synthetic_consumer.py mark --dry-run`，再去掉 `--dry-run` 执行：配置 `nexloop-consumer-persona/1`，`consumers` 列出这个 Consumer 和它的 principal（必须在第 1 步第 2 条的 webchat-consumers 配方里），理由写 `contact-channel-demo retired`。
2. 核对 persona 变为 `simulated_agent`：

```bash
ro "select runtime.nexloop_consumer_persona('<TENANT_ID>','real','<CONSUMER_ID>')"
```

3. `erase_synthetic_consumer.py erase --dry-run`，看 precheck 只有计数、persona 为 `simulated_agent`；然后执行擦除。
4. 从 webchat-consumers 配方里撤掉这个 principal（`revoke`），身份操作员停用账号；Keychain 条目删除。
5. 核对擦除状态：

```bash
ro "select state from control.nexloop_consumer_erasures where tenant_id='<TENANT_ID>' and consumer_id='<CONSUMER_ID>'"
```

预期进入 `blocking` 之后的状态，最终 `completed`。

## 4. 风险与边界

- 在退役之前，这个客户会进入外联候选（0231）。外发只会以 WebChat 消息出现在这个合成账号里，不会离开 NexLoop 和参考端点。
- EffectControl 到期后，这个客户的效应会被拒绝；即使忘记退役，也不会有新的外部效应。退役仍然必须做，以免留下 real 人设的合成数据。
