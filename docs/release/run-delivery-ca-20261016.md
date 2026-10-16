# 命令单：webchat-delivery 改用与 action-worker 路由相同的 provider CA（2026-10-16）

**背景**：冒烟中 webchat-delivery 返回 503，外发 effect 的 `delivery_state=unknown`。
- **根因**：两边计算 provider profile 时用的是不同的 CA 文件，摘要不一致。
  - action-worker 路由的 provider 配置使用 `ca_file=/run/secrets/action_worker_webchat_provider_ca`（CA bundle）。
  - webchat-delivery 的 `--provider-ca-file` 却指向 `/run/secrets/webchat_delivery_cert`，那是它自己的服务端证书链。
  - profile 摘要的内容是 origin、connect_address 和 CA 文本的 sha256。0175 要求投递一侧的摘要等于 effect 记录的摘要，所以每次投递都被拒绝。
- **修复**：webchat-delivery 有了自己的 secret `webchat_delivery_provider_ca`，文件为 `secrets/webchat-delivery/provider-ca.crt`，内容与 `action-worker/webchat-provider-ca.crt` 相同；`--provider-ca-file` 改为指向它。

只用角色名和变量。**B** 表示需负责人确认；**sudo-A** 表示需要 APP_HOST 的 sudo。不需要新镜像，也不需要迁移。

## 1. APP_HOST：放置 CA 副本，修改 compose（**B**，sudo-A）

```
B=$NEXLOOP_ROOT/ops/delivery-ca-backup; sudo install -d -o root -g root -m 0700 "$B"
sudo cp -p "$NEXLOOP_ROOT/config/compose.app.yaml" "$B/compose.app.yaml"
sudo install -o 10001 -g 10001 -m 0600 "$NEXLOOP_ROOT/secrets/action-worker/webchat-provider-ca.crt" "$NEXLOOP_ROOT/secrets/webchat-delivery/provider-ca.crt"
sudo cmp "$NEXLOOP_ROOT/secrets/action-worker/webchat-provider-ca.crt" "$NEXLOOP_ROOT/secrets/webchat-delivery/provider-ca.crt" && echo same
```

- **预期**：最后一条输出 `same`。目标文件原本不存在，存在时请先停下来核对。
- 管理机的秘密树下次生成时会自带这个文件（`stage_secrets.py` 把 `provider-ca.crt` 列入 CA 文件，内容是同一个 ca_bundle，属主与 webchat-delivery 的其他文件一致）。

然后编辑 `$NEXLOOP_ROOT/config/compose.app.yaml`，做与本分支 `deploy/stage/compose.app.example.yaml` 相同的三处修改：
1. 在 webchat-delivery 的 `command` 中，把 `--provider-ca-file, /run/secrets/webchat_delivery_cert` 改为 `--provider-ca-file, /run/secrets/webchat_delivery_provider_ca`。注意只改这一处，`--certificate-file` 仍然是 `webchat_delivery_cert`。
2. 在 webchat-delivery 的 `secrets:` 列表中加上 `webchat_delivery_provider_ca`。
3. 在文件末尾的顶层 `secrets:` 中加一行：
   `webchat_delivery_provider_ca: {file: "${NEXLOOP_ROOT:?root}/secrets/webchat-delivery/provider-ca.crt"}`

用下面的命令检查改动：
```
C="sudo docker compose -p nexloop-app -f $NEXLOOP_ROOT/config/compose.app.yaml --env-file $NEXLOOP_ROOT/config/compose.app.env --env-file $NEXLOOP_ROOT/config/compose.images.env"
$C config --quiet && echo config-ok
sudo diff "$B/compose.app.yaml" "$NEXLOOP_ROOT/config/compose.app.yaml"
```
- **预期**：第一条输出 `config-ok`；diff 只显示上面三处修改。

## 2. 只重建 webchat-delivery

**时机**：选一个没有进行中冒烟的时段。重建的几秒内投递会失败；已受理的 effect 留在 PG，重建后由 recovery 继续处理。

```
$C up -d --force-recreate --no-deps webchat-delivery
```

- **为什么只重建它**：
  - `--no-deps` 不会碰 action-worker；
  - 没有任何服务以 `network_mode` 引用 webchat-delivery。
- **netns**：webchat-delivery 用的是 `network_mode: service:action-worker`，它会加入当前 action-worker 容器的网络命名空间。重建后核对：
  ```
  aw=$($C ps -q action-worker); wd=$($C ps -q webchat-delivery)
  [ "$(sudo docker inspect -f '{{.State.Pid}}' $aw | xargs -I{} sudo readlink /proc/{}/ns/net)" = "$(sudo docker inspect -f '{{.State.Pid}}' $wd | xargs -I{} sudo readlink /proc/{}/ns/net)" ] && echo netns-same
  ```
  预期输出 `netns-same`。如果不同（例如 action-worker 在这之后被重建过），再执行一次上面的 `up -d --force-recreate --no-deps webchat-delivery`。
- **以后重建 action-worker 时**：也必须接着重建 webchat-delivery。

## 3. 撤销

```
sudo install -o root -g root -m 0600 "$B/compose.app.yaml" "$NEXLOOP_ROOT/config/compose.app.yaml"
$C up -d --force-recreate --no-deps webchat-delivery
```
撤销后会回到原来的故障状态，即投递被拒绝、结果为 unknown。`secrets/webchat-delivery/provider-ca.crt` 可以保留，旧的 compose 不会引用它。

## 4. 核对

1. **健康**：在 action-worker 容器里（与 webchat-delivery 共享网络命名空间），用它路由所用的 CA 校验 TLS：
   ```
   $C exec -T action-worker python -c 'import ssl,sys,urllib.request
   c=ssl.create_default_context(cafile="/run/secrets/action_worker_webchat_provider_ca")
   print(urllib.request.urlopen(sys.argv[1]+"/v1/health",context=c,timeout=5).status)' "https://127.0.0.1:$WEBCHAT_DELIVERY_PORT"
   ```
   - 预期输出 `200`。
   - `$C ps webchat-delivery` 为 running，不再重启。
2. **两边的 profile 摘要前缀相等**。每边都在自己的容器里、以自己的身份计算：
   ```
   $C exec -T action-worker python -c 'import json
   from nexloop_eios.private_configuration import read_private_text as r
   from nexloop_eios.effect_provider import profile_identity as i, profile_digest_of as d
   c=json.loads(r(json.loads(r("/run/secrets/action_worker_provider_routes"))["routes"]["nexloop.service.request"]))
   print(d(i(c["origin"],c.get("connect_address"),r(c["ca_file"],maximum=32768)))[:12])'
   $C exec -T webchat-delivery python -c 'import sys
   from nexloop_eios.private_configuration import read_private_text as r
   from nexloop_eios.effect_provider import profile_identity as i, profile_digest_of as d
   print(d(i(sys.argv[1],None,r("/run/secrets/webchat_delivery_provider_ca",maximum=32768)))[:12])' "https://127.0.0.1:$WEBCHAT_DELIVERY_PORT"
   ```
   - 预期两行输出的 12 位前缀相同。
   - 只输出摘要前缀，不输出任何文件内容。
   - `$WEBCHAT_DELIVERY_PORT` 取自 compose.app.env。
   - 这里的 `effect_provider.profile_identity` 要等下一次发布才有。在当前镜像中，可以对比 `python -c` 中两份 CA 文本的 sha256 前缀，再加上 origin 字符串是否相同来替代。
3. **门禁**（下一次发布之后）：`gate_dir.py` 加 `nexloop-doctor --deploy-gate` 时，新检查 `webchat_delivery_profile_matches_route` 应为 true。它会以各服务的 uid 读 routes 文件、provider 配置和两份 CA，逐项比较 origin、connect_address 和 CA 摘要。不一致时，detail 会给出 `<字段>_differs`，例如这次的问题会显示为 `trust_digest_differs`。
4. **冒烟**：重跑外发投递那一步。effect 应为 `delivered`，不再是 `unknown`。

## 5. 依据与测试（本分支）

- `tests/test_delivery_provider_profile.py`：
  - CA 和 origin 都相同时通过；
  - 用服务端证书链作 CA（这次 stage 的配置）时为 `trust_digest_differs`；
  - origin 不同、缺少路由、属主不对、缺少参数，都各有对应的代码；
  - `profile_identity` 与 `HttpEffectProvider.profile_digest` 算出的值相同；
  - stage compose 让 webchat-delivery 引用自己的 CA 副本。
- `tests/test_stage_secrets.py::test_the_delivery_service_gets_the_same_ca_as_the_action_worker_route`：生成的副本与 action-worker 的 CA 字节相同，权限 0600，属主与 webchat-delivery 的其他文件一致。
- `tests/test_deploy_gate.py`：app 角色的门禁中，这项检查与其他检查一起为 true。
