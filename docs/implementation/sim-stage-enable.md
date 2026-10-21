# 模拟进程组在 stage 上启用（发布 27 之后，AT-046 实测）

- **依据**：负责人批准“发布 27 部署后，在 stage 上启用模拟进程组，实测 AT-046”。参考 L6 的演进登记做法（`evolution_enroll.py`、`evolution_hba_data_host.sh`）。
- **适用发布**：第七批 r4（1bd26ba1，发布 27）。twin 用途和 `nexloop_twin_port` 角色属于第八批，见“第八批之后”。
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
  - `nexloop_twin_port` 在第八批之前不存在，不会匹配任何人。
- **与你原先要求的差异**：你要求“只放行模拟子网”，我没有让 pg_hba 直接看到模拟子网的源地址。那样需要关闭该网段的 masquerade，并在 DATA_HOST 加回程路由，改动面大，也与现有容器网段的做法不一致。所以我把源地址限制放在 APP_HOST 防火墙，pg_hba 只按角色、库和 SCRAM 限制。如果仍要在 DATA_HOST 侧看到模拟子网，需要另外做一次网络改造（关闭该网段 masquerade、加 DATA_HOST 回程路由、DATA_HOST 防火墙放行模拟子网），这次没有做。

## 步骤与执行人

| # | 步骤 | 执行人 | 命令或工具 |
|---|---|---|---|
| 1 | 部署发布 27（含本分支的模板和工具） | 调度员 | 部署单 |
| 2 | pg_hba：加模拟一行，可与演进 local 行一起，一次重新加载 | **负责人，DATA_HOST root，一次** | `sudo bash scripts/deploy/sim_hba_data_host.sh <私有配置>`。配置是 0600 的 KEY=VALUE：`NEXLOOP_ROOT`、`STAGE_ENV`、`RELEASE_CHECKOUT`。脚本要求新渲染的 pg_hba 只比现有的多出模拟行（可再多一条演进行），不能删任何行；原地改写后发 HUP，失败就写回原内容。之后不必再跑 `evolution_hba_data_host.sh` |
| 3 | 模拟角色的口令（bootstrap 窗口），口令存入钥匙串 | 调度员 | `ALTER ROLE … PASSWORD` 在 bootstrap 窗口执行，不经过任何工具 |
| 4 | 模拟 secret 目录与 DSN 文件 | 调度员 | `sim_dsn_files.py --config <私有配置> --dry-run`，然后用钥匙串管道 `printf '%s %s\n' <角色> <口令>`（每个角色一行）喂给 `sim_dsn_files.py --config …`。会写 5 个 `*_dsn` 文件（0600），各自的 `sslrootcert=/run/secrets/sim_pg_ca` |
| 5 | 其余 secret 文件 | 调度员 | 见下一节清单。`sim_pg_ca` 复制 real 的 pg CA 即可（同一套） |
| 6 | 创建 sim world、生成合成数据、发布该 world 的 Action 目录 | 调度员（工具以负责人的工作台会话执行） | `at046_stage.py prepare --config …`，负责人口令走 stdin。依次调用 create_sim_world、generate_sim_world_data、publish_sim_catalog（0278，见下），输出里有 world id。也可以单独用 `sim_world.py publish-catalog --config …` 发布目录 |
| 7 | 该 world 的凭据 | 调度员（受信配置签发：configurator 登录窗口和签名密钥） | `sim_world_credentials.py --config … --dry-run`，再去掉 `--dry-run` 执行。会签发 api、worker、agent 三种用途，写 token 文件，并把 worker 凭据写成 compose 的 `sim_world_credential`，最后自检：每个凭据在本 world 能认证，在 real 和 test 被拒 |
| 8 | 防火墙 | **负责人，APP_HOST root** | 见“防火墙”一节 |
| 9 | 启动模拟进程组 | 调度员（APP_HOST 的部署账户，有 docker 权限） | 见“启动”一节 |
| 10 | 自检模拟角色 | 调度员 | `post_deploy_check.py … --sim-secret-dir <模拟 secret 目录>`，在模拟项目的一次性容器里执行，或者 `sim_dsn_files.py --check`。每个 DSN 必须以它自己的模拟角色登录 |
| 11 | AT-046 | 调度员 | `at046_stage.py run --config …`（`inputs.world` 填步骤 6 得到的 world id），结果作为 evidence |

## compose 需要的 secret 文件（`NEXLOOP_SIM_SECRET_DIR`，全部 0600，属主为 `NEXLOOP_SIM_UID`）

| 文件 | 来源 |
|---|---|
| `sim_worker_dsn`、`sim_guard_dsn`、`sim_stand_in_worker_dsn`、`sim_files_dsn`（以及 `sim_api_dsn`，供 AT-046 的接收步骤使用） | `sim_dsn_files.py` |
| `sim_pg_ca` | real 的 pg CA bundle |
| `sim_world_credential` | `sim_world_credentials.py` 写入（worker 凭据） |
| `sim_signing_key` | 模拟专用签名密钥。它的 key id（`NEXLOOP_SIM_SIGNING_KEY_ID`）要按 S6b 的步骤登记到 `authz.nexloop_authority_signing_keys` |
| `sim_host_cert`、`sim_host_key`、`sim_host_ca`、`sim_guard_ca`、`sim_guard_certificate`、`sim_guard_tls_key` | 模拟 Host 和 guard 的 TLS 材料，用 `issue_certs.py` 签，与 real 分开 |
| `sim_host_transport_key`、`sim_host_guard_key`、`sim_host_purge_key` | 随机密钥；purge key 同时登记给数据库（`authz.nexloop_sim_hosts`，见 S6） |
| `sim_host_runtime_config`、`sim_host_model_configuration` | 运行时配置：`world_group: simulation`、`context_input_protocol: nexloop.context-pack.sim.v1`、`host_id`。模型配置用确定性档位或廉价模型（D6） |

私有 env 还需要：`NEXLOOP_SIM_UID`、`NEXLOOP_SIM_GID`、`NEXLOOP_SIM_SUBNET`、`NEXLOOP_SIM_HOST_PORT`、`NEXLOOP_SIM_GUARD_PORT`、`NEXLOOP_SIM_HOST_ID`、`NEXLOOP_SIM_SIGNING_KEY_ID`、`NEXLOOP_SIM_RUNTIME_DIR`、`NEXLOOP_SIM_ARTIFACT_DIR`、`NEXLOOP_SIM_SECRET_DIR`、`NEXLOOP_SIM_WORLD`（步骤 6 得到的 world id），以及与 real 相同的镜像 digest。

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

## 模拟 world 的 Action 目录（0278，`sim_world.publish_catalog`）

- **入口**：负责人的受治理 Action `nexloop.sim_world.publish_catalog`（operation `publish_sim_catalog`），只能从 real 发起；命令行是 `sim_world.py publish-catalog`。
- **复制什么**：把 real 已发布、并且在 S4 替身目录（`control.nexloop_stand_ins`）里有替身的 Action 定义，复制到一个已登记、处于 active 的 sim world。没有替身的效应（例如 enterprise delivery）一律不复制，按 D11 失败关闭。
- **不复制什么**：凭据、授权事实、效应类别声明、任何配置都不复制。
- **可重复执行**：已存在的定义只计数，不重写；同一版本但内容不同，判为 `catalog_conflict`。
- **随 world 删除**：这些行在 world 整体删除时一起删掉。
- **真实执行被拦住**：在 sim world 里，效应只能走替身路径。real 的效应表由 0262 的 `nx041_real_effect_world` CHECK 拒绝任何模拟行。
- **authority revision 的取舍**：受治理入口在提交前会复核租户的 authority revision。为此，模拟 world 的 Action 定义写入不再递增这个 revision（重定义了 0004 的 `authz.nexloop_authority_epoch_guard`，已在复制件守护登记）。这些定义只服务于该 world 自己的凭据；real、test 和其他所有授权行的变化仍然照常递增。

## AT-046 实测现在会卡在哪

- **孪生 observe/act**：孪生端口（`authz.nexloop_twin_*`、`nexloop_twin_port`）属于第八批。在发布 27 上这一步标为 blocked，不算通过。
- **模拟 Run**：Run Action 由 prepare 阶段发布到 world 里，不再卡在“未发布”。如果漏跑了 prepare，Run 这一步会判为失败。

第八批到位之后，同一个脚本无需修改就能完整跑通 AT-046。

## 第八批之后

- 用 `sim_world_credentials.py` 再签发 twin 用途（发布 27 的数据库不接受，工具会在写入前拒绝）。
- `nexloop_twin_port` 在第八批建角色。pg_hba 那一行已经包含它，不用再改；它的 DSN 文件要加入 `sim_dsn_files.py` 的清单（`post_deploy_check.SIM_DSN_ROLES`）。
