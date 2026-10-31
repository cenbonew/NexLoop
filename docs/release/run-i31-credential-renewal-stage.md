# I31 stage 服务凭据续期运行单（2026-11-03 前执行）

stage 上有三个 active 服务凭据于 **2026-11-04T00:00:00Z** 到期：

| 凭据 | 主体（role） | 处理 |
| --- | --- | --- |
| `nexloop-service-outreach-sender-key` | `outreach_sender` | 续期（rotate） |
| `nexloop-service-world-glossary-key`（world real） | `world_glossary_translator` | 续期（rotate） |
| `nexloop-service-relation-backfiller-key` | `relation_backfiller`（v21 起退役，没有授权） | 撤销（revoke --retired），不续期 |

到期后果（I31 诊断）：认证要求 `status='active' and expires_at>now`，否则 `service authentication denied`（42501），失败关闭。

- **outreach-sender**：运行中每个 tick 都重新认证，失败时打 `background tick failed` 后继续重试；重启时认证失败，进程以 1 退出，容器反复重启。accepted 的意图留在队列里，不会丢。
- **world-glossary**：每轮 `--once` 以 1 退出，触发 `timer_stale_world_glossary` 和 `timer_scheduled_failure`；job 保持 pending。

两者在新凭据装好后都会自动恢复。

## 0. 前提

| 编号 | 前提 | 核对 |
| --- | --- | --- |
| P1 | 迁移 0384（`service_credential_rotation`）已执行 | `select max(version) from` 迁移表，或 `\df control.nexloop_rotate_service_credential` 有结果 |
| P2 | `$RUN/src` 是含 0384 和本单代码的 main 打包，做法同 run-assembly §0.4 | `ls $RUN/src/scripts/deploy/stage_secrets.py`；`grep -c rotate-credential` 大于 0 |
| P3 | 管理机上的秘密树 `<树>` 与 `$RUN` 一致，并另存快照 `cp -a <树> <树>.before-i31`（0700，不进仓库；第 7 步销毁） | `stage_secrets.py check --out <树>` 预期 ok |
| P4 | 告警规则 v25 已发布（见第 6 步，可先做） | 87+2=89 条 |

- **`$COMMON`、`$WINDOW`**：与 run-nx057-remediation-stage §2 相同（DATA_HOST）。`$RUN` 只读挂在 `/stage`，本单在 DATA_HOST 上只写数据库，不写树。
- **`$NEW_EXPIRES`**：新凭据的到期时间。建议 `2027-01-31T00:00:00+00:00`，与其余 4 个 2027-01-31 的凭据对齐。函数要求它晚于当前到期时间、至少 1 小时后、最多 400 天后。
- 证据只写角色名（APP_HOST、DATA_HOST），不贴令牌、DSN、口令、地址或主机名。所有输出只含路径、计数和 ID，命令的输出可以直接存证。

## 1. 准备（管理机）

对两个主体各执行一次：

```bash
python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role outreach_sender --prepare
```

```bash
python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role world_glossary_translator --prepare
```

预期：
- `{"credential_id": "nexloop-service-outreach-sender-key", "files": ["app:outreach-sender/service-credential"], "prepared": true, "role": "outreach_sender"}`；
- glossary 的那条 `files` 为 `["app:world-glossary-translator/service-credential"]`。

效果：
- `<树>/management/rotation/<role>/` 目录 0700，内有 `current.json`（当前令牌）和 `pending.json`（新令牌和 request_id），都是 0600；
- 凭据文件和 `service-secrets.json` 都还没改。

然后把树同步到 `$RUN`（属主 999，目录 0700、文件 0600），做法同 run-nx054-s8-backfill 第 6 步第 2 条。

- **撤回**：删除 `<树>/management/rotation/<role>/`，再同步一次。数据库还没动。

## 2. 在库里加新令牌（DATA_HOST，配置员窗口）

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py rotate-credential --out /stage --role outreach_sender --apply \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID" --credential-expires-at "$NEW_EXPIRES"
```

world_glossary_translator 同样执行一次。

预期：
- `"rotated": true, "active_tokens": 2, "replayed": false, "credential_id": ...`，`expires_at` 等于 `$NEW_EXPIRES`，`previous_expires_at` 为 `2026-11-04T00:00:00+00:00`；
- 同一命令再执行一次，得到 `"replayed": true` 和相同的 `authority_revision`。

此时新旧两个令牌都能认证，绑定的是同一个主体。认证 fact 的 `expires_at` 已经改成新时间。服务还在用旧令牌，不受影响。

- **拒绝时**：`ServiceGrantsRejected credential_rotate_refused:42501`，原因可能是：
  - 当前令牌不是唯一的 active 令牌，例如已经轮换过一次；
  - 新到期时间不在允许范围内；
  - 签名密钥不对；
  - 认证 fact 与库里不一致。

  遇到时停下，交调度员。函数是失败关闭的，什么都没写。
- **只读核对**：

  ```sql
  select credential_id,count(*) filter (where status='active') active,max(expires_at) filter (where status='active') latest
  from authz.nexloop_service_credentials where credential_id in ('nexloop-service-outreach-sender-key','nexloop-service-world-glossary-key') group by 1;
  ```

  预期两行，`active` 都是 2，`latest` 等于 `$NEW_EXPIRES`。
- **撤回**：到这一步为止什么都不用撤，旧令牌继续有效。新令牌没有被任何服务使用，可以在第 4 步撤销旧令牌之前保持原样。如果要放弃这次续期，交调度员决定。

## 3. 切换到新令牌并重启（管理机 → APP_HOST）

1. **管理机**：对两个主体各执行一次 `--switch`，把新令牌写进凭据文件、`service-secrets.json` 和 `manifest.json` 的摘要：

   ```bash
   python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role outreach_sender --switch
   ```

   预期 `{"files_rewritten": ["app:outreach-sender/service-credential"], "role": "outreach_sender", "switched_to": "new"}`。然后 `stage_secrets.py check --out <树>` 预期 ok，并同步到 `$RUN`。
2. **重启前核对**（APP_HOST，只读）：
   - `docker inspect -f '{{.State.Status}} {{.RestartCount}}' nexloop-app-outreach-sender-1` 记下状态和重启次数；
   - `docker logs --since 10m nexloop-app-outreach-sender-1 2>&1 | grep -c 'background tick failed'` 记下次数，预期 0。
3. **装到 APP_HOST**：文件内容走 ssh 管道，sudo 口令从 Keychain 经管道传入，两者都不落中间明文文件，也不进命令行。以 outreach-sender 为例：

   ```bash
   { security find-generic-password -s <Keychain 中 APP_HOST sudo 的条目> -w; cat <树>/app/secrets/outreach-sender/service-credential; } | \
     ssh <APP_HOST> 'F="$NEXLOOP_ROOT/secrets/outreach-sender/service-credential"; sudo -S -p "" sh -c "install -o 10001 -g 10001 -m 0600 /dev/stdin \"\$1.i31new\" && mv -f \"\$1.i31new\" \"\$1\"" sh "$F"'
   ```

   - sudo 从标准输入的第一行读口令，`install` 读其余部分。
   - **为什么先装到 `<文件>.i31new` 再 `mv -f`**：APP_HOST 上的 GNU `install` 以 `/dev/stdin` 为源覆盖**已存在**的目标文件时报 `install: No such file or directory`（新建文件不报；R62 用探针文件复现）。所以先 install 到同目录的新文件（属主 10001、0600），再 `mv -f` 在同一文件系统上原子替换。结果仍是新 inode，第 4 条的重启理由不变。
   - 装之前记下旧 inode：`sudo stat -c '%i' <文件>`。
   - 装完核对（都只看元数据和摘要，不显示内容）：
     - `sudo stat -c '%u:%g %a %s %i' <文件>` 预期 `10001:10001 600`、非零大小，inode 与装之前不同；
     - `sudo test ! -e <文件>.i31new && echo no-temp` 预期 `no-temp`（没有遗留临时文件；若 install 成功而 mv 失败，临时文件会留下，删除后重做本条）；
     - `sudo sha256sum <文件>` 与 `<树>/manifest.json` 中这个文件的 `sha256` 一致。
   - glossary 的路径是 `$NEXLOOP_ROOT/secrets/world-glossary-translator/service-credential`，属主同样是 10001。
4. **重启 outreach-sender**：

   ```bash
   docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/config/compose.app.env" \
     --env-file "$NEXLOOP_ROOT/config/compose.images.env" restart outreach-sender
   ```

   必须重启：`mv -f` 替换后是新的 inode，而 compose 的 `secrets: file:` 是 bind-mount，运行中的容器还看着旧文件。重启后会重新挂载。

   glossary 不用重启，下一轮 timer 会起一个新容器读新文件。
5. **重启后核对**（APP_HOST）：
   - `docker inspect -f '{{.State.Status}} {{.RestartCount}}' nexloop-app-outreach-sender-1`：预期 `running`，重启次数没有继续增长；
   - 等 2 个 tick 后，`docker logs --since 5m nexloop-app-outreach-sender-1 2>&1 | grep -E 'background tick (failed|recovered)|unavailable'` 预期没有输出；
   - glossary：等下一轮 timer（最多 10 分钟），`systemctl show nexloop-world-glossary.service -p Result -p ExecMainStatus` 预期 `Result=success`、`ExecMainStatus=0`；
   - 以上是新令牌在用的证据。旧令牌这时仍有效，所以出问题可以直接回退。
- **撤回**（第 4 步之前随时可以）：管理机 `rotate-credential --role <role> --switch-back` 写回旧令牌，`check` ok 后同步，再按第 3.3 条装回 APP_HOST 并重启。新令牌留在库里，到期即失效；也可以之后由调度员决定撤销。

## 4. 撤销旧令牌（DATA_HOST）

确认第 3.5 条全部通过后再做：

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py revoke-credential --out /stage --role outreach_sender --previous \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID"
```

world_glossary_translator 同样执行一次。

- 预期：`"revoked": 1, "active_tokens": 1, "retired": false`。
- 前提是树里已经是新令牌（第 3 步同步到 `$RUN` 之后），否则拒绝：`switch to the new token first`。
- 撤销规则：剩下的令牌必须比被撤销的晚到期，否则 `credential_revoke_refused:42501`。
- 恢复日志会记下被撤销令牌的摘要，PITR 回放时只撤销这一个令牌。
- **撤销后的核对**：重复第 3.5 条；第 2 步的只读查询预期 `active` 都是 1。
- **管理机收尾**：

  ```bash
  python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role outreach_sender --finish
  ```

  预期 `{"finished": true, ...}`，删除 `management/rotation/<role>/`，里面有已撤销的旧令牌。然后同步到 `$RUN`，同步时 `$RUN/management/rotation/` 下对应目录也要删掉。

- **撤回**：撤销不能撤回。撤销之后如果发现新令牌有问题，就从第 1 步重新做一次续期（新的 request_id），不能再用旧令牌。

## 5. 退役主体的凭据（DATA_HOST）

**前置核对（只读，执行前必须都成立）**：

1. 该服务已从 compose 移除：在 APP_HOST 上 `docker compose -f <stage compose> config --services | grep -c relation` 预期 0，`docker ps --format '{{.Names}}' | grep -c relation` 预期 0。
2. 库里没有规则 fact（配置员窗口）：

   ```sql
   select fact_kind,count(*) from authz.nexloop_authority_facts f
    where tenant_id='<TENANT_ID>' and entity_key[1]=(select binding->>'subject_principal_id' from authz.nexloop_service_credentials
      where credential_id='nexloop-service-relation-backfiller-key' limit 1)
      and fact_kind in ('grants','message_read_rule','message_purpose_rule','property_access_rule','relay_source_read_rule',
                        'role_source_read_rule','definition_read_rule','property_group_restriction')
    group by 1;
   ```

   预期：没有行，或只有一行 `grants`（其 `payload->'grants'` 为空数组）。有任何规则型 fact 就停下，先回报调度员。

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py revoke-credential --out /stage --role relation_backfiller --retired \
  --credential-id nexloop-service-relation-backfiller-key --request-id <固定 UUID，记入证据> \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID"
```

- 预期：`"revoked": 1, "active_tokens": 0, "retired": true`。同一命令再执行一次得到 `"replayed": true`。
- 前提：
  - 主体在 service-grants 的 `retired_principals` 里；
  - 库里这个主体已经没有任何授权（v21 的 register 已经撤销），也没有任何规则型 fact（`message_read_rule`、`message_purpose_rule`、`property_access_rule`、`relay_source_read_rule`、`role_source_read_rule`、`definition_read_rule`、`property_group_restriction`）。
  - 否则拒绝（`not a retired principal` 或 `credential_revoke_refused:42501`）。
- 不撤销的话，它会在 11-04 后让 `service_credential_expired`（critical）一直报警。

## 6. 告警规则 v25（DATA_HOST，配置员窗口）

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v25.json --check
```

再去掉 `--check` 正式应用（`--expected-revision` 等参数同 R54 S8）。预期 89 条：在 v24 的基础上加 `service_credential_expiring`（warning：7 天内到期）和 `service_credential_expired`（critical：已过期仍 active）。

- **v25 发布时机**：
  - 在第 1–4 步之前发布：三条凭据都在 7 天内到期，`service_credential_expiring` 会立即报出。
  - 续期过程中，被轮换的旧令牌也会一直报这条，直到第 4 步撤销。
  - 这是预期行为，正好说明告警有效，不是误报。
  - 特别是：第 2 步轮换之后、第 4 步撤销旧令牌之前，`expiring_7d` 的 warning（`service_credential_expiring`）会一直触发——旧令牌那一行仍在 7 天内到期。属于预期；第 4 步完成后应在下一个评估周期自行恢复，没有恢复才需要排查。
- **只读核对**：

  ```sql
  select runtime.nexloop_metrics_snapshot('<TENANT_ID>','real')->'service_credentials';
  ```

  第 1 步之前预期 `expiring_7d` 3、`expired_active` 0；第 4、5 步之后 `expiring_7d` 0、`expired_active` 0，`earliest_expires_at` 不早于 2027-01-12。

## 7. 收尾：销毁 P3 快照（管理机）

两个服务（outreach_sender、world_glossary）都完成第 4 步并通过第 3.5 条核对、第 5 步完成、第 6 步只读核对符合预期之后，才执行。快照 `<树>.before-i31` 里有旧令牌和整棵秘密树，不能留：

```bash
find <树>.before-i31 -type f -exec shred -u -z -n 3 {} +   # macOS 无 shred 时用 gshred（coreutils）
rm -rf <树>.before-i31
test ! -e <树>.before-i31 && echo snapshot-gone
```

- 预期：`snapshot-gone`。
- 注意：快照删除后第 3 步的切回（`--switch-back`）不再能回到续期前的整棵树；所以必须在全部核对通过之后才做。
- APFS/SSD 上覆写不保证物理擦除；快照所在卷应是 FileVault 加密卷；这里的 shred 是尽力而为，结果记入证据。

## 8. 证据与遗留

- 证据记录：每一步的命令输出（已经不含令牌）、第 3.5 条的核对结果、第 6 步前后的 `service_credentials` 快照；执行人和判定人分开写。
- **PITR 注意**：续期本身不进恢复日志。如果 stage 恢复到续期之前的时间点：
  - 库里没有新令牌，服务会认证失败（失败关闭）；
  - 被恢复的旧令牌如果还没到期，依然可用；
  - 处理办法是按本单重新续期。
- 下一次到期（2027-01-12 的 24 个凭据）用同一套命令，可以逐个主体做。
