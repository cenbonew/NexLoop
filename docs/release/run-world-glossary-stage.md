# 运行单：世界模型中文注释上 stage

依据：`docs/design/world-model-glossary.md`；迁移 0374（临时编号，合入时由调度员重排）。执行人在部署主机上照做；示例里的主机、路径一律用角色名和环境变量，真实值见私有附件。

## 0. 前提

- 本分支已合入 main，并按发布流程构建出 backend 镜像，摘要与测试过的发布清单一致。
- 执行前按惯例做 PG 备份（`sudo -n systemctl start nexloop-pg-diff.service`）。
- 模型 key 不新增：机翻服务读 `secrets/claim-extraction-worker/model.env`（负责人 10-29 同意共用 key）。单价读 Agent Host 已有的 `secrets/agent-host/model-tariff.cny.json`。

## 1. 新服务的身份（APP_HOST）

```bash
python3 scripts/deploy/stage_secrets.py add-service --out "$NEXLOOP_ROOT/secrets" --ca-dir "$CA_DIR" --params "$PRIVATE_PARAMS" --service world-glossary-translator
```

生成 `secrets/world-glossary-translator/` 下的 `dsn`、`pg-client.crt`、`pg-client.key`、`service-credential`。不复制任何别的服务的 DSN 或证书。

## 2. 注册与发布（先 dry-run）

1. `stage_secrets.py publish ... --dry-run`：确认
   - `actions_to_publish` 里有 `nexloop.world_glossary.set:1`；
   - Action 总数 129，主体 26，凭据文件 35；
   - 没有 conflicts。
2. 去掉 `--dry-run` 正式发布；再按现有 register 步骤写入 service-grants v23（新主体 `world_glossary_translator`，唯一授权 `nexloop.world_glossary.translate:1`）。
3. `workbench_roles.py --apply`：workbench-roles v49，owner 获得 `nexloop.world_glossary.set:1`。

## 3. 迁移

照常执行迁移（0374）。迁移只新增文件，不改已发布的迁移。

## 4. 发布词表 v1

```bash
python3 scripts/deploy/world_glossary_apply.py --manifest deploy/configuration/world-model-glossary.v1.json --check
```

应输出 `entries: 304`，`manifest_sha256` 与 `sha256sum` 一致。

```bash
python3 scripts/deploy/world_glossary_apply.py --bootstrap-dsn-file "$BOOTSTRAP_DSN_FILE" --tenant "$TENANT_ID" --expected-revision 0 --manifest deploy/configuration/world-model-glossary.v1.json
```

应返回 `applied: true`、`manual_entries: 304`。重跑同一文件是重放，不写任何东西。

## 5. 定时运行机翻（一次一轮）

机翻服务不常驻：应用的 60 个 PG 连接已用满（`deploy/stage/connection-budget.v1.json`）。设计第 8 节写明了运维保留的峰值和约束。

1. 先手工跑一轮，确认能跑通：
   ```bash
   docker compose -f deploy/stage/compose.app.yaml --env-file "$APP_ENV" --profile world-glossary run --rm -T world-glossary-translator
   ```
   输出一行计数摘要（enqueued、translated、failed、manual_kept、quota），退出码 0。
2. 安装 `deploy/stage/systemd/nexloop-world-glossary.{service,timer}.example`：去掉 `.example` 后缀，按实际路径填写，然后 `systemctl daemon-reload`，再 `systemctl enable --now nexloop-world-glossary.timer`。timer 落在每小时的 03、13……分，`flock` 保证同一时间只有一个实例。
3. host-health：在 APP_HOST 的安装配置 `timers` 里加上 `world-glossary`，重跑 `host_health_install.sh`（幂等）；告警规则发布 v22（`alert_rules_apply.py`）。必须先装好 timer，再加这一项，否则会按 fail closed 在 30 分钟后报 `timer_stale_world_glossary`。
4. 运维约束：
   - 做迁移或 sim-setup 前，先 `systemctl stop nexloop-world-glossary.timer`，做完再启动；
   - 不在 02:00 的 PG 备份窗口里做人工操作。

## 6. 验收

- 工作台“企业世界模型”页：标识显示为 `Consumer（客户）`；机翻条目带“机翻”标记（前端由 L9 实现）。
- 只读查询（admin，按租户）：
  - `control.nexloop_world_glossary` 按 `source` 计数：manual 304，machine 为运行后的机翻条数；
  - `control.nexloop_world_glossary_usage` 当天条数 ≤ 50；
  - `runtime.nexloop_cost_entries` 里 `entry_id like 'model:world-glossary:%'` 的条目，`basis=configured_rate`。
- owner 在工作台改一条译名：`gloss_source` 变为 manual，下次机翻不再覆盖它。

## 7. 回退

- 停用定时器即停止机翻。
- 已写入的译名只是显示数据。如需撤回某条机翻，发一版清单把它写成 `null`（恢复自动翻译）或 `""`（人工清空）；不要直接改表。
