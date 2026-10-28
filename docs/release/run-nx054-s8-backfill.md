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
3. **DATA_HOST**：先按 `docs/OPERATIONS.md` §8a.11a 做授权差异核对（本单唯一预期的新增是 `relation_backfiller` 的 `nexloop.relation.backfill:1`；还有别的就交调度员确认），再 dry-run、正式执行。`$COMMON`、`$WINDOW` 与 `run-nx057-remediation-stage.md` §2 相同。

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
   | `grants_added` | 0 | ≥ 1（不写死，见下） |
   | `credentials_created` | `[]` | `["relation_backfiller"]` |
   | `grants_revoked` | 0 | 0 |
   | `extra_grants_outside_manifest` | 只能是模拟世界凭据（见下） | 同左 |

   - **`grants_added` 不写死**：它是清单中有、库里没有的授权（`missing_grants`），所以按定义都来自清单；上次 register 之后清单若还加过别的授权，这里会一并补上。stage 实测情况 B 为 3。核对：dry-run 的 `grants` 列表就是清单的全部授权；第 4 条的查询中 `relation_backfiller` 只有 1 行；其余新增项要能在 `git diff <上次 register 的提交> <本次 src> -- deploy/authorization/` 中找到。找不到就停下。
   - **`extra_grants_outside_manifest`**：库里有、但主体不在清单中的授权（stage 实测 6）。核对它们都是模拟世界的 `nexloop-sim-*` 凭据（由 sim_world 签发，不归 register 管）：

     ```
     ro "select entity_key[1], count(*) from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants'
         and payload->>'subject_kind'='service' and entity_key[1] not like 'nexloop-service-%' group by 1 order by 1"
     ```

     `subject_kind='service'` 不能省：库里还有人类主体（审批人、owner、customer-* 等，stage 上几百行），register 的 extra 只统计服务主体。每一行的主体都应以 `nexloop-sim-` 开头，计数合计等于 `extra_grants_outside_manifest`。出现别的主体就停下报告。register 不会撤销它们（只报告）。
   - 其余数字不符就停下：说明库里的授权与清单在这之前已有偏差。
   - 被拒为 `credential_rotation_required` 时，不要重试，停下报告。
4. **核对**：重跑第 1 条的查询，应当有一行，长度为 1。

## 第 7 步：`$BF`（APP_HOST）

镜像以 10001:10001 运行。`read_private_text` 和 libpq 都要求文件属主等于进程用户、权限 0600，所以 `$BF` 和其中的文件属主都是 10001。

```
BF=$(sudo -n mktemp -d /dev/shm/nexloop-backfill.XXXXXX)
S="$NEXLOOP_ROOT/secrets"
# 凭据：管理机经 scp 送到 APP_HOST 的中转副本 $CRED_COPY（0600），install 之后立即删除
sudo -n install -o 10001 -g 10001 -m 0600 "$CRED_COPY" "$BF/service-credential" && rm -f -- "$CRED_COPY"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/retention-keeper/pg-client.crt" "$BF/pg-client.crt"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/retention-keeper/pg-client.key" "$BF/pg-client.key"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/shared/db-ca.crt" "$BF/db-ca.crt"
sudo -n install -o 10001 -g 10001 -m 0600 "$S/shared/signing.key" "$BF/signing.key"
# DSN：只把三个证书路径改到 $BF，写入同一 tmpfs 上的临时文件，再 install 到位；内容不打印
TMPDSN=$(sudo -n mktemp /dev/shm/nexloop-bf-dsn.XXXXXX)
sudo -n sed -E -e "s#sslcert=[^ ]+#sslcert=$BF/pg-client.crt#" -e "s#sslkey=[^ ]+#sslkey=$BF/pg-client.key#" \
  -e "s#sslrootcert=[^ ]+#sslrootcert=$BF/db-ca.crt#" "$S/retention-keeper/dsn" | sudo -n tee "$TMPDSN" >/dev/null
sudo -n install -o 10001 -g 10001 -m 0600 "$TMPDSN" "$BF/dsn" && sudo -n rm -f -- "$TMPDSN"
sudo -n grep -c -E "sslcert=$BF/pg-client.crt|sslkey=$BF/pg-client.key|sslrootcert=$BF/db-ca.crt" "$BF/dsn"   # 预期 1（三项在同一行）
sudo -n grep -o -E "ssl(cert|key|rootcert)=[^ ]+" "$BF/dsn"   # 只看三条路径，都在 $BF 下
sudo -n chown 10001:10001 "$BF"
```

- 这里假定 DSN 是 libpq 的 `key=value` 形式（stage_secrets 生成的就是这种）。如果三条 grep 不符（例如是 URI 形式 `?sslcert=…&…`），停下，不要手工改。

- 这个主体在库里的角色是 `nexloop_domain_worker`，retention keeper 也是。用 retention keeper 的那一组 DSN 和证书。
- 凭据文件来自管理机的树，用 `scp` 送到 APP_HOST 的私有目录（中转副本 `$CRED_COPY`），`install` 之后立即 `rm`（见命令块）。
- **DSN**：由上面的 sed 改写 `$S/retention-keeper/dsn` 的三条证书路径，其余不变。
  - 容器里 `$BF` 挂在同一路径（`-v "$BF:$BF:ro"`），所以 DSN 中的路径在宿主和容器里都成立。
  - DSN 内容不进命令行、不打印。
- **通过条件**：`sudo -n ls -ln "$BF"` 中 6 个文件都是 10001、`-rw-------`。

## 第 8–13 步：回填（APP_HOST，release 52 的 backend 镜像）

```
test -n "$SIGNING_KEY_ID" || { echo 'SIGNING_KEY_ID 为空：停止'; exit 1; }   # 为空就停下，不用缺省值
APPNET=$(docker compose -p <应用 compose 项目名> ps -q app-net)
BFRUN="--rm --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=0700,uid=10001,gid=10001 \
  --network container:$APPNET -v $BF:$BF:ro --entrypoint python"
BFARGS="--database-url-file $BF/dsn --signing-key-file $BF/signing.key --signing-key-id $SIGNING_KEY_ID \
  --service-credential-file $BF/service-credential --artifact-root /tmp"
bf() { docker run $BFRUN "$BACKEND_IMAGE" -m nexloop_eios.relation_backfill $BFARGS "$@"; }
```

- `$BACKEND_IMAGE` 用 release 52 的 backend 镜像 digest，与服务正在运行的镜像相同。
- `$SIGNING_KEY_ID` 取应用 compose env 中的值。缺省值 `active` 不对。
- 网络：domain worker 服务都是 `network_mode: service:app-net`。这里用 `--network container:` 加入同一个网络命名空间，因此 APP_HOST 的 nft 规则（按 `APP_CONTAINER_CIDR` 匹配）照样放行到 DATA_HOST 的 PostgreSQL（TLS）。
- `/tmp` 的 `mode=0700,uid=10001,gid=10001` 不能省：`--artifact-root /tmp` 要求目录归运行账号所有、权限 0700，否则 runner 启动即拒绝（stage 实测）。
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
- **对照 preflight**：preflight 的 `report` 列出每个已发布关系（含 `with_value` 为 0 的），verify 只列有 hex 源值的组合，所以 verify 的关系数可以更少。差出来的关系在 preflight 里应当 `with_value`（或 `hex`）为 0；例如 stage 本次 preflight 22 个关系、verify 20 个，差的 `commitment_for_goal`、`supersedes` 两者 `with_value` 都是 0。差出来的关系若 `hex` > 0，就是 verify 漏行，停下报告。
- `dangling`（目标不存在）和 `erasing`（属于擦除中的 Consumer）照实记录，不计入 `a`，所以不影响平衡。

## 中止条件与 p95 的对照（第 9–13 步运行期间）

§8 的四条中止条件逐条照办。具体读法如下：

- **`waiters`、`event=paused`、`event=retry`**：在 tee 出来的 jsonl 上计数。
  - 连续 5 批 `waiters` > 3；
  - `paused` 持续超过 10 分钟；
  - 同一 cursor 上 55P03 / 40001 / 40P01 的 retry 累计超过 20 次。
- **`code=connection`**：重试 5 次后 runner 退出（非 0）。先 `bf status --run-id <run>` 再决定，不要直接重跑。
- **0323 写请求 p95**：
  - 读法：下面“0323 写类 p95 与 calls 的读取”的 5 分钟快照命令（metrics DSN，只读），读 `write.p95_ms` 与 `write.calls`。
  - 回填前的基线：第 9 步之前 15 分钟内读 3 次，取中位数。
  - 运行中：每 5 分钟读一次。比基线高出 25% 以上，或者超过 500 ms，就中止。
  - **写类 `calls` 在该 5 分钟窗口内少于 20 时，p95 不作判据。** stage 没有真实流量，样本太少，p95 只是噪声。这时只看前三条，并在证据里注明“p95 样本不足”。

## 第 5 步的判定（L6 2026-10-28 定）

以 `NX-054-stage-publish.md` §4 的门槛表为准：

- relay `goal_create`、`step_create`、`assignment_create`：stage 上无样本（`relation_derive_timing.py relay` 的输出里没有这三个键），记为“**无样本，不可判定**”，作为限制记录在案；P4b 上 real 之前按绝对值重测。
- (a) sim 生成墙钟、(b) 擦除 `sync_purge_seconds`：派生状态比空闲状态增加都不超过 25%。
- (c) 0323 写类 p95：发布前 24 h 与发布后 24 h 对比，增加不超过 10 ms。
  - 前提：两边写类 `calls` 都不少于 200。不足时记为“不可判定”，不阻塞。
  - 样本充足而增加超过 10 ms：判 FAIL，不回填，执行 §5 回退。
- relay `authenticate` 的 `p95_ms_median` 前后变化超过 10 ms 时，在 (c) 的结论里注明“环境漂移，结论打折”。

## 0323 写类 p95 与 calls 的读取（DATA_HOST，只读）

**5 分钟快照（metrics DSN）**：用于回填中的中止判据。`nexloop_metrics` 只能执行 `authz.nexloop_metrics_export()`，它返回的是当前 5 分钟窗口。`$COMMON`、`$RUN` 同 `NX-054-stage-publish.md` §0。

```
docker run $COMMON -v "$RUN/metrics.dsn:/run/metrics-dsn:ro" "$BACKEND_IMAGE" -c \
  "import json,psycopg; c=psycopg.connect(open('/run/metrics-dsn').read().strip(),autocommit=True); s=c.execute('select authz.nexloop_metrics_export()').fetchone()[0]; print(json.dumps(((s[0].get('api') or {}).get('classes') or {}).get('write')))"
```

- 输出形如 `{"calls": …, "p50_ms": …, "p95_ms": …, "error_rate": …, "slow_rate": …}`。
- API 段与租户、world 无关（进程级样本），所以取第一个快照即可。
- 输出 `null` 表示窗口内没有写请求。

**24 h 窗口（第 5 步 (c) 用）**：metrics DSN **读不到**。没有授予它按任意窗口读 API 样本的函数（0336 的 `nexloop_relation_relay_p95` 只读 `relay_step`），而 export 只有当前 5 分钟。所以改用 `run-nx057-remediation-stage.md` §0.3 的只读 owner 会话 `ro`：

- 经 bootstrap socket 登录，`begin read only`，`set local role nexloop_owner`，最后 rollback；
- 口令经 Keychain 管道，同该单。

按与快照相同的口径，合并窗口内各分钟样本的直方图，再取 0.95 分位：

```
ro "with s as (select sample from runtime.nexloop_process_samples where kind='api' and service='write'
      and recorded_at>='<since ISO>' and recorded_at<'<until ISO>'),
  h as (select e.key,sum(e.value::numeric) n from s cross join lateral jsonb_each_text(s.sample) e where e.key ~ '^h_[0-9]+\$' group by e.key)
 select json_build_object('calls',(select coalesce(sum((sample->>'calls')::numeric),0) from s),
  'samples',(select count(*) from s),
  'p95_ms',runtime.nexloop_histogram_quantile((select coalesce(jsonb_object_agg(key,n),'{}'::jsonb) from h),0.95))"
```

- 发布前：`<since>` 为发布前 24 h，`<until>` 为发布时刻；发布后：`<since>` 为发布时刻，`<until>` 为发布后 24 h。
- 输出第一行是 `set_config` 返回的租户 ID，第二行是结果。
- 样本保留 7 天，发布前的窗口现在仍可读。
- 这条查询只读、没有在 stage 上执行过；第一次请先用一个 5 分钟窗口试跑，与快照命令的结果对照。

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
