# 执行单：NX-059 审计哈希链在 stage 上的验收（带密钥校验一次，导出后离线校验一次）

NX-059 的审计哈希链（迁移 0320）已经在 stage 上运行，DATA_HOST 每 15 分钟封印一次。本单补做剩下的两项验收：

1. 在 DATA_HOST 上带密钥校验一次：库内校验，加上逐个核对封印的 HMAC；
2. 导出一次，在负责人管理机上离线校验。

两项由一条命令依次完成，命令只读一个配置文件。

- **执行者**：调度员。**判定者**：L6。证据里两者分开写。
- **按 main 575859f7（release 50）核对过**：
  - 工具 `scripts/ops/audit_chain.py`：子命令 `verify`、`export`、`verify-export`，参数见 §4；
  - 模块 `packages/eios-core/src/nexloop_eios/audit_chain.py`；
  - 迁移 `0320_audit_hash_chain.sql`：四张表、库内函数 `runtime.nexloop_audit_chain_verify(text)` 和 `runtime.nexloop_audit_chain_export(text,bigint,integer)`，都属 `nexloop_owner`，不授权给应用角色。
  - 私有文件读取 `packages/eios-core/src/nexloop_eios/private_configuration.py`（工具读密钥和 DSN 都经过它）。
  - main 前进后，第 0 步的前置检查会自动比对 DATA_HOST 上这三个文件的 sha256 与本机检出：不一致就停，不发送密钥。
- **入口**：`scripts/ops/audit_chain_stage_verify.py`（新增）。
  - 它在 DATA_HOST 的一次性后端容器里调用上面这个**未改动**的工具，所以不需要重新部署镜像；
  - 配置文件示例是 `deploy/examples/audit-chain-stage-verify.example.json`；
  - 测试是 `tests/test_audit_chain_stage_verify_pg.py`。
- 本单只读，不写库。导出只读链和源行，不会追加，也不会封印；封印仍由 DATA_HOST 的定时任务负责。

## 0. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | release 50（main 575859f7）已经在 stage 部署，迁移 0320 在库里 | 部署单；只读会话 `ro "select 1 from control.schema_migrations where version='0320'"`（`ro` 见 run-nx057 §0.3），返回 1 |
| P2 | 封印定时任务在跑：封印日志最后一行的时间不早于 20 分钟前（定时任务每 15 分钟一轮） | 在 DATA_HOST 上用 `crontab -l` 找到封印任务的输出位置，`tail -n 1` 看最后一行的时间。**不要用库里的 `max(sealed_at)` 判断**：链头没变（没有新的审计行）时，定时任务照常运行，但不写新的封印，`max(sealed_at)` 可能是十几个小时前。日志超过 20 分钟没有新行时，先查定时任务（§5），本单不代替它封印 |
| P3 | 密钥在管理机钥匙串里，条目 `nexloop/stage/audit-seal-key`，账户 `nexloop` | `security find-generic-password -a nexloop -s nexloop/stage/audit-seal-key >/dev/null && echo present`（不加 `-w`，不显示值） |
| P4 | DATA_HOST 上的 `$RUN/src` 是 575859f7 的打包，属主与 `$RUN/boot.dsn` 相同；`$RUN/boot.dsn` 是 `nexloop_bootstrap` 的本地 socket DSN，权限 0600，属主是部署用户（stage 上为 1000） | `stat -c '%u:%g %a' $RUN/boot.dsn`，预期 `<UID>:<GID> 600`。入口用同一条 `stat -c %u:%g` 读出属主，容器就以这个 UID:GID 运行（与其他数据主机脚本相同）；配置项 `run_user` 可覆盖。`$RUN/src` 的做法同 run-assembly §0.4；三个文件的 sha256 由入口在第 0 步自动比对 |
| P5 | 管理机有 575859f7 的检出，`python3` ≥ 3.12；离线校验只用标准库 | `git -C <检出> rev-parse HEAD` 为 575859f7；`python3 -c 'import sys;print(sys.version_info>=(3,12))'` 输出 True |
| P6 | 到 DATA_HOST 的 ssh 免交互，用平时部署的身份，该身份能运行 `docker run` | `ssh -o BatchMode=yes <data_host> docker version --format '{{.Server.Version}}'` 有版本号输出 |

## 1. 在哪台机器、用什么身份、密钥怎么走

| 步骤 | 机器 | 身份 | 密钥 |
|---|---|---|---|
| 0 前置：比对 DATA_HOST 上的工具、模块和私有文件读取 | DATA_HOST，一次性容器 | 容器以运行用户（`$RUN/boot.dsn` 的属主，或 `run_user`）运行；不连库 | 不用 |
| 1 带密钥校验 | DATA_HOST，一次性容器：`--network none --read-only --user <UID:GID> --cap-drop ALL`，只读挂载 `$RUN/src`、PG socket 目录、`$RUN/boot.dsn` | 库：`nexloop_bootstrap`（超级用户，经 PG 容器本地 socket），与封印定时任务和其他数据主机工具相同 | 钥匙串 → 管道 → 入口进程 → ssh 的 stdin → 容器的 stdin → 容器 tmpfs 中的 `/tmp/seal.key`（0600，属主为运行用户）。校验结束就删除，不落 DATA_HOST 的磁盘，不出现在命令行参数里 |
| 2 导出 | DATA_HOST，同样的容器，另加一个 tmpfs `/export` | 同上 | 不用 |
| 3 离线校验 | 负责人管理机 | 本机用户 | 写进本机私有临时目录中的文件（0600），进程结束时删除 |

- 导出先写进容器的 tmpfs，再以 tar 流经 ssh 传回管理机，放进配置里 `out_dir` 下的一个新目录（0700）。DATA_HOST 上不留任何文件。
- 导出里有审计行的原文，是私有材料：`out_dir` 必须在 `local-only/` 下，不进仓库，不进公开证据。证据只写摘要（§3）。

## 2. 执行

### 2.1 配置文件（只做一次）

复制 `deploy/examples/audit-chain-stage-verify.example.json` 到 `local-only/` 下，填写各字段：

| 字段 | 含义 |
|---|---|
| `data_host` | ssh 主机别名（`~/.ssh/config` 里的名字，不写地址） |
| `run_dir` | 部署单的 `$RUN` |
| `nexloop_root` | 部署单的 `$NEXLOOP_ROOT`；PG socket 在 `$NEXLOOP_ROOT/run/postgresql` |
| `backend_image` | release 50 的后端镜像，必须带 digest（`name@sha256:…`），与发布清单一致 |
| `tenant_id` | stage 租户 |
| `out_dir` | 管理机上已有的私有目录，属主是本人，权限 0700。解析符号链接之后，必须在检出根目录的 `local-only/` 下，或者在检出之外（例如 `~/.nexloop/` 下）。工作区里其他会被 git 跟踪的路径一律拒绝（入口的 `load()` 会检查） |
| `run_user`（可选） | 容器运行用户 `UID:GID`（两个数字）。不填时入口在 DATA_HOST 上用 `stat -c %u:%g $RUN/boot.dsn` 读属主，stage 上是部署用户。只有属主不是该用的身份时才填；示例文件里不写 |

必填字段缺一个、多出不认识的字段、`run_user` 不是 `UID:GID`，或者镜像不带 digest，入口都会拒绝（退出码 2）。

### 2.2 先看计划（A：只读，不读密钥，不连接任何机器）

```bash
python3 scripts/ops/audit_chain_stage_verify.py --config local-only/ops/audit-chain-stage-verify.json --dry-run
```

预期输出：先是 1 行 `{"step":"plan","host":"DATA_HOST","command":"stat -c %u:%g <run_dir>/boot.dsn"}`（读运行用户；配置了 `run_user` 时没有这一行），再是 3 行 `{"step":"plan","host":"DATA_HOST","command":"docker run -i --rm --network none --read-only --user UID:GID …"}`（演练时属主还没读，以 `UID:GID` 占位；配置了 `run_user` 时就是该值），依次是前置、校验、导出，最后一行 `{"step":"plan","host":"local",…}`，退出码 0。

核对：
- 三条命令都带 `@sha256:` 镜像；
- 都只读挂载 `…/boot.dsn:/run/bootstrap-dsn:ro`；
- 第二条带 `--key-file /tmp/seal.key`，但命令里没有任何密钥内容。

### 2.3 执行（A：只读）

```bash
security find-generic-password -a nexloop -s nexloop/stage/audit-seal-key -w \
  | python3 scripts/ops/audit_chain_stage_verify.py --config local-only/ops/audit-chain-stage-verify.json
```

在管理机上，从 575859f7 检出的根目录执行。密钥只经管道传入，不能先存进变量或文件再传；从终端直接运行而没有管道时，入口会拒绝。

### 2.4 预期输出（每步一行 JSON，只有代码、数字和 key_id）

| 行 | 预期 |
|---|---|
| `{"step":"run_user","user":"UID:GID","source":"boot.dsn owner"}` | 与 P4 读到的属主相同；配置了 `run_user` 时 `source` 为 `configuration` |
| `{"step":"preflight","ok":true,"files":[…]}` | 三个文件与本机检出相同 |
| `{"step":"verify","tenant_id":…,"valid":true,"entries":N,"pruned":P,"pending":Q,"head_seq":H,"seals":S,"problems":[]}` | `valid` 为 true；`problems` 为空；`head_seq = entries = N`；`seals` ≥ 1。`pending` 可以大于 0：这是上次封印以后才写入的审计行，下一轮定时任务会把它们追加进链。`pending` 为 0 时库里最近一次封印可能很旧（链头没变就不写新封印），这正常，不影响判定 |
| `{"step":"export","directory":…,"entries":N2,"head_seq":H2,"files":{…}}` | `N2 ≥ N`、`H2 ≥ H`（两步之间定时任务可能追加了新行） |
| `{"step":"offline","tenant_id":…,"valid":true,"entries":N2,"pruned":P2,"seals":S2,"problems":[]}` | `valid` 为 true；`S2 ≥ S` |
| `{"step":"summary","valid":true,…,"agreement_problems":[]}` | 退出码 0 |

## 3. 证据（写进 NX-059 的任务证据，以及相关 AT；不含密钥、DSN、主机地址和导出原文）

- 执行者（调度员）：执行时间（UTC），main 575859f7，release 50；
- 校验那一行的 `valid`、`entries`、`pruned`、`pending`、`head_seq`、`seals`；
- 离线校验那一行的 `valid`、`entries`、`seals`；
- summary 那一行；
- `manifest.json` 中 `files` 的两个 sha256，以及导出目录在 `local-only/` 下的路径（只写路径，不附内容）。
- 判定者（L6）：对照 §2.4 判定 passed 或 failed。不一致时写明是 §5 的哪一种情况。

## 4. 本单用到的工具参数（按 575859f7）

| 调用 | 参数 | 输出与退出码 |
|---|---|---|
| `audit_chain.py verify` | `--database-url-file /run/bootstrap-dsn --key-file /tmp/seal.key --tenant <T>` | 每个租户一行 JSON；0 有效，1 发现问题，2 用法或连接错误（stderr 只写异常类名） |
| `audit_chain.py export` | `--database-url-file /run/bootstrap-dsn --tenant <T> --out /export/audit-export`（目录必须是新的） | manifest 一行（入口把它转到 stderr，stdout 只用来传 tar） |
| `audit_chain.py verify-export` | `--key-file <私有临时文件> --dir <导出目录>` | 一行 JSON；0 / 1 / 2 同上 |

工具读取密钥和 DSN 文件时，要求文件是普通文件、属主为当前进程、权限 0600、不是符号链接（`private_configuration.read_private_text`），所以不能直接把 `/dev/stdin` 当作密钥文件。入口先在容器的 tmpfs 里以运行用户建文件，再把路径交给工具。

## 5. 失败时怎么判断

| 现象 | 含义 | 处理 |
|---|---|---|
| 退出码 2，`preflight … release_tree_differs` | DATA_HOST 的 `$RUN/src` 不是 575859f7（至少这三个文件中有一个不同） | 按 P4 重新打包，或者用匹配的检出重跑；这时还没有发送密钥 |
| 退出码 2，`… release_tree_unreadable` / `verify_unavailable` / `export_unavailable`，`error` 是异常类名或 docker 的报错 | ssh、docker 或连库失败（例如 `OperationalError` 表示 socket 或 DSN 有问题，`PermissionError` 表示 DSN 文件属主或权限不对） | 按 P4、P6 查。不是链的问题，不判 failed，修好后重跑 |
| 导出步骤报 tmpfs 挂载错误 | 该 docker 版本不支持 tmpfs 的 `uid=`/`gid=` 选项 | 记下 docker 版本交回 L2；不要改成在 DATA_HOST 磁盘上导出 |
| `verify` 的 `problems` 中有 `link_broken`、`sequence_gap`、`head_mismatch` | 链本身被改过（条目被删、插入或重算） | 判 failed。停止，保留现场，报负责人 |
| `record_altered` / `record_deleted` / `record_unchained` | 源审计行被改、被删（又没有保留标记），或者有行绕过了触发器 | 判 failed，报负责人。`event_key` 和 `source` 指明是哪一行 |
| `seal_invalid` | 封印的 MAC 用当前密钥算不对：密钥不对，或者封印被伪造 | 先核对钥匙串中的密钥与 DATA_HOST 上 `~/nx-audit/seal.key` 的 key_id（`sha256(key)` 的前 16 位，两边各算一次，只比较这 16 位）。一致时判 failed，报负责人 |
| `seal_key_unknown` | 封印所用的 key_id 不在传入的密钥里：密钥轮换过，而钥匙串条目只有新密钥 | 把旧密钥按“第一行是当前密钥、其后是旧密钥”补进钥匙串条目后重跑；补不上时判 blocked |
| `seal_mismatch` | 封印的位置上，链里现在的 link 与封印记录的不同：封印之后链被重算过 | 判 failed，报负责人 |
| `offline` 中出现 `file_altered` | 导出文件在传输或落盘后变了 | 重跑一次；仍然出现就判 failed |
| `agreement_problems` 含 `export_behind_verified_head` 或 `export_has_fewer_seals` | 导出比刚校验过的库还旧：链或封印在两步之间变少了 | 判 failed，报负责人 |
| P2 不满足：封印日志最后一行早于 20 分钟前 | 封印定时任务停了 | 查 DATA_HOST 的 `crontab -l` 和任务输出，修好并等下一轮运行后再执行本单。本单不手工封印。只是 `max(sealed_at)` 很旧而日志在更新，不算不满足：链头没变时不写新封印 |
| `audit chain stage verify: the owner of run_dir/boot.dsn on DATA_HOST could not be read` | `run_dir` 写错，或 ssh 身份读不到 `$RUN/boot.dsn` | 核对 `run_dir` 与 P4；确需用别的 UID:GID 时在配置里写 `run_user` |

## 6. 回退

本单只读，没有需要回退的写入。执行完把 `out_dir` 中的导出目录留在 `local-only/` 下作为私有证据；判定之后是否删除，由负责人决定。

## 7. 局限

- **管理机上的密钥临时文件可能残留**：离线校验时，密钥写在 `tempfile.TemporaryDirectory()` 下的 `seal.key`（0600，只有本人可读）。入口正常结束、出错或被 Ctrl-C 中断时都会删除；但如果进程被 SIGKILL（例如 `kill -9`、强行关闭终端会话），这个目录会留下。执行后可以检查系统临时目录里有没有 `tmp*/seal.key`，有就删除（`rm -P` 或 `shred -u`）。
- **tmpfs 可能被换出到 swap**：DATA_HOST 上容器的 `/tmp` 和 `/export` 是 tmpfs，内存紧张时其中的页（包括密钥文件和导出的审计行原文）可能被换出到 swap，留在磁盘上。本单不解决这个问题；如果 DATA_HOST 的 swap 没有加密，执行本单就要接受这项风险。
- **docker 段未在本机实测**：测试是在本机直接运行容器里的脚本，没有经过 docker 和 ssh。tmpfs 的 `uid=`/`gid=` 选项在第 0 步前置检查时会首先用到（`/tmp`）；导出用的 `/export` 若报挂载错误，按 §5 处理。
