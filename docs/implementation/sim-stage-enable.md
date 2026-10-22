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
| 4 | 模拟 secret 目录与 DSN 文件 | 调度员（APP_HOST 的部署账户，有 docker 权限，不用 sudo） | APP_HOST；写文件不连数据库，`--check` 以模拟角色走 hostssl | 先确认 `NEXLOOP_SIM_RELEASE_CHECKOUT` 是只读导出（见“release 检出的只读导出”），再建目录：`docker compose -p nexloop-simulation … --profile sim-setup run --rm sim-setup-dir`，建出 secret、token、runtime（`NEXLOOP_SIM_RUNTIME_DIR`）和 artifact（`NEXLOOP_SIM_ARTIFACT_DIR`）四个目录，0700，属主为 `NEXLOOP_SIM_UID`。再在一次性容器里以该 UID 运行工具：`… --profile sim-setup run --rm -T sim-setup scripts/deploy/sim_dsn_files.py --config /setup/<配置> --dry-run`；然后去掉 `--dry-run`，用钥匙串管道 `printf '%s %s\n' <角色> <口令>`（每个角色一行）从 stdin 喂进去。配置里的 `secret_dir` 写 `/sim-secrets`。会写 6 个 `*_dsn` 文件（0600，第九批起包括孪生端口的 `sim_twin_port_dsn`），各自的 `sslrootcert=/run/secrets/sim_pg_ca`，文件从一开始就属于该 UID。DSN 中写的是 `sslrootcert=/run/secrets/sim_pg_ca`，与运行中的服务一致；sim-setup 另外把 secret 目录只读挂载到 `/run/secrets`，所以在这个容器里 `--check` 和 `--verify` 能直接用这些文件连接（stage 2026-10-22 发现：之前只挂在 `/sim-secrets`，6 个 DSN 全部报 unreachable） |
| 5 | 其余 secret 文件，以及两把密钥的数据库登记 | 调度员 | 5a：**APP_HOST**，写文件，不连数据库；5b：在**调度员的机器上**发起一条管道 `ssh APP_HOST … \| ssh DATA_HOST …`，DATA_HOST 走 **socket**（bootstrap，以及由它打开的 configurator 窗口）；两台主机之间不建立 ssh 信任 | **5a**：`… --profile sim-setup run --rm -T sim-setup scripts/deploy/sim_secret_files.py --config /setup/<配置> --dry-run`，再去掉 `--dry-run` 执行（配置见 `deploy/examples/sim-secret-files.example.json`）。在 `/sim-secrets` 写出下一节的全部文件（DSN 和 world 凭据除外），每个 0600、只在缺少时写，重复执行什么都不改；在 `/sim-tokens/sim.env.draft` 写私有 env 草稿。输出只含文件名、key id、host id 和两把密钥的 sha256，不含任何值。**5b**：把 5a 输出的 `registration` 中两个 sha256 填进登记配置的 `expect`（`deploy/examples/sim-secret-register.example.json`），先在 DATA_HOST 的 sim-issue 容器里执行 `sim_secret_register.py --config /setup/<配置> --dry-run`，然后在调度员的机器上执行 `bash scripts/deploy/sim_secret_registration_transfer.sh <私有配置>`。签名密钥和 purge key 都是**对称密钥**，数据库保存的就是密钥本身，所以登记必须拿到密钥：密钥只在这条管道中经过，DATA_HOST 先核对 sha256 与 APP_HOST 的一致，再登记（签名密钥经受信配置，purge key 由 bootstrap 写入 `authz.nexloop_sim_hosts`），最后自检；之后可以用 `--check` 单独复查 |
| 6 | 创建 sim world、生成合成数据、发布该 world 的 Action 目录 | 调度员（工具以负责人的工作台会话执行） | APP_HOST 或其他能访问工作台的执行机；不连数据库（工作台 HTTPS） | `at046_stage.py prepare --config …`，负责人口令走 stdin。依次调用 create_sim_world、generate_sim_world_data、publish_sim_catalog（0294，见下），输出里有 world id。也可以单独用 `sim_world.py publish-catalog --config …` 发布目录 |
| 7 | 签发该 world 的凭据并自检 | 调度员（受信配置签发：configurator 登录窗口和签名密钥） | **DATA_HOST**；configurator 和 bootstrap 检查角色都走 **socket**（它们在 pg_hba 里只有 local 规则；不加 hostssl） | DATA_HOST 上的 `NEXLOOP_SIM_RELEASE_CHECKOUT` 也必须是只读导出（`NEXLOOP_PG_UID` 要能读取，见“release 检出的只读导出”）。在 `deploy/stage/compose.sim-issue.example.yaml` 的一次性容器里运行（以 `NEXLOOP_PG_UID` 运行，即 socket 目录的属主 uid，当前 postgres 镜像为 999；挂载 socket 目录，无网络）。先 `… --profile sim-issue run --rm sim-issue-dir`，把 token 目录和私有输入目录（配置、两个 DSN 文件、签名密钥）设为 `NEXLOOP_PG_UID` 的 0700/0600。第一次执行前，用 `stat -c '%u:%g' $NEXLOOP_ROOT/run/postgresql` 核对 uid，用 `getent passwd <uid>` 查看宿主机上有没有同 uid 的账户（如果有，它也能读输入目录，需要评估）；再执行 `… run --rm -T sim-issue scripts/deploy/sim_world_credentials.py --config /setup/<配置> --dry-run`，然后去掉 `--dry-run` 签发。两个 DSN 都必须是 `host=/var/run/postgresql …`，工具会拒绝 TCP 形式的 DSN。配置中 `token_dir` 写 `/sim-tokens`，不写 `compose_secret_dir`。签发后自检（见“凭据自检”）；`check_database_url_file` 是 bootstrap 窗口的只读检查 DSN（`default_transaction_read_only=on`）。输入文件放进 DATA_HOST 输入目录的方法见“DATA_HOST 输入目录的写入”。`--export`、`--forget` 只读取 `world`、`uses`、`token_dir`，`--check` 只读取 tenant、world、uses、token_dir 和检查 DSN；签发配置中的 configurator DSN 指向的窗口只在签发进程里存在，这几个模式不会再去读它，所以同一份配置可以用于所有模式（stage 2026-10-22） |
| 8 | 把凭据安装到 APP_HOST | 调度员 | **在调度员的机器上**发起一条管道，分别 ssh 到两台主机：DATA_HOST 导出（不连数据库）→ 调度员机器上的管道 → APP_HOST 安装（不连数据库）。**两台主机之间不建立任何 ssh 信任**（L6 at046-split 复核 2.2：DATA_HOST 上有 docker 权限就等于有 root，所以不能让 APP_HOST 能 ssh 到它） | `bash scripts/deploy/sim_credentials_transfer.sh <私有配置>`，在调度员的机器上执行。私有配置是 0600 的 KEY=VALUE：`DATA_HOST_SSH`、`APP_HOST_SSH`（两台主机在调度员 ssh 配置中的别名，二者必须不同）、`DATA_RELEASE`、`DATA_ENV`、`ISSUE_CONFIG`、`APP_RELEASE`、`APP_ENV`、`INSTALL_CONFIG`。脚本只用一条管道完成传输：`ssh DATA_HOST '…sim-issue … --export' \| ssh APP_HOST '…sim-setup … --install'`。token 只在调度员机器的管道内存中经过，不进终端、日志或参数，任何主机上都不落中间文件。安装成功后，脚本再 ssh DATA_HOST 执行 `--forget`；安装失败时副本保留，脚本以 1 退出。`--export` 只在脚本设置的 `NEXLOOP_SIM_TRANSFER_PIPE=1` 下才运行，用来防止手工单独执行，**不是安全边界**（L6 复核 2.1）。安装配置（`nexloop-sim-credentials-install/1`）只有 `world`、`uses`、`token_dir`（`/sim-tokens`）和 `compose_secret_dir`（`/sim-secrets`），写出的文件属于 `NEXLOOP_SIM_UID` |
| 9 | 防火墙 | **负责人，APP_HOST root** | APP_HOST；不连数据库 | 见“防火墙”一节 |
| 10 | 启动模拟进程组 | 调度员（APP_HOST 的部署账户，有 docker 权限） | APP_HOST；各模拟角色走 hostssl | 见“启动”一节 |
| 11 | 自检模拟角色 | 调度员 | APP_HOST（sim-setup 一次性容器，在模拟网络上，以 `NEXLOOP_SIM_UID` 运行）；6 个模拟角色都走 hostssl，verify-full | `… --profile sim-setup run --rm -T sim-setup scripts/deploy/sim_dsn_files.py --config /setup/<配置> --verify`。不读口令，不写文件，只用 `/run/secrets` 下现有的 DSN 文件逐个登录，确认每个 DSN 都以它自己的模拟角色登录，没有提升的属性、成员关系，也没有 real 的执行权限（`post_deploy_check.check_sim_roles`）。部署后的整体检查 `post_deploy_check.py … --sim-secret-dir` 仍然可用，但它需要 stage env 和 docker，在 sim-setup 里跑不了 |
| 12 | AT-046 探针（之前） | 调度员 | **DATA_HOST**；只读探针走 socket（bootstrap 只读 DSN） | 在 `sim-issue` 一次性容器里运行 `at046_stage.py probe-before --config …`（配置里只填 `roles.probe`；容器里只有 `/tmp` 和 token 目录可写，所以 `results_dir` 设为 `/tmp`，结果从 stdout 重定向保存为文件）。会记录 real 侧摘要，并检查 synthetic Consumer 和该 world 的 Run Action。它会生成一个 run nonce 写进结果，后面三步都靠它确认属于同一次运行 |
| 13 | AT-046 接收 | 调度员 | **APP_HOST**；`sim_api` 和 `twin_port` 走 hostssl（模拟一次性容器，不用探针） | 先把步骤 12 的结果文件（只含摘要、id、计数和 nonce，不含 token 或 DSN）拷到 APP_HOST 的 `NEXLOOP_SIM_SETUP_INPUT_DIR`，容器里只读挂载为 `/setup`。再执行 `… --profile sim-setup run --rm -T sim-setup scripts/acceptance/at046_stage.py intake --config /setup/<配置> --from /setup/<probe-before 结果>`。配置里只填 `roles.sim_api`，以及有孪生时的 `roles.twin_port`（DSN 用 `/sim-secrets/sim_twin_port_dsn`）；`results_dir` 设为 `/tmp`（容器里可写的只有 `/tmp`、`/sim-secrets`、`/sim-tokens`），结果从 stdout 重定向保存为文件。依次执行 twin、签发 Run 凭据、登记 sim context、accept。直接连数据库，不经过工作台 |
| 14 | AT-046 探针（之后） | 调度员 | **DATA_HOST**；只读探针走 socket | 先把步骤 13 的结果文件用 `sim-issue-input` 写进 DATA_HOST 的 `NEXLOOP_SIM_ISSUE_INPUT_DIR`（容器里是 `/setup`，见“DATA_HOST 输入目录的写入”），再执行 `at046_stage.py probe-after --config /setup/<配置> --from /setup/<probe-before 结果> --from /setup/<intake 结果>`，`results_dir` 设为 `/tmp`。它会等 task 结束，再核对 Run 凭据的 world、该 world 的凭据，以及 real 侧摘要是否不变 |
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

**排除的周期性簿记表（stage 2026-10-22）**：stage 第一次实跑时，`control.nexloop_alert_state`（告警评估器每个周期改写每条规则的状态）和 `runtime.nexloop_orphan_sweeps`（产物孤儿清理器记录每次周期清理）被报为“改变”。实际上这两张表不管 AT-046 跑不跑都会变。它们按表名排除在对照之外，排除的理由写在 `at046_stage.EXCLUDED` 里，并且列在结果的 `observed.excluded` 中；除此之外的任何改变，仍然判这一步失败。如果以后还有别的定时写入的表报出来，要先确认它确实与 AT-046 无关，再写明理由加入清单。

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

`NEXLOOP_SIM_RELEASE_CHECKOUT`（APP_HOST 和 DATA_HOST 都有）必须指向 release 的只读导出，`NEXLOOP_SIM_UID` 或 `NEXLOOP_PG_UID` 要能读取，见“release 检出的只读导出”。

`sim_secret_files.py` 会写出一份草稿（`env_draft_file`），列出 compose 读取的全部 `NEXLOOP_SIM_*` 变量（外加两个镜像变量）。草稿里已经填好配置中给出的值，以及 host id、签名 key id、guard 端口；其余写成 `<…>` 占位，用之前逐项补齐。草稿只在文件不存在时写出。

DATA_HOST 侧的两点（stage 2026-10-22）：
- `sim-issue-dir` 实际执行时，同样会遇到“root 只有 CHOWN 能力，不能 chmod 不属于自己的路径”的问题：docker 建出的 0755 root 目录、调度员放进来的输入文件，以及重复执行时已属于 `NEXLOOP_PG_UID` 的 0700 目录。`sim-setup-dir` 和 `sim-issue-dir` 都用同样的三步解决：先 chown 给 root，再 chmod，最后 chown 给目标 uid（`tests/test_sim_setup_dir_capabilities.py`）。
- `NEXLOOP_PG_UID` 是 999，在 DATA_HOST 上对应宿主机的系统账户 `dnsmasq`，第一次执行前的 `getent passwd 999` 会看到它。这是已知的系统账户，**评估后接受**，并采取以下措施：
  - 这个 uid 是 postgres 容器使用的 uid，socket 目录本来就属于它，所以这不是新的暴露面；
  - 输入目录（configurator DSN、bootstrap DSN、签名密钥和配置）只在第 7、12、14 步执行期间存在，用完立即删除其中的文件；
  - 第 7 步签发完成后，执行 `--forget` 删掉 DATA_HOST 上的 token 副本（第 8 步）。
  - 替代方案是改用一个宿主机上没有对应账户的 uid。但 socket 目录必须由 postgres 容器的 uid 持有，这需要改动数据库容器，这次不采用。

## release 检出的只读导出（`NEXLOOP_SIM_RELEASE_CHECKOUT`，两台主机都要）

- **为什么**：`sim-setup`（APP_HOST，以 `NEXLOOP_SIM_UID` 运行）和 `sim-issue`（DATA_HOST，以 `NEXLOOP_PG_UID` 运行）都把 `NEXLOOP_SIM_RELEASE_CHECKOUT` 只读挂载为 `/opt/nexloop-release`，从中运行工具。部署时的 release 检出属于 root，权限 0700，这两个 uid 读不到。stage 上第一次执行时报错：`can't open file … [Errno 13] Permission denied`（2026-10-22）。
- **做法**：两台主机各自从部署用的检出导出一份只含源码的副本，属主 root，对其他用户只读：

  ```bash
  sudo install -d -o root -g root -m 0755 <导出目录>
  sudo git -C <部署用的 release 检出> archive --format=tar <release 提交> | sudo tar -x -C <导出目录>
  sudo chmod -R go+rX,go-w <导出目录>
  ```

  - `git archive` 只导出受版本控制的文件，私有配置、`.env`、`local-only/` 这类未跟踪或被忽略的文件不会进入导出目录，所以不要用 `cp -r` 代替。
  - 导出完成后用 `git -C <部署用的 release 检出> ls-files | wc -l` 与 `find <导出目录> -type f | wc -l` 核对文件数，两者应该一致。
  - 私有 env 中 `NEXLOOP_SIM_RELEASE_CHECKOUT` 指向这个导出目录，不要指向部署用的检出。每个新 release 都要重新导出一次。
- **预检**：两个容器的入口先检查 `/opt/nexloop-release` 及其中的 `scripts/deploy`、`scripts/acceptance`、`packages/eios-core/src` 对本 uid 是否可读，不可读就以 2 退出，并输出一行 JSON，写明 uid 和读不到的目录，然后才运行指定的脚本。脚本只能是 `scripts/…` 下的路径。容器的 `working_dir` 是 `/`，这样即使检出无法进入，也会先由预检报错，而不是在 docker 切换目录时报错。

## DATA_HOST 输入目录的写入（第 7、12、14 步）

`NEXLOOP_SIM_ISSUE_INPUT_DIR` 属于 `NEXLOOP_PG_UID`，权限 0700，调度员的账户写不进去。文件以 tar 流的形式交给 `sim-issue-input` 一次性容器，由 `scripts/deploy/sim_input_put.py` 写入。这个容器以该 uid 运行，只挂载输入目录（可写）和 release 只读导出，不挂 socket，不挂 token 目录，没有网络。在调度员的机器上执行：

```bash
tar -C <本地私有目录> -cf - <文件…> | ssh DATA_HOST 'cd <release 导出> && docker compose -p nexloop-sim-issue -f deploy/stage/compose.sim-issue.example.yaml --env-file <私有 env> --profile sim-issue run --rm -T sim-issue-input scripts/deploy/sim_input_put.py'
```

- 只接受文件名不含路径的普通文件（每个最多 1 MiB，最多 64 个），以 0600 原子写入，同名文件会被替换。
- 只要流里有目录、链接、带路径或含 `..` 的名字、超大文件，整个流都被拒绝，什么都不写。
- 输出只有文件名和大小。
- 用完之后，按“DATA_HOST 侧的两点”的要求删除输入文件。

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

**启动顺序与排错（stage 2026-10-22）**：
- runtime worker、stand-in executor 和 world-files 三个服务共用 `sim-agent-host` 的网络命名空间。它们都设置了 `depends_on: sim-agent-host: {condition: service_healthy, restart: true}`：Host 的 healthcheck（loopback 上的 `/health/live`）通过之后，它们才会启动；Host 重启时，它们也跟着重启。以前 Host 处在重启循环时，这几个服务先起来，就会拿到一个坏掉的命名空间（报 `Network is unreachable`），之后不会自己恢复。
- 如果 `docker compose ps` 显示 Host 一直是 unhealthy，或者在重启：看 `docker compose -p nexloop-simulation logs sim-agent-host`。启动器现在会写明原因，例如 `private runtime root required: a directory of uid <UID> with mode 0700 (found uid …, mode …)`。`NEXLOOP_SIM_RUNTIME_DIR` 和 `NEXLOOP_SIM_ARTIFACT_DIR` 必须属于 `NEXLOOP_SIM_UID`、权限 0700，`sim-setup-dir` 会把它们和 secret、token 两个目录一起建好。
- 如果用的是不支持 `restart: true` 的旧版 compose，或者依赖的服务仍然报 `Network is unreachable`：等 Host 健康之后，执行 `docker compose -p nexloop-simulation … --profile simulation up -d --force-recreate sim-runtime-worker sim-stand-in-executor sim-world-files`。

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

- **孪生 observe/act**：孪生端口（`authz.nexloop_twin_*`、`nexloop_twin_port`）从第八批起具备。要跑这一步，需要先满足以下条件（缺任何一项，这一步都是 blocked，不算通过）：
  1. **已发布的 consumer twin**：twin 属于租户，不属于某个 world，必须是 published 状态，并且生成器不调用模型（D6）。最简单的做法是在配置里加上 `inputs.publish_twin`：**只要有这个键就开启**，值可以是 `true`、`{}` 或 `{"twin_id": "at046-synthetic-<名字>"}`，默认名字是 `at046-synthetic-trial`。名字必须带 `at046-synthetic-` 前缀；值不合法（例如 `false`，或名字不带前缀）时，在创建任何东西之前就拒绝，不会留下空的 world。可以在 prepare 时一起发布，也可以单独执行 `at046_stage.py publish-twin --config …`：它只发布 twin，不创建 world（twin 属于租户），之后的 run 各步继续用 `inputs.world` 复用已有的 world。发布经负责人的受治理 Action `publish_consumer_twin`，内容是契约示例中的合成 twin（`packages/contracts/examples/consumer-twin.valid.json`，生成器 `twin-template/1`）。结果里的 `twin_published` 步骤给出 `twin_id` 和 `version`。同一份定义重复执行时会回放：工作台只返回 `replayed`，这时 `version` 为空，twin 和版本与第一次发布相同，版本号从第一次发布的结果文件里取。stage 上第一次执行时返回 422（2026-10-22），原因是工作台这个表单的字段正好是 `definition` 和 `calibration_report_ref` 两个，请求体的键集合必须与之完全一致，而当时只发了 `definition`；现在会带上 `calibration_report_ref: null`。也可以在工作台手工执行 `publish_consumer_twin`。校准（`twin_calibrate.py`，或 Action `calibrate_consumer_twin`）不是必需的，未校准的 twin 也能绑定，只是事件的 provenance 中会写 `uncalibrated`。
     **验收结束后**执行 `at046_stage.py cleanup --config …`（`inputs.twin` 填这个 twin 的 `twin_id` 和 `version`，负责人口令走 stdin），经负责人的 `retire_consumer_twin` 将它作废。cleanup 只会作废带前缀的 twin，重复执行只会回放。作废后它不能再被绑定（L6 第十二批复核 4.1）。
     **会不会被 NX-043 的 real 实验选中**：不会（0311 修复之后）。实验从不自己挑 twin，twin 由负责人在实验登记里写明；在此基础上还有三道限制：
     - online 和 shadow 阶段的登记，只要 twin 名带 `at046-synthetic-` 前缀，就在 register 时直接拒绝（`twin_synthetic`），不管它是否已作废；
     - screening 和 shadow 阶段要求 twin 已发布、未作废并且已校准（D9），这个合成 twin 没有校准；
     - online 阶段仍然豁免校准，但从 0311 起要求 twin 未被作废，已作废的 twin 在 start、分配和 guard 时都会被拒（`twin_not_calibrated`）。0311 之前，online 连已作废的 twin 也放行，那时只作废并不能把它挡在 online 实验之外（L6 第十二批修复复核）。
  2. **该 world 的 twin 凭据**：`sim_world_credentials.py` 的 `uses` 要包含 `twin`，并经第 8 步装到 APP_HOST。
  3. **twin 端口的 DSN**：`sim_dsn_files.py` 会写出 `sim_twin_port_dsn`；pg_hba 的模拟行从第九批起已包含 `nexloop_twin_port`；它的口令在 bootstrap 窗口设置。
  4. **intake 配置（第 13 步）**：
     - `roles.twin_port`：`{"dsn_file": "/run/secrets/sim_twin_port_dsn", "token_file": "/sim-tokens/<world uuid>/twin-credential"}`；
     - `inputs.twin`：`{"twin_id": <twin_published.twin_id>, "version": <twin_published.version>, "seed": <1–64 位的 [A-Za-z0-9_.-]，同一 seed 可完全重放>, "steps": <每个 Consumer 观察和行动的步数，默认 2>}`。
- **AT-046 的核心反例（N1、N2，stage 判定 2026-10-22）**：stage 第三次实跑只证明了正常路径下的隔离，没有执行 AT-046 的核心断言——“Twin 调用真实消息 Action，服务端拒绝，即使模型伪造 mode”。现在 intake 在同一个 run nonce 下多执行两步，两步都加入了 merge 的 `REQUIRED`：缺少任一步，结果就不能是 true；任一步被放行，就判失败。
  - **N1 `real_action_refused`**：用该 world 自己的 api 凭据向服务端请求 real world 的会话。任何 real 消息 Action（例如 `nexloop.service.request`、`nexloop.outreach.send`）都要先经过这一步；期望 42501（`service authentication denied`）。配置了孪生端口时，再以 `nexloop_twin_port` 角色调用端口的服务端入口，world 填 real（客户端在调用前就会拒绝 real，所以这里直接调用服务端入口）；期望 42501。
  - **N2 `forged_mode_refused`**：模拟 intake 在模拟 world 中提交一条 `mode:'real'` 的 Run 命令，并且只针对这一次调用跳过 Python 客户端自己的 mode 检查，模拟一个伪造 mode 的模型；期望服务端拒绝（42501，`activation command denied`）。
  - **0312 修复**：在本地复现时发现，**服务端原先并不检查 mode**。只有 Python 客户端要求 mode 等于 world 的类型；绕过客户端之后，服务端会接受并保存一条伪造 mode 的命令。0312 让服务端的命令检查（register、create、resolve、authorize 共用同一处）也要求 `mode` 等于 world 的类型。real 命令本来就是 mode real、world real，所以 real 路径不变。
  - **安全性（stage 上真实外发的三层开关是开着的）**：两步都不可能走到任何外发路径，即使被意外放行也一样。N1 只请求会话、只做绑定，不提交任何效应；N2 即使被接受，task 也留在模拟 world 中，那里的效应只会到替身。probe-after 中 real 侧摘要不变，证明“被拒绝”和“没有效应”同时成立。结果中只记录 SQLSTATE 和服务端固定的拒绝文字，不记录请求内容。
- **模拟 Run**：Run Action 由 prepare 阶段发布到 world 里，不再卡在“未发布”。如果漏跑了 prepare，Run 这一步会判为失败。
- **worker 权限（stage 2026-10-22 修复，迁移 0310）**：Run 在 worker 的 control 阶段失败（`runtime_control_unavailable` / `InsufficientPrivilege`），原因是 `nexloop_sim_worker` 不能执行派发前复核负责人控制的 4 个入口：`assert_task_dispatch_controls`、`bind_run_goal`、`budget_status`、`reserve_budget`。每个 sim world 创建时都有自己的模型预算，所以每个 sim Run 都会预留预算。本地测试之所以没发现，是因为它直接调用了 claim 和 activation，跳过了 worker 自己的派发路径；现在 `tests/test_nx041_sim_pi_run_pg.py` 改为走 `RuntimeDispatcher.run_once`。worker 遇到这类失败时，会在 stderr 输出一行 `runtime_dispatch_error`，只包含阶段、任务代码和异常类名。这 4 个入口还加了第二道防线，做法同 0275（L6 第十二批复核 2.1）：模拟角色在 real 或 test world 调用时直接拒绝（42501，`simulation role outside its world`），即使它拿到了 real 凭据的 digest 也一样。real 角色的 `app_role` 就是它自己，所以 real worker 的路径不变。

第八批到位之后，同一个脚本无需修改就能完整跑通 AT-046。

## 第八批之后

- 用 `sim_world_credentials.py` 再签发 twin 用途（发布 27 的数据库不接受，工具会在写入前拒绝）。
- `nexloop_twin_port` 已在第九批加进 pg_hba 的模拟行（模板、`sim_hba_data_host.sh` 的 SIM 正则和预检、测试）。负责人在 release 29 上线后执行一次 `sim_hba_data_host.sh` 即可。它的口令在 bootstrap 窗口设置；它的 DSN 文件 `sim_twin_port_dsn` 已经在 `post_deploy_check.SIM_DSN_ROLES` 中，`sim_dsn_files.py` 会一起写出。
