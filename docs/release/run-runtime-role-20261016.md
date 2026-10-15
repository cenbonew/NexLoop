# 命令单：stage 的 runtime-worker 改用 nexloop_domain_worker 连库（2026-10-16）

**背景**：带模型冒烟时，operations 任务以 `runtime_activation_unavailable` 结束。PG 日志为 `permission denied for function nexloop_read_action_bundle`。
- **根因**：stage 的 runtime-worker 用 `nexloop_scheduler` 连库，映射来自 `stage_secrets.PRIMARY_ROLE`。入口程序接受这个角色，但激活路径上的函数大多只授予 `nexloop_domain_worker`。0063 还明确从 scheduler 撤销了 `nexloop_role_run_hint`。
- 所有 runtime 测试都以 `nexloop_domain_worker` 运行 worker。
- **调度员裁定方案 A**：改用 `nexloop_domain_worker`，不加迁移。

只用角色名和变量。**B** 表示需负责人确认；**sudo-A** 表示需要 APP_HOST 的 sudo。

**不需要新镜像**：发布镜像中的 `runtime_worker` 和 `runtime_guard_worker` 本来就接受 `nexloop_domain_worker`。失败结果里新增的 `error_class` 要等下一次发布才有。

## 1. 管理机：重新签发 runtime-worker 的 DSN 与证书（不需要 sudo）

```
cp -p "$MGMT_TREE/manifest.json" "$MGMT_TREE/manifest.json.before-runtime-role"
python3 "$RELEASE_CHECKOUT/scripts/deploy/stage_secrets.py" reissue-role --out "$MGMT_TREE" --ca-dir "$CA_DIR" --params "$PARAMS" --service runtime-worker
python3 "$RELEASE_CHECKOUT/scripts/deploy/stage_secrets.py" check --out "$MGMT_TREE"
```

- **预期**：
  - 第二条输出 `{"reissued": ["runtime-worker/dsn", "runtime-worker/pg-client.crt", "runtime-worker/pg-client.key"], "role": "nexloop_domain_worker", ...}`；
  - 第三条输出 `"ok": true`。
- **改了什么**：
  - DSN 的用户改为 `nexloop_domain_worker`，口令取自同一棵树里 domain-worker 的 DSN（一个角色一个口令，数据库里已经生效），其余字段（地址、端口、库名、证书路径、verify-full）不变；
  - 客户端证书由中间 CA 重新签发，CN 为 `nexloop_domain_worker`；
  - 其他文件一律不动，manifest 记录新的摘要。
- **再次执行**：DSN 已经是该角色时拒绝，退出码 2。
- **撤销**：恢复 `manifest.json.before-runtime-role`，三个文件按第 3 步的备份恢复。
- **pg_hba 不需要改**：`APP_PG_ROLES` 已含 `nexloop_domain_worker`，规则是 `hostssl … scram-sha-256 clientcert=verify-full`，也就是证书 CN 必须等于角色名，新证书正好满足。

## 2. 传到 APP_HOST（不需要 sudo）

把 `app/secrets/runtime-worker/` 下的 `dsn`、`pg-client.crt`、`pg-client.key` 用 `scp -p` 传到 APP_HOST 的私有目录 `$RR_SRC`（0700），传完核对 sha256。

## 3. APP_HOST：替换文件并只重建 runtime-worker（**B**，sudo-A）

**时机**：选一个没有进行中的交互和冒烟的时段。
- runtime-worker 同时承载 guard。重建期间（几秒到十几秒）Agent Host 的 guard 调用会失败，进行中的 Run 会以授权不可用结束，由既有的重试或回复保障路径处理。
- 已受理但还没派发的任务留在 PG 队列里，重建后继续处理。

```
B=$NEXLOOP_ROOT/ops/runtime-role-backup; sudo install -d -o root -g root -m 0700 "$B"
for f in dsn pg-client.crt pg-client.key; do sudo cp -p "$NEXLOOP_ROOT/secrets/runtime-worker/$f" "$B/$f"; done
for f in dsn pg-client.crt pg-client.key; do sudo install -o 10001 -g 10001 -m 0600 "$RR_SRC/$f" "$NEXLOOP_ROOT/secrets/runtime-worker/$f"; done
sudo docker compose -p nexloop-app -f $NEXLOOP_ROOT/config/compose.app.yaml --env-file $NEXLOOP_ROOT/config/compose.app.env \
  --env-file $NEXLOOP_ROOT/config/compose.images.env up -d --force-recreate --no-deps runtime-worker
```

- 这三个文件是单文件的 secret 绑定挂载。用 `install` 替换会产生新 inode，所以**必须重建容器**，它才会挂载新文件（见 run-metrics-gate §1.3 的注意事项）。上面的 `--force-recreate` 正是为此。
- **只重建 runtime-worker**：
  - `--no-deps` 不会碰 app-net；runtime-worker 用的是 `network_mode: service:app-net`，app-net 不需要重建；
  - 没有任何服务以 `network_mode` 引用 runtime-worker，已用 compose 文件核对过。
- **撤销**：
  ```
  for f in dsn pg-client.crt pg-client.key; do sudo install -o 10001 -g 10001 -m 0600 "$B/$f" "$NEXLOOP_ROOT/secrets/runtime-worker/$f"; done
  ```
  然后执行同一条 `up -d --force-recreate --no-deps runtime-worker`，回到 `nexloop_scheduler`，也就回到原来的故障状态。

## 4. 核对

1. **登录角色**：在 runtime-worker 容器内，以它自己的身份（10001）读它的 DSN：
   ```
   sudo docker compose -p nexloop-app exec -T runtime-worker python -c 'import psycopg
   from nexloop_eios.private_configuration import read_private_text
   with psycopg.connect(read_private_text("/run/secrets/runtime_worker_dsn",maximum=16384),connect_timeout=5) as c:print(c.execute("select current_user").fetchone()[0])'
   ```
   预期输出 `nexloop_domain_worker`。
2. **进程**：`sudo docker compose -p nexloop-app ps runtime-worker` 为 running、不再重启；`running_snapshot.py` 输出 `guard_children: 4`。
3. **日志**：`sudo docker compose -p nexloop-app logs --since 5m runtime-worker` 只有 ready 行，没有 `unavailable`。DATA_HOST 的 PG 日志里不再出现 `permission denied for function`。
4. **冒烟**：重跑带模型冒烟的那一步（run-model-smoke-20261018）。operations 任务应为 `succeeded`；如果失败，记下 `code`。下一次发布之后，结果里还会有 `error_class` 和 `error_cause_class`。

## 5. 依据与测试（本分支）

- **守护测试** `test_every_stage_dsn_role_is_one_its_entry_point_accepts_and_the_runtime_worker_runs_as_its_tests_do`：
  - 每个服务 DSN 的角色，都必须在其入口程序 `verify_application_role` 接受的集合里；
  - runtime-worker 的角色必须与 runtime 测试实际使用的角色（`nexloop_domain_worker`）一致。
- **重新签发**：`test_reissue_role_gives_the_runtime_worker_a_domain_worker_dsn_and_certificate_once`，确认只改三个文件，第二次执行被拒绝。
- **证书登录**：真实 TLS PostgreSQL 上，以 runtime-worker 的 DSN 和证书登录，`current_user` 为 `nexloop_domain_worker`（`hostssl … cert clientcert=verify-full`）。
- **激活路径**：runtime 测试全部以 `nexloop_domain_worker` 运行 worker，覆盖 create、authorize、Host 请求与完成（`test_runtime_dispatch`、`test_runtime_atomic_accept`）。
- **connection-budget 不变**：预算按服务计，没有按角色的上限（代码和迁移里都没有 `connection limit`），runtime-worker 的连接数和 guard 子进程数都没有变化。
