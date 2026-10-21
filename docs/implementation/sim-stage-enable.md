# 模拟进程组在 stage 上启用（发布 27 之后，AT-046 实测）

- **依据**：负责人批准“发布 27 部署后，在 stage 上启用模拟进程组，实测 AT-046”。参考 L6 的演进登记做法（`evolution_enroll.py`、`evolution_hba_data_host.sh`）。
- **适用发布**：第七批 r4（1bd26ba1，发布 27）。twin 用途和孪生端口属于第八批，见“第八批之后”；`nexloop_twin_port` 角色在 0266 已存在，第九批起放进 pg_hba 的模拟行。
- **原则**：
  - 模拟进程组只用模拟角色和该 world 自己的凭据；
  - 主机配置（root、防火墙、路由）只写成负责人命令；
  - 所有私有值放在私有配置里，口令只走 stdin 或钥匙串管道。

## 网络与 pg_hba 的设计

- **路径**：模拟容器在 APP_HOST 的 `nexloop-simulation` 网桥（`NEXLOOP_SIM_SUBNET`）上。出主机时和 APP_HOST 上其他容器网段一样做 masquerade（参照 claim、effect 的出站命名空间），所以到达 DATA_HOST 时源地址是 `APP_HOST_ADDR`。
- **“只放行模拟子网”在哪一层做**：在 APP_HOST 的 `nexloop_simulation` 表做（`NX-041-S6-simulation.nft.example`）：
  - 模拟子网只能新建两种连接：到 DATA_HOST 的 PostgreSQL 端口，以及到模型白名单的 443；
  - 其余新建连接一律丢弃，包括访问 APP_HOST 本机的。
  - 模拟子网只有在 APP_HOST 上才看得到，所以这条限制只能放在这里。
- **pg_hba 新增一行**：
  `hostssl <stage 库> nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api,nexloop_stand_in_worker,nexloop_sim_files,nexloop_twin_port <APP_HOST_ADDR>/32 scram-sha-256`
  - 只走 TLS，只用 SCRAM，只能连 stage 库，不要客户端证书。
  - 这一行没有任何 real 角色；real 应用角色那一行里也没有模拟角色。
  - 从第九批起，这一行包含 `nexloop_twin_port`（角色在 0266 已建，第八批带来了孪生端口和它的 DSN），用于 AT-046 接收步骤中的孪生。它的口令在 bootstrap 窗口设置，没有口令就无法登录。如果 stage 上已经装过不含它的旧模拟行，`sim_hba_data_host.sh` 的预检只允许一种差异：删掉这条旧行、换成新行；其他任何删除都会拒绝。
- **与你原先要求的差异**：你要求“只放行模拟子网”，我没有让 pg_hba 直接看到模拟子网的源地址。那样需要关闭该网段的 masquerade，并在 DATA_HOST 加回程路由，改动面大，也与现有容器网段的做法不一致。所以我把源地址限制放在 APP_HOST 防火墙，pg_hba 只按角色、库和 SCRAM 限制。如果仍要在 DATA_HOST 侧看到模拟子网，需要另外做一次网络改造（关闭该网段 masquerade、加 DATA_HOST 回程路由、DATA_HOST 防火墙放行模拟子网），这次没有做。

## 步骤与执行人

| # | 步骤 | 执行人 | 主机与连接 | 命令或工具 |
|---|---|---|---|---|
| 1 | 部署发布 27（含本分支的模板和工具） | 调度员 | APP_HOST 和 DATA_HOST；迁移在 DATA_HOST 走 socket | 部署单 |
| 2 | pg_hba：加模拟一行，可与演进 local 行一起，一次重新加载 | **负责人，DATA_HOST root，一次** | DATA_HOST；不连数据库（改文件后发 HUP） | `sudo bash scripts/deploy/sim_hba_data_host.sh <私有配置>`。配置是 0600 的 KEY=VALUE：`NEXLOOP_ROOT`、`STAGE_ENV`、`RELEASE_CHECKOUT`。脚本要求新渲染的 pg_hba 只比现有的多出模拟行，而且恰好一条（可再多一条演进行，同一行出现两次不算两行），不能删任何行；原地改写后发 HUP，失败就写回原内容。之后不必再跑 `evolution_hba_data_host.sh` |
| 3 | 模拟角色的口令（bootstrap 窗口），口令存入钥匙串 | 调度员 | DATA_HOST；bootstrap 走 socket | `ALTER ROLE … PASSWORD` 在 bootstrap 窗口执行，不经过任何工具 |
| 4 | 模拟 secret 目录与 DSN 文件 | 调度员（APP_HOST 的部署账户，有 docker 权限，不用 sudo） | APP_HOST；写文件不连数据库，`--check` 以模拟角色走 hostssl | 先建目录：`docker compose -p nexloop-simulation … --profile sim-setup run --rm sim-setup-dir`，建出 secret 和 token 两个目录，0700，属主为 `NEXLOOP_SIM_UID`。再在一次性容器里以该 UID 运行工具：`… --profile sim-setup run --rm -T sim-setup scripts/deploy/sim_dsn_files.py --config /setup/<配置> --dry-run`；然后去掉 `--dry-run`，用钥匙串管道 `printf '%s %s\n' <角色> <口令>`（每个角色一行）从 stdin 喂进去。配置里的 `secret_dir` 写 `/sim-secrets`。会写 6 个 `*_dsn` 文件（0600，第九批起包括孪生端口的 `sim_twin_port_dsn`），各自的 `sslrootcert=/run/secrets/sim_pg_ca`，文件从一开始就属于该 UID |
| 5 | 其余 secret 文件，以及两把密钥的数据库登记 | 调度员 | 5a：**APP_HOST**，写文件，不连数据库；5b：在**调度员的机器上**发起一条管道 `ssh APP_HOST … \| ssh DATA_HOST …`，DATA_HOST 走 **socket**（bootstrap，以及由它打开的 configurator 窗口）；两台主机之间不建立 ssh 信任 | **5a**：`… --profile sim-setup run --rm -T sim-setup scripts/deploy/sim_secret_files.py --config /setup/<配置> --dry-run`，再去掉 `--dry-run` 执行（配置见 `deploy/examples/sim-secret-files.example.json`）。在 `/sim-secrets` 写出下一节的全部文件（DSN 和 world 凭据除外），每个 0600、只在缺少时写，重复执行什么都不改；在 `/sim-tokens/sim.env.draft` 写私有 env 草稿。输出只含文件名、key id、host id 和两把密钥的 sha256，不含任何值。**5b**：把 5a 输出的 `registration` 中两个 sha256 填进登记配置的 `expect`（`deploy/examples/sim-secret-register.example.json`），先在 DATA_HOST 的 sim-issue 容器里执行 `sim_secret_register.py --config /setup/<配置> --dry-run`，然后在调度员的机器上执行 `bash scripts/deploy/sim_secret_registration_transfer.sh <私有配置>`。签名密钥和 purge key 都是**对称密钥**，数据库保存的就是密钥本身，所以登记必须拿到密钥：密钥只在这条管道中经过，DATA_HOST 先核对 sha256 与 APP_HOST 的一致，再登记（签名密钥经受信配置，purge key 由 bootstrap 写入 `authz.nexloop_sim_hosts`），最后自检；之后可以用 `--check` 单独复查 |
| 6 | 创建 sim world、生成合成数据、发布该 world 的 Action 目录 | 调度员（工具以负责人的工作台会话执行） | APP_HOST 或其他能访问工作台的执行机；不连数据库（工作台 HTTPS） | `at046_stage.py prepare --config …`，负责人口令走 stdin。依次调用 create_sim_world、generate_sim_world_data、publish_sim_catalog（0294，见下），输出里有 world id。也可以单独用 `sim_world.py publish-catalog --config …` 发布目录 |
| 7 | 签发该 world 的凭据并自检 | 调度员（受信配置签发：configurator 登录窗口和签名密钥） | **DATA_HOST**；configurator 和 bootstrap 检查角色都走 **socket**（它们在 pg_hba 里只有 local 规则；不加 hostssl） | 在 `deploy/stage/compose.sim-issue.example.yaml` 的一次性容器里运行（以 `NEXLOOP_PG_UID` 运行，即 socket 目录的属主 uid，当前 postgres 镜像为 999；挂载 socket 目录，无网络）。先 `… --profile sim-issue run --rm sim-issue-dir`，把 token 目录和私有输入目录（配置、两个 DSN 文件、签名密钥）设为 `NEXLOOP_PG_UID` 的 0700/0600。第一次执行前，用 `stat -c '%u:%g' $NEXLOOP_ROOT/run/postgresql` 核对 uid，用 `getent passwd <uid>` 查看宿主机上有没有同 uid 的账户（如果有，它也能读输入目录，需要评估）；再执行 `… run --rm -T sim-issue scripts/deploy/sim_world_credentials.py --config /setup/<配置> --dry-run`，然后去掉 `--dry-run` 签发。两个 DSN 都必须是 `host=/var/run/postgresql …`，工具会拒绝 TCP 形式的 DSN。配置中 `token_dir` 写 `/sim-tokens`，不写 `compose_secret_dir`。签发后自检（见“凭据自检”）；`check_database_url_file` 是 bootstrap 窗口的只读检查 DSN（`default_transaction_read_only=on`） |
| 8 | 把凭据安装到 APP_HOST | 调度员 | **在调度员的机器上**发起一条管道，分别 ssh 到两台主机：DATA_HOST 导出（不连数据库）→ 调度员机器上的管道 → APP_HOST 安装（不连数据库）。**两台主机之间不建立任何 ssh 信任**（L6 at046-split 复核 2.2：DATA_HOST 上有 docker 权限就等于有 root，所以不能让 APP_HOST 能 ssh 到它） | `bash scripts/deploy/sim_credentials_transfer.sh <私有配置>`，在调度员的机器上执行。私有配置是 0600 的 KEY=VALUE：`DATA_HOST_SSH`、`APP_HOST_SSH`（两台主机在调度员 ssh 配置中的别名，二者必须不同）、`DATA_RELEASE`、`DATA_ENV`、`ISSUE_CONFIG`、`APP_RELEASE`、`APP_ENV`、`INSTALL_CONFIG`。脚本只用一条管道完成传输：`ssh DATA_HOST '…sim-issue … --export' \| ssh APP_HOST '…sim-setup … --install'`。token 只在调度员机器的管道内存中经过，不进终端、日志或参数，任何主机上都不落中间文件。安装成功后，脚本再 ssh DATA_HOST 执行 `--forget`；安装失败时副本保留，脚本以 1 退出。`--export` 只在脚本设置的 `NEXLOOP_SIM_TRANSFER_PIPE=1` 下才运行，用来防止手工单独执行，**不是安全边界**（L6 复核 2.1）。安装配置（`nexloop-sim-credentials-install/1`）只有 `world`、`uses`、`token_dir`（`/sim-tokens`）和 `compose_secret_dir`（`/sim-secrets`），写出的文件属于 `NEXLOOP_SIM_UID` |
| 9 | 防火墙 | **负责人，APP_HOST root** | APP_HOST；不连数据库 | 见“防火墙”一节 |
| 10 | 启动模拟进程组 | 调度员（APP_HOST 的部署账户，有 docker 权限） | APP_HOST；各模拟角色走 hostssl | 见“启动”一节 |
| 11 | 自检模拟角色 | 调度员 | APP_HOST；模拟角色走 hostssl | `post_deploy_check.py … --sim-secret-dir <模拟 secret 目录>`，在模拟项目的一次性容器里执行，或者 `sim_dsn_files.py --check`。每个 DSN 必须以它自己的模拟角色登录 |
| 12 | AT-046 探针（之前） | 调度员 | **DATA_HOST**；只读探针走 socket（bootstrap 只读 DSN） | 在 `sim-issue` 一次性容器里运行 `at046_stage.py probe-before --config …`（配置里只填 `roles.probe`；容器里只有 `/tmp` 和 token 目录可写，所以 `results_dir` 设为 `/tmp`，结果从 stdout 重定向保存为文件）。会记录 real 侧摘要，并检查 synthetic Consumer 和该 world 的 Run Action。它会生成一个 run nonce 写进结果，后面三步都靠它确认属于同一次运行 |
| 13 | AT-046 接收 | 调度员 | **APP_HOST**；`sim_api` 和 `twin_port` 走 hostssl（模拟一次性容器，不用探针） | 先把步骤 12 的结果文件（只含摘要、id、计数和 nonce，不含 token 或 DSN）拷到 APP_HOST 的 `NEXLOOP_SIM_SETUP_INPUT_DIR`，容器里只读挂载为 `/setup`。再执行 `… --profile sim-setup run --rm -T sim-setup scripts/acceptance/at046_stage.py intake --config /setup/<配置> --from /setup/<probe-before 结果>`。配置里只填 `roles.sim_api`，以及有孪生时的 `roles.twin_port`（DSN 用 `/sim-secrets/sim_twin_port_dsn`）；`results_dir` 设为 `/tmp`（容器里可写的只有 `/tmp`、`/sim-secrets`、`/sim-tokens`），结果从 stdout 重定向保存为文件。依次执行 twin、签发 Run 凭据、登记 sim context、accept。直接连数据库，不经过工作台 |
| 14 | AT-046 探针（之后） | 调度员 | **DATA_HOST**；只读探针走 socket | 先把步骤 13 的结果文件拷到 DATA_HOST 的 `NEXLOOP_SIM_ISSUE_INPUT_DIR`（容器里是 `/setup`），重跑 `sim-issue-dir` 改好属主，再执行 `at046_stage.py probe-after --config /setup/<配置> --from /setup/<probe-before 结果> --from /setup/<intake 结果>`，`results_dir` 设为 `/tmp`。它会等 task 结束，再核对 Run 凭据的 world、该 world 的凭据，以及 real 侧摘要是否不变 |
| 15 | AT-046 合并 | 调度员 | APP_HOST 或 DATA_HOST 均可；不连数据库 | `at046_stage.py merge --config … --from <probe-before> --from <intake> --from <probe-after>`，合成一份 `at046-run` 结果，作为 evidence。合并会核对：三份结果正好依次是这三步，nonce、world、tenant 一致，执行时间先后正确，必须出现的步骤都在。任何一项不满足、任何一步失败，整体就是失败，不会被 blocked 掩盖；只有 blocked 而无失败时，整体为 null（L6 at046-split 复核 1.1） |

## 凭据自检（`sim_world_credentials.py`，L6 评审 4.3）

逐项输出，每项都要为真：

- 本 world 的凭据：每个配置的用途恰好一个 active 凭据，没有其他凭据；
- 每个凭据：`worlds` 恰好是 `[world]`，`sim_use` 等于该用途，状态 active，它的 principal 的授权集合等于最小清单（api：队列和效应 Action；worker：队列；agent：效应 Action；都只有 execute）；
- 在本 world 能认证；在 real 和 test 被拒，并且 SQLSTATE 必须是 42501，其他错误不算“被拒”；
- 正对照：该租户一个 active 的 real 凭据在 real 能认证。没有它，整体判失败（否则“被拒”可能只是调用本身坏了）。

效应 Action 只授给模拟 world 的 principal，这是“效应 Action 不经 manifest 授权”的唯一例外（评审 4.2）。工具在生成任何授权之前断言 world 是 `sim:` 或 `shadow:`，数据库再把凭据绑定到这个 world。每个 world 的 principal 和凭据 id 由 world uuid 派生，所以不同 world 的凭据不会互相复用（评审 4.4，不改代码）。

## AT-046 的 real 侧对照（评审 5.2）

`real_side_unchanged` 不再只比几张表的行数。它对 `authz`、`control`、`runtime`、`ontology` 中所有带 `world` 列的基表，取该租户 world='real' 的全部行（没有 `tenant_id` 列的表取整表的 real 行）算摘要，前后对比；所以更新也能看出来。表的数量写在结果里，为 0 时判失败。测试里有正对照：改一行 real 定义，摘要就变。

## compose 需要的 secret 文件（`NEXLOOP_SIM_SECRET_DIR`，全部 0600，属主为 `NEXLOOP_SIM_UID`）

文件的属主（L6 评审 B2）：两个目录建好后属于 `NEXLOOP_SIM_UID`，部署账户自己读不了，要查看或备份也得再起一次性容器，这是预期的效果（评审 B2.2）。DATA_HOST 侧（步骤 7）的 token 目录和输入目录属于 uid 999，私有 env 需要 `NEXLOOP_ROOT`、`NEXLOOP_SIM_RELEASE_CHECKOUT`、`NEXLOOP_SIM_ISSUE_INPUT_DIR`、`NEXLOOP_SIM_ISSUE_TOKEN_DIR`。APP_HOST 上写文件的两步（步骤 4 的 `sim_dsn_files.py` 和步骤 8 的 `--install`）都在 compose 的 `sim-setup` 一次性容器里以 `NEXLOOP_SIM_UID` 运行，所以写出的文件一开始就属于这个 UID，不需要 sudo chown。目录由 `sim-setup-dir` 建出：它是唯一以 root 身份运行的步骤，只有 CHOWN 一项能力，并且没有网络。这两个服务只属于 profile `sim-setup`，`--profile simulation` 不会启动它们。写完后用 `stat` 确认 0600 和属主。工具本身仍然不改属主，也不以 root 运行。

| 文件 | 来源 |
|---|---|
| `sim_worker_dsn`、`sim_guard_dsn`、`sim_stand_in_worker_dsn`、`sim_files_dsn`（以及 `sim_api_dsn`，供 AT-046 的接收步骤使用） | `sim_dsn_files.py` |
| `sim_pg_ca` | `sim_secret_files.py` 从 `real_pg_ca_file` 复制（real 的 pg CA bundle，同一套服务器证书） |
| `sim_world_credential` | `sim_world_credentials.py` 写入（worker 凭据） |
| `sim_signing_key` | `sim_secret_files.py` 生成：32 个 URL 安全的 ASCII 字符（服务正好读取 32 字节）。key id 是 `signing_key_id`（必须以 `sim-` 开头），同时也是 `NEXLOOP_SIM_SIGNING_KEY_ID`。由第 5b 步经受信配置登记到 `authz.nexloop_authority_signing_keys` |
| `sim_host_cert`、`sim_host_key`、`sim_host_ca`、`sim_guard_ca`、`sim_guard_certificate`、`sim_guard_tls_key` | `sim_secret_files.py` 调用 `issue_certs` 签发。模拟 CA 放在 `ca_dir`（不能放在 secret 目录内），首次执行时自建根 CA 和中间 CA，与 real 的 CA 完全分开。Host 和 guard 共用 loopback，所以 SAN 是 `IP:127.0.0.1`；两个 `*_ca` 文件都是模拟 CA 的 bundle |
| `sim_host_transport_key`、`sim_host_guard_key`、`sim_host_purge_key` | `sim_secret_files.py` 生成：各 32 字节随机数，写成 64 位 hex。purge key 由第 5b 步登记到 `authz.nexloop_sim_hosts`（`host_id`） |
| `sim_host_runtime_config`、`sim_host_model_configuration` | `sim_secret_files.py` 生成。运行时配置包含：`world_group: simulation`、`context_input_protocol: nexloop.context-pack.sim.v1`、`host_id`、`world_purge_key_file`、guard 地址 `127.0.0.1:<guard_port>`，以及 `runtime_profile: deterministic-test`（D6，确定性档位）。在这个档位下，Host 不读取模型配置，也不允许配置 `model_configuration_file`，所以模型配置文件只是确定性档位的标记，留着它是因为 compose 会挂载这个文件 |

私有 env 还需要：`NEXLOOP_SIM_UID`、`NEXLOOP_SIM_GID`、`NEXLOOP_SIM_SUBNET`、`NEXLOOP_SIM_HOST_PORT`、`NEXLOOP_SIM_GUARD_PORT`、`NEXLOOP_SIM_HOST_ID`、`NEXLOOP_SIM_SIGNING_KEY_ID`、`NEXLOOP_SIM_RUNTIME_DIR`、`NEXLOOP_SIM_ARTIFACT_DIR`、`NEXLOOP_SIM_SECRET_DIR`、`NEXLOOP_SIM_TOKEN_DIR`、`NEXLOOP_SIM_RELEASE_CHECKOUT`、`NEXLOOP_SIM_SETUP_INPUT_DIR`（步骤 4 和 8 的私有输入，只读挂载）、`NEXLOOP_SIM_WORLD`（步骤 6 得到的 world id），以及与 real 相同的镜像 digest。

`sim_secret_files.py` 会写出一份草稿（`env_draft_file`），列出 compose 读取的全部 `NEXLOOP_SIM_*` 变量（外加两个镜像变量）。草稿里已经填好配置中给出的值，以及 host id、签名 key id、guard 端口；其余写成 `<…>` 占位，用之前逐项补齐。草稿只在文件不存在时写出。

## 防火墙（负责人，APP_HOST root）

1. 从 `docs/implementation/NX-041-S6-simulation.nft.example` 生成私有文件，手工替换占位符：`__SIM_SUBNET__`、`__DATA_HOST_ADDR__`、`__PG_PORT__`，以及模型白名单 `__MODEL_PROVIDER_ALLOWLIST_ALL__`（只用确定性 Host 时删掉这一行）。
2. 先检查语法：`sudo nft -c -f <私有文件>`。
3. 应用：`sudo nft -f <私有文件>`。撤销用 `sudo nft delete table inet nexloop_simulation`。
4. 部署单要求的探测：启动之后，从模拟容器连一个 real Host 端口，应当被拒；连 DATA_HOST 的 PG 端口，应当能通。

DATA_HOST 的防火墙不用改：源地址仍然是 `APP_HOST_ADDR`，见上面的设计说明。

## 启动（调度员，APP_HOST）

```bash
docker compose -p nexloop-simulation -f deploy/stage/compose.simulation.example.yaml --env-file <私有模拟 env> config -q
docker compose -p nexloop-simulation -f deploy/stage/compose.simulation.example.yaml --env-file <私有模拟 env> --profile simulation up -d
docker compose -p nexloop-simulation ps
```

停止和清理用 `--profile simulation down`。world 的整体删除走 `sim_world.delete`，不靠 compose。

## 模拟 world 的 Action 目录（0294，`sim_world.publish_catalog`）

- **入口**：负责人的受治理 Action `nexloop.sim_world.publish_catalog`（operation `publish_sim_catalog`），只能从 real 发起；命令行是 `sim_world.py publish-catalog`。
- **复制什么**：把 real 已发布、并且在 S4 替身目录（`control.nexloop_stand_ins`）里有替身的 Action 定义，复制到一个已登记、处于 active 的 sim world。没有替身的效应（例如 enterprise delivery）一律不复制，按 D11 失败关闭。
- **不复制什么**：凭据、授权事实、效应类别声明、任何配置都不复制。
- **可重复执行**：已存在的定义只计数，不重写；同一版本但内容不同，判为 `catalog_conflict`。
- **随 world 删除**：这些行在 world 整体删除时一起删掉。
- **数据库拦截（L6 评审 A1）**：sim/shadow 的 Action 定义上有触发器 `nx041_sim_definition_guard`：任何 update 都拒绝（42501）；delete 只放行 S2 整体删除（经由擦除范围）；只要还有这类行，truncate 也拒绝。所以动态 SQL、`on conflict do update`、以后迁移里的 DML 都会在执行时被拒。写入点扫描测试保留，作为写入点清单的补充；它只扫函数体，迁移里的 DML 是它的盲区，这正是要加触发器的原因。
- **真实执行被拦住**：在 sim world 里，效应只能走替身路径。real 的效应表由 0262 的 `nx041_real_effect_world` CHECK 拒绝任何模拟行。
- **authority revision 的取舍**：受治理入口在提交前会复核租户的 authority revision。为此，模拟 world 的 Action 定义写入不再递增这个 revision（重定义了 0004 的 `authz.nexloop_authority_epoch_guard`，已在复制件守护登记）。这些定义只服务于该 world 自己的凭据；real、test 和其他所有授权行的变化仍然照常递增。

## AT-046 实测现在会卡在哪

- **孪生 observe/act**：孪生端口（`authz.nexloop_twin_*`、`nexloop_twin_port`）属于第八批。在发布 27 上这一步标为 blocked，不算通过。
- **模拟 Run**：Run Action 由 prepare 阶段发布到 world 里，不再卡在“未发布”。如果漏跑了 prepare，Run 这一步会判为失败。

第八批到位之后，同一个脚本无需修改就能完整跑通 AT-046。

## 第八批之后

- 用 `sim_world_credentials.py` 再签发 twin 用途（发布 27 的数据库不接受，工具会在写入前拒绝）。
- `nexloop_twin_port` 已在第九批加进 pg_hba 的模拟行（模板、`sim_hba_data_host.sh` 的 SIM 正则和预检、测试）。负责人在 release 29 上线后执行一次 `sim_hba_data_host.sh` 即可。它的口令在 bootstrap 窗口设置；它的 DSN 文件 `sim_twin_port_dsn` 已经在 `post_deploy_check.SIM_DSN_ROLES` 中，`sim_dsn_files.py` 会一起写出。
