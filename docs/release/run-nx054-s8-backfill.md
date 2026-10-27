# NX-054 §8 第 6–14 步：回填执行清单（release 52 及以后）

依据：`docs/implementation/NX-054-stage-publish.md` §7、§8；设计稿 `docs/design/relation-backfill-p3.md`。本清单只把那两节展开成逐条命令，并写明预期输出；通过条件与中止条件以 §8 为准。

- 源码基准：main 17aa74c1。stage 上的 release 52（933bc1a2）与它在 `nexloop_eios/relation_backfill.py`、迁移 0349/0350、`stage_secrets.py`、`deploy/authorization/` 上没有差异。DATA_HOST 的 `$RUN/src` 用 87227dca 也可以（同为 service-grants v20、stage-principals v5）。
- 前提：目录 v1 已发布（25 个派生类型，`assessment_evidence` 推迟）；迁移头 0363；第 5 步已由 L6 判定。
- **回填期间不要补发 `assessment_evidence`**（`--resume-derivation --from-version 1`）。它会改变派生映射的 digest：运行中的批会以 `catalog_changed` 停下，verify 的范围也会变。补发放在第 14 步之后，而且要在出现任何 RelationshipAssessment 对象之前做，因为那时回填主体已经撤销（缺口检查的 `deferred_with_objects` 会报出这种情况）。
- 执行人：调度员。判定人：L6。

## 第 6 步：凭据与授权（service-grants v20）

1. **先分情况**（DATA_HOST，只读）：执行 `run-nx057-remediation-stage.md` §2.1 中 `nexloop-service-relation-backfiller-principal` 那条授权查询。
   - **情况 A**：有一行，长度为 1。说明 NX-057 已经按 v20 register 过。
   - **情况 B**：查询为空。
2. **管理机**：`stage_secrets.py setup-credentials --out <树>`
   - 情况 B 的预期输出：`{"changed": true, "added": ["setup/relation-backfiller-credential"], "setup_files": 6}`。
   - 情况 A（树里已经有这个文件）的预期输出：`{"changed": false, "added": [], "setup_files": 6}`。
   - 把树同步到 `$RUN`：属主 999，0600。
3. **DATA_HOST**：先 dry-run，再正式执行。`$COMMON`、`$WINDOW` 与 `run-nx057-remediation-stage.md` §2 相同。

   ```
   docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" --dry-run
   docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" \
     --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID" --credential-expires-at "$CREDENTIAL_EXPIRES_AT"
   ```

   - dry-run 的 `grants` 中有 `relation_backfiller action eios:action:nexloop.relation.backfill:1 execute`。
   - dry-run 的 `principals` 有 25 个，`credential_files` 为 34。

   | 字段 | 情况 A | 情况 B |
   | --- | --- | --- |
   | `changed` | false | true |
   | `grants_added` | 0 | 1 |
   | `credentials_created` | `[]` | `["relation_backfiller"]` |
   | `grants_revoked`、`extra_grants_outside_manifest` | 0 | 0 |

   - 数字不符就停下：说明库里的授权与清单在这之前已有偏差。
   - 被拒为 `credential_rotation_required` 时，不要重试，停下报告。
4. **核对**：重跑第 1 条的查询，应当有一行，长度为 1。

## 第 7 步：`$BF`（APP_HOST）

镜像以 10001:10001 运行。`read_private_text` 和 libpq 都要求文件属主等于进程用户、权限 0600，所以 `$BF` 和其中的文件属主都是 10001。

```
BF=$(sudo -n mktemp -d /dev/shm/nexloop-backfill.XXXXXX)
S="$NEXLOOP_ROOT/secrets"
sudo -n install -o 10001 -g 10001 -m 0600 <树>/setup/relation-backfiller-credential "$BF/service-credential"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/retention-keeper/pg-client.crt" "$BF/pg-client.crt"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/retention-keeper/pg-client.key" "$BF/pg-client.key"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/shared/db-ca.crt" "$BF/db-ca.crt"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/shared/signing.key" "$BF/signing.key"
sudo -n chown 10001:10001 "$BF"
```

- 这个主体在库里的角色是 `nexloop_domain_worker`，retention keeper 也是。用 retention keeper 的那一组 DSN 和证书。
- 凭据文件来自管理机的树，用 `scp` 等私有通道送到 APP_HOST，送到后立即删除中转副本。
- **DSN**：复制 `$S/retention-keeper/dsn`，只改三个路径，改成 `$BF` 中副本的绝对路径：`sslcert=$BF/pg-client.crt`、`sslkey=$BF/pg-client.key`、`sslrootcert=$BF/db-ca.crt`。其余不变。结果写入 `$BF/dsn`，属主 10001，0600。
  - 容器里 `$BF` 挂在同一路径（`-v "$BF:$BF:ro"`），所以 DSN 中的路径在宿主和容器里都成立。
  - DSN 内容不进命令行、不打印。
- **通过条件**：`sudo -n ls -ln "$BF"` 中 6 个文件都是 10001、`-rw-------`。

## 第 8–13 步：回填（APP_HOST，release 52 的 backend 镜像）

```
APPNET=$(docker compose -p <应用 compose 项目名> ps -q app-net)
BFRUN="--rm --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --network container:$APPNET -v $BF:$BF:ro --entrypoint python"
BFARGS="--database-url-file $BF/dsn --signing-key-file $BF/signing.key --signing-key-id $SIGNING_KEY_ID \
  --service-credential-file $BF/service-credential --artifact-root /tmp"
bf() { docker run $BFRUN "$BACKEND_IMAGE" -m nexloop_eios.relation_backfill $BFARGS "$@"; }
```

- `$BACKEND_IMAGE` 用 release 52 的 backend 镜像 digest，与服务正在运行的镜像相同。
- `$SIGNING_KEY_ID` 取应用 compose env 中的值。缺省值 `active` 不对。
- 网络：domain worker 服务都是 `network_mode: service:app-net`。这里用 `--network container:` 加入同一个网络命名空间，因此 APP_HOST 的 nft 规则（按 `APP_CONTAINER_CIDR` 匹配）照样放行到 DATA_HOST 的 PostgreSQL（TLS）。
- 连接：只有一条，计入 ADR-037 的 7 个运维预留连接。
- 前台运行，不加 `-d`。Ctrl-C 经 docker 的信号代理转给 python，runner 在两次调用之间停下：输出 `event=interrupted`，退出码 3。
- 每条命令的输出都 `| tee -a <证据目录>/<步>.jsonl`。

| 步 | 命令 | 预期 |
| --- | --- | --- |
| 8 | `bf preflight` | `blocking` 为 `[]`，`digest` 记下。`report` 中只出现 25 个已发布的关系，没有 `assessment_evidence`。<br>`catalog_unpublished` 说明派生映射为空。`assessment_name_collision` 不应出现，因为 RelationshipAssessment 没有登记，也就没有对象。 |
| 9a | DATA_HOST 备份（同 §8 第 4a 步） | `Result=success`、`ExecMainStatus=0` |
| 9 | `bf start --run-id p3-1`，然后 `bf run --run-id p3-1 --max-batches 1` | start 输出 `{"run_id":"p3-1","state":"running","cursors":N}`。N =（有对象的源类型 × 它们出现的 world）。<br>run 输出一行 `event=batch`：`elapsed_ms` < 300、`waiters` 为 0，退出码 0（到了批数上限）。 |
| 10 | `bf run --run-id p3-1` | 每批一行。最后有若干行 `event=rescan`，`remaining` 到 0；然后一行 `event=complete`，`completed: true`，退出码 0。 |
| 11 | `bf verify` | 见下面“verify 怎么看”。 |
| 12 | `bf revert --run-id p3-1`，再 `bf verify` | 最后一行 `event=revert`、`state: reverted`。verify 的 `b` 回到回填前的值，`a == b` 此时**不成立**，这是预期的。 |
| 13 | `bf start --run-id p3-2`、`bf run --run-id p3-2`、`bf verify` | 同第 9–11 步。start 会再跑一次 preflight，比对 digest。 |

### 推迟的 `assessment_evidence` 怎么处理

回填完全由 `control.nexloop_relation_derivations` 中 `source_property is not null` 的行驱动：preflight、start 的 cursor、批、verify 都是如此。`assessment_evidence` 没有发布，所以没有映射行，也就没有 cursor 和 verify 行。回填不会碰它，也不会报错。

### verify 怎么看

- `relations` 每一项是一组（关系，world），只列出有 64 位 hex 源值、且 world 不在清除中的组合。所以**不是固定 25 行**：
  - 没有源值的关系没有行；
  - 一个关系在 real、test、sim 中都有源值，就有多行。
- **通过条件**：
  - 每一行 `a == b`、`extra == 0`；
  - `skipped_remaining == 0`；
  - 行中的 relation 都属于 v1 的 25 个，没有 `assessment_evidence`。
- `dangling`（目标不存在）和 `erasing`（属于擦除中的 Consumer）照实记录，不计入 `a`，所以不影响平衡。

## 中止条件与 p95 的对照（第 9–13 步运行期间）

§8 的四条中止条件逐条照办。具体读法如下：

- **`waiters`、`event=paused`、`event=retry`**：在 tee 出来的 jsonl 上计数。
  - 连续 5 批 `waiters` > 3；
  - `paused` 持续超过 10 分钟；
  - 同一 cursor 上 55P03 / 40001 / 40P01 的 retry 累计超过 20 次。
- **`code=connection`**：重试 5 次后 runner 退出（非 0）。先 `bf status --run-id <run>` 再决定，不要直接重跑。
- **0323 写请求 p95**：
  - 读法：工作台指标页的 API 写类 p95（`api.classes.write.p95_ms`，5 分钟窗口）和写类 `calls`。也就是告警规则 `api_read_p95_high` 读的那份快照中的 write 类；数据库里没有更细的 per-route 数据。
  - 回填前的基线：第 9 步之前 15 分钟内读 3 次，取中位数。
  - 运行中：每 5 分钟读一次。比基线高出 25% 以上，或者超过 500 ms，就中止。
  - **写类 `calls` 在该 5 分钟窗口内少于 20 时，p95 不作判据。** stage 没有真实流量，样本太少，p95 只是噪声。这时只看前三条，并在证据里注明“p95 样本不足”。

## 第 5 步 relay 门槛在没有样本时怎么判（建议，由 L6 定）

现状：24 h 窗口内 `relation_derive_timing.py relay` 只有 `authenticate` 有样本。`goal_create`、`step_create`、`assignment_create` 三步在输出里根本没有键：`authz.nexloop_relation_relay_p95` 只对有样本的步骤输出。这三步正是派生要跟随的写入。

建议：

1. 这三步记为“**无样本，不可判定**”，不能记为通过。§4 的“增加不超过 10 ms”在两边都没有数时无从比较。
2. 第 5 步的通过与否，由 sim 墙钟（对照组）、擦除 `sync_purge_seconds`（≤ 25%）两项决定。另加一项**替代判据**：发布前后各 24 h，0323 写类 p95 的增加不超过 10 ms。它覆盖经 API 写入的 Goal、PlanStep、Assignment，同样走派生触发器。前提是两边写类 `calls` 都足够（例如每个 24 h 窗口不少于 200 次），否则这一项也记为“不可判定”。
3. `authenticate` 不写派生跟随的对象，只作噪声对照：它的 `p95_ms_median` 前后变化超过 10 ms，说明环境本身在漂移，第 2 条的数字要打折看。
4. 留作限制写进证据：P4b 上 real 之前，有真实会话样本后，再取一次这三步的 p95，按绝对值对照（没有发布前的基线）。

## 第 14 步：收尾（顺序很重要）

1. APP_HOST：`sudo -n rm -rf -- "$BF"`，然后 `test ! -e "$BF"`。
2. 合入 v21：提交 31884a17，分支上只有这一个提交。它与 main 17aa74c1 能干净合并，涉及的文件在 main 上从它的父提交 3517187c 起没有变化。
   - 合入后 `service-grants` 为 v21：`relation_backfiller` 进入 `retired_principals`。
   - `stage-principals` 为 v6：去掉 `setup/relation-backfiller-credential`。
3. **管理机，先删**：
   - 从树中删除 `setup/relation-backfiller-credential`；
   - 删除 `management/service-secrets.json` 中的 `setup:setup/relation-backfiller-credential` 条目：`jq 'del(."setup:setup/relation-backfiller-credential")'` 写到同目录的临时文件，0600，再 `mv` 覆盖。不打印内容。
   - 再同步到 `$RUN`。
   - **必须在 register 之前删**：v6 下 register 读 `service-secrets.json` 时，遇到没有主体的条目会拒绝（`a generated credential file has no principal: setup/relation-backfiller-credential`），什么也不写。已用 31884a17 的代码实测。
4. **DATA_HOST，再 register**：用含 v21 的 `$RUN/src`，先 dry-run，再执行第 6 步第 3 条的同一条命令。
   - dry-run 预期：`principals` 24 个，`credential_files` 33，`grants` 中没有 `relation_backfiller`。
   - 正式执行的预期：`grants_revoked` 1、`grants_added` 0、`credentials_created` `[]`。
5. **核对**：第 6 步第 1 条的授权查询返回空。
6. 清理 Keychain 中这个凭据的条目（如有），并更新 `~/.nexloop/vault-index.md`。
