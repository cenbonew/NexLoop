# 运行单：入口票据（S81 / ADR-043，stage）

- 适用分支：`l2/s81-entry-ticket`，基于第 80 批 8ba485e6。迁移 0392（临时号）。
- 用途：负责人在「管理 › 关联系统入口」点"直接进入"：
  - WebChat 以合成测试客户 `entry-test-consumer` 的身份打开；
  - 参考端点员工页以只读测试员工 `entry-test` 的身份打开。
- 执行者：调度员。判定者：L6。
- 口令、密钥、票据只放在 Keychain 和两台主机的 secrets（0600），不写进文档、证据或命令行。

## 0. 前提

- 0392 已迁移：`\df control.nexloop_entry_ticket_issue` 有结果。
- 本批镜像已部署：api 和参考端点都已换成本批。

## 1. 发放共享密钥（管理机 → APP_HOST）

```bash
python3 <检出>/scripts/deploy/stage_secrets.py entry-ticket-key --out <树>
```

- 预期输出 `{"added": ["app:api/entry-ticket.key", "management/endpoint/entry_ticket_key"]}`，不含密钥值。`stage_secrets.py check --out <树>` 预期 ok。
- 新生成的树里 api 那一份已经由 generate 写好，这条命令只补参考端点的副本。
- 安装做法同 run-i31 第 3.3 条：管道传入，sudo 口令来自 Keychain，先装到临时文件再 `mv -f`。
  - `<树>/app/secrets/api/entry-ticket.key` 装到 `$NEXLOOP_ROOT/secrets/api/entry-ticket.key`，属主 10001，0600；
  - `<树>/management/endpoint/entry_ticket_key` 装到参考端点的 `$ENDPOINT_SECRETS_DIR/entry_ticket_key`，属主 10002，0600。
- 核对：两边 `sudo sha256sum` 的结果一致，并与 `<树>/manifest.json` 里 `api/entry-ticket.key` 的摘要一致。只比摘要，不看内容。

## 2. 建测试客户（受治理路径）

配方模板是 `deploy/examples/nx037/entry-test-consumer.example.json`。私有副本 0600，不进仓库。

1. **身份操作员建号**：用配方的 `identity_human` 部分，做法同 identity-human 配方。口令用 python secrets 生成，并检查长度；生成后直接存进 Keychain（`nexloop/…/entry-test-consumer`），再经管道装到主机。不回显，不写文件。
2. **加入 WebChat 客户授权**：用配方的 `webchat_consumers` 部分执行 `python -m nexloop_eios.webchat_consumers --apply ...`。
3. **建出 Consumer**：用 entry-test-consumer 手动登录 WebChat 一次，开一个对话。这一步会写入 `control.nexloop_consumer_owners`。
4. **标记 persona**：负责人在工作台对这个 Consumer 执行 `consumer.set_persona` = `simulated_agent`，理由写 `entry-test`（ADR-034）。
5. **只读核对**：
   - `ro "select runtime.nexloop_consumer_persona('<TENANT_ID>','real',(select consumer_id from control.nexloop_consumer_owners where tenant_id='<TENANT_ID>' and principal_id='<PRINCIPAL_ID>'))"` 预期 `simulated_agent`。
   - 0231 的外呼候选会因 `persona_excluded` 跳过这个客户。

参考端点**不建账号**：只读员工会话由票据签出，没有口令。

## 3. 配置与发布（APP_HOST）

1. 在私有的 `compose.app.env` 里写入：
   - `ENTRY_TEST_WEBCHAT_PRINCIPAL=<PRINCIPAL_ID>`；
   - 把开关变量 `NEXLOOP_ENTRY_TICKETS` 设为 stage。这一行只写在主机的私有 env 里；任何公开文件都不得把它设为 stage，check_publication 会拒绝。
2. 确认 `REFERENCE_ENDPOINT_STAFF_URL` 已配置（S73）。
3. 重建 api-1、api-2：
   - `docker compose ... up -d --no-deps api-1 api-2`；
   - 核对：`docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' <api-1 容器> | grep -c ENTRY_TICKETS` 预期 1，**不要**打印这一行的值。
4. 参考端点：
   - 叠加 `deploy/stage/reference-endpoint/compose.entry-ticket.example.yaml` 重建：`docker compose -f compose.example.yaml -f compose.entry-ticket.example.yaml --env-file <私有 env> up -d`；
   - 核对：用一个无效票据请求 `/staff/entry`，预期 401，返回固定页"自动登录失败"。不叠加这个文件时预期 404。

## 4. 只读核对

```bash
ro "select target, outcome, count(*) from control.nexloop_entry_ticket_events where tenant_id='<TENANT_ID>' group by 1,2 order by 1,2"
```

- 第 5 步走查完成后，预期至少有：
  - `webchat issued`
  - `webchat session`
  - `reference_staff issued`
- 不应出现 `not_synthetic`。如果出现，说明第 2 步的 persona 或钉住的 principal 有问题，停下报告。

## 5. 走查（负责人）

1. 以 owner 登录工作台，打开「管理 › 关联系统入口」。
2. 点"以测试客户身份直接进入 WebChat"：新窗口打开 WebChat，已登录为 entry-test-consumer，地址栏是 `/`，不带票据。
3. 点"以只读测试员工身份直接进入员工页面"：新窗口打开员工页，顶部提示"只读测试员工（entry-test）"，工单没有标记按钮。
4. 用运营角色登录工作台：入口页的按钮仍然可见，但点了只会提示"没能直接进入"（签发被拒，返回 403 `not_owner`）。

## 撤回

- **关开关**：从私有 env 删掉开关那一行，或改为 off，然后重建 api-1、api-2。签发返回 404，入口页按钮消失。
- **参考端点**：去掉叠加文件重建即可。
- **轮换密钥**：`stage_secrets.py entry-ticket-key --rotate`，然后按第 1 步重新安装，api 和参考端点一起重启。
- **测试客户**：如果不再需要，用 `consumer.erase_synthetic` 擦除（persona 是 simulated_agent，允许擦除）。票据表里的相关行会随之假名化。

## 证据

- 第 1 步的输出和摘要比对结果；
- 第 2.5 步的查询结果；
- 第 4 步的计数；
- 第 5 步的截图：不能露出票据、口令、主机名或地址。
