# 运行单：工作台外的告警通知（APP_HOST，群机器人 webhook）

- **状态**：草稿。执行者：调度员；负责人只做第 2 步。判定者：L6。证据只记计数和规则名，不记 URL、DSN 或消息正文。
- **依据**：`docs/implementation/alert-notifier-design.md`。
- **适用**：分支 `l4/alert-notifier`。迁移 `alert_notify_export`（临时号 0425，以组批后的编号为准），告警规则 v33 → v34（基于批次 133 的 v33）。compose 没有改动。

## 1. 迁移与规则（DATA_HOST，调度员）

1. 部署前备份：`sudo -n systemctl start nexloop-pg-diff.service`。
2. 部署本批镜像并迁移。迁移尾为组批后的编号，`versions.lock.json` 的 `bootstrap_revision` 与之相同。
3. 告警规则 v34，用现行流程（v33 由批次 133 先上线）：

   ```bash
   docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v34.json --check
   ```

   去掉 `--check` 正式应用。只比 v33 多一条 `timer_stale_alert_notify`。

**核对**（`ro`，只读）：

```sql
select has_function_privilege('nexloop_metrics','authz.nexloop_alerts_firing_export()','execute'),
       has_function_privilege('nexloop_api','authz.nexloop_alerts_firing_export()','execute');
-- 预期 true, false
```

## 2. webhook URL 写进 APP_HOST（负责人，一条命令）

前提：负责人已经在群里建好机器人，并把 URL 存进 Keychain。存的时候用 `-w` 不带值，交互输入，不进 shell 历史：`security add-generic-password -s nexloop/stage/alert-webhook-url -a stage -w`。同时在 `~/.nexloop/vault-index.md` 记一行，只写名字和用途，不写值。

在负责人的 Mac 上执行（URL 只经过管道，不进命令行参数、不经过 python，也不回显）：

```bash
security find-generic-password -s nexloop/stage/alert-webhook-url -w | ssh <APP_HOST 的 ssh 别名> 'sudo -n sh -c "umask 077; cat > /etc/nexloop/alert-notify-webhook-url"'
```

- 要求负责人在 APP_HOST 上的账号能执行 `sudo -n`。如果需要输入密码，这一步改为负责人在场、由调度员执行同一条命令。
- 执行完不需要回传任何内容。第 3 步的 dry-run 会核对这个文件的权限和格式（https），不会打印 URL。

## 3. 安装（APP_HOST，调度员）

1. 配置：把 `deploy/examples/alert-notify.example.json` 复制成 `/etc/nexloop/alert-notify.json`，root 所有，0600。
   - `format` 按负责人选的机器人填：`feishu`、`wecom`、`dingtalk` 或 `generic`。
   - `host_label` 填 `stage`。
2. 只读 DSN：`/etc/nexloop/alert-notify-metrics-dsn`，root 所有，0600。内容与部署门禁已在用的 `nexloop_metrics` 证书 DSN 相同（同一张客户端证书，没有口令）。复制现有文件即可，不新签发凭据。
3. 文件检查：

   ```bash
   sudo -n stat -c '%U %a %n' /etc/nexloop/alert-notify.json /etc/nexloop/alert-notify-metrics-dsn /etc/nexloop/alert-notify-webhook-url
   ```

   预期三个都是 `root 600`。
4. dry-run：只读告警，不发送，也不写状态：

   ```bash
   sudo -n /opt/nexloop/host-health/current/venv/bin/python -I -B /srv/nexloop/release/scripts/deploy/alert_notify.py --config /etc/nexloop/alert-notify.json --dry-run
   ```

   预期一行 JSON：`ok` 为 true，`fired` 等于当前正在触发的条数（stage 上至少有 `timer_stale_file_backup`，除非 f5a9bac6 已经上线），`sent` 为 false。
5. unit：把两个 `.example` 复制到 `/etc/systemd/system/`（去掉后缀），然后：

   ```bash
   sudo -n systemctl daemon-reload
   sudo -n systemctl enable --now nexloop-alert-notify.timer
   ```

6. 立刻跑一轮：`sudo -n systemctl start nexloop-alert-notify.service`。群里应收到一条消息，列出当前正在触发的告警；`journalctl -t nexloop-alert-notify -n 5` 里应有 `"sent": true`。

   这是手工运行，不算定时运行，所以 freshness 要等下一次定时运行之后才更新。
7. host-health：在 `/etc/nexloop/host-health.json` 的 `timers` 里加上 `{"name": "alert-notify", "timer": "nexloop-alert-notify.timer", "service": "nexloop-alert-notify.service"}`，与示例一致，下一分钟生效。

**核对**：

- 下一次定时运行（07、22、37 或 52 分）之后 2 分钟内，host-health 应报 `app_host:alert-notify` 有一次定时成功；
- `timer_stale_alert_notify` 不触发；
- 再下一轮，如果没有变化就不发消息：journal 里 `events` 为 0，`sent` 为 false。

## 4. 验收

1. **新触发、恢复各一次**：在测试租户上制造一条 warning，例如临时停掉一个每 15 分钟运行的 timer。下一轮群里收到“新触发”；恢复后的下一轮收到“已恢复”；中间几轮不重复发。
2. **发送失败不静默**：把 URL 文件临时换成一个无效但仍是 https 的地址（由负责人用同一条管道命令写入），跑一轮：
   - unit 失败；
   - `journalctl -p err -t nexloop-alert-notify` 有 `send failed`，里面没有 URL；
   - 连续失败 45 分钟后，工作台出现 `timer_stale_alert_notify`。

   换回正确的 URL 后，下一轮把积压的事件补发出来。
3. **critical 每 24 小时一次**：不专门制造。观察已有的 critical 告警，满 24 小时时出现一条“持续中”。

## 5. 回退

1. `sudo -n systemctl disable --now nexloop-alert-notify.timer`；
2. 从 host-health 配置里删掉 `alert-notify`；
3. 告警规则回到 v33；
4. 迁移不回退：函数只授权给 `nexloop_metrics`，没人调用时没有影响；
5. URL 文件由负责人决定是否删除：`sudo -n rm /etc/nexloop/alert-notify-webhook-url`。也可以在群里直接删掉机器人，URL 随之失效。
