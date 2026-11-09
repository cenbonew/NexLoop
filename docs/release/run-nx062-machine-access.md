# NX-062 机器接入：stage 启用、pepper 备份与恢复演练

- **适用**：NX-062 M2b（受治理的生命周期 Action）和 M3（`/machine/v1` 与 machine_key realm）。执行：调度员；判定：L6。M5 之前，机器密钥只能读一个空的能力列表，没有任何读写能力（`docs/implementation/NX-062-M2b-machine-lifecycle.md`）。
- **公开文本**：主机只写角色名（APP_HOST、DATA_HOST、管理机），不写地址。本文的命令不打印任何密钥或 pepper 的值。

## 1. 部署步骤（按顺序）

1. 备份：按惯例执行 `sudo -n systemctl start nexloop-pg-diff.service`，确认成功后再迁移。
2. 迁移：本批的 NX-062 迁移（组批时会重新编号）。
3. 新 Action 必须先 publish：`stage_secrets.py publish` 先 dry-run，确认计划里有 6 个 `nexloop.machine_client.*` / `nexloop.machine_key.*`，然后正式执行。没发布之前，工作台调用会返回 404。
4. 工作台角色清单 v54：重新配置（owner 拥有 104 个 Action）。
5. pepper 文件：
   - 新生成的密钥树由 `generate` 写出 `app/secrets/api/machine-key-peppers`（版本 1）。
   - 已有的密钥树用下面的命令补加（不打印值，不覆盖已有文件），再按现有流程安装到 APP_HOST：0600，属于 api，路径对应 compose 里的 `api_machine_key_peppers`。

     ```bash
     python scripts/deploy/stage_secrets.py machine-key-peppers --out <树>
     ```

6. 速率策略：以 configurator 身份发布 `deploy/configuration/machine-rate-limits.v1.json`。**不发布就是关闭**：所有机器请求返回 503 `rate_policy_missing`，告警 `machine_rate_policy_missing` 触发。

   ```bash
   python -m nexloop_eios.machine_access publish-rate-policy --policy deploy/configuration/machine-rate-limits.v1.json --database-url-file <configurator DSN 文件>
   ```

7. 告警规则 v35：按现有的 `apply_rules` 流程应用。v35 在 v34 的基础上只增加 `machine_rate_policy_missing` 和 `machine_key_denials` 两条规则。
8. Gateway：新的 `Caddyfile.template` 增加了 `/machine/*`，转发时删除身份类与转发类请求头。整个网关没有访问日志（C9），所以 URI 不会被记录。
9. 重启 api-1、api-2 和 gateway。先跑 deploy gate，通过后再 promote，两步不要连在一起执行。
10. 验收（test world 的合成主体）：负责人在工作台登记 `s6c-machine`，签发一把 world 为 test、scope 为允许表内能力的密钥。
    - 无凭据、cookie、其他 Bearer：返回 401。
    - 这把密钥：返回 200，结果是 `{"capabilities": []}`。
    - 带任何查询串：返回 400。
    - 超过每分钟上限：返回 429。
    - 吊销后：返回 401。
    - 指标块 `machine_access` 的计数与上面的调用一致。

## 2. pepper 的私有备份

- pepper 只在 APP_HOST 的 api secret 里，不进 PG，不进 Runtime，也不进普通的数据备份。**pepper 丢失，所有机器密钥都失效**，只能重新签发，签发出去的密钥无法恢复。
- 备份位置：管理机钥匙串，条目 `nexloop/stage/machine-key-peppers`，账户 `nexloop`。这和审计链封印密钥是同一条私有备份路径（`docs/release/run-nx059-stage-verify.md` P3）。
- pepper 文件有多行（每个版本一行），所以以 base64 单行的形式存入钥匙串。值经 `security -i` 的标准输入写入，不出现在任何命令的参数里（与 `run-s6-stage-prep.md` 同一做法）：

  ```bash
  P=$(ssh <APP_HOST 管理入口> 'sudo -n base64 -w0 <NEXLOOP_ROOT>/secrets/api/machine-key-peppers'); printf 'add-generic-password -U -s %s -a nexloop -w %s\n' nexloop/stage/machine-key-peppers "$P" | security -i >/dev/null 2>&1 || echo "keychain: add failed" >&2; unset P
  ```

- 在 `~/.nexloop/vault-index.md` 登记一行，只写条目名和用途，不写值。
- 恢复时写到 0600 的文件里（演练见 §3）：

  ```bash
  umask 077; security find-generic-password -a nexloop -s nexloop/stage/machine-key-peppers -w | base64 -d > <pepper 文件>
  ```

  核对是否存在（不加 `-w`，不显示值）：

  ```bash
  security find-generic-password -a nexloop -s nexloop/stage/machine-key-peppers >/dev/null && echo present
  ```

- 每次轮换（`stage_secrets.py machine-key-peppers --out <树> --rotate` 追加下一版本，旧版本保留）之后，都要立即重新写入钥匙串，并重启 api-1 和 api-2。
- 撤下旧版本：先确认没有任何有效密钥还在用它。

  ```sql
  select count(*) from authz.nexloop_machine_keys where pepper_version=<v> and status='active' and expires_at>clock_timestamp()
  ```

  结果为 0 时才能从文件中删掉那一行，然后同样重新写入钥匙串并重启。

## 3. 恢复演练条目（纳入 AT-072 本机恢复演练的下一轮）

| # | 步骤 | 期望 |
|---|---|---|
| P-1 | 演练前，在源端签发一把 test world 的合成机器密钥，记下 prefix（不记明文；明文只保存在演练用的私有文件里，0600） | 签发成功 |
| P-2 | 按 AT-072 恢复数据库到演练目录 | `authz.nexloop_machine_keys` 中有这把密钥的行，库里只有摘要 |
| P-3 | **不放** pepper 文件就启动演练 API | `/machine/v1/capabilities` 带这把密钥返回 503 `unavailable`，失败关闭 |
| P-4 | 把钥匙串里的 pepper 写到演练 API 的 pepper 文件（0600），重启 | 同一把密钥返回 200，结果是 `{"capabilities": []}` |
| P-5 | 用错误的 pepper（随机生成的同版本值）启动 | 返回 401，并计入 denied |
| P-6 | 清理：删除演练用的 pepper 文件和明文文件，在源端吊销这把合成密钥 | 吊销后源端返回 401 |

演练记录写进演练报告，需要包含：执行人、判定人、时间（取自 `date`）、各步的状态码。
