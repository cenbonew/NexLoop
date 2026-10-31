# 运行单：服务凭据随授权变更 scopes 的轮换（S79，stage R66）

- 适用分支：`l2/s79-credential-scope-rotation`，基于 main 7b316753。迁移 0391（临时号）。
- 用途：解除 S77 上线阻塞。R65 实测：service-grants v24 给 catalog_maintainer 加了两条 object_type READ，`compile_principal` 编出的
  requested_scopes 是 `[action.execute, object_type.read]`，库里绑定仍是 `[action.execute]`，register 报 `credential_rotation_required`；
  0384 的轮换复制旧绑定，scopes 不变。
- 执行者：调度员。判定者：L6。证据只写角色名、计数和 ID，不贴令牌、DSN、主机名或地址。
- `$COMMON`、`$WINDOW`、`$RUN`、`<树>`、同步做法与 run-i31-credential-renewal-stage 相同。catalog_maintainer 只由操作员手动使用
  （catalog_setup / incentive_setup），没有常驻服务，**不需要重启任何容器**。

## 变更

| 项 | 内容 |
|---|---|
| 0391 §1 | `control.nexloop_rotate_service_credential_scopes`：0384 的轮换（同一调用者、签名密钥、request 重放、当前令牌须为唯一 active、到期规则），请求多一个 `requested_scopes`。新令牌的绑定 = 当前绑定只换 scopes；认证 fact 只允许 `expires_at`、`snapshot_digest`、`requested_scopes` 三处不同。scopes 相同（顺序无关）时绑定逐字节不变 |
| 0391 §2 | 0384 行守卫也接受从上述入口插入第二枚令牌（调用栈，不是 GUC）；copy guard 已登记 |
| 0391 §3 | `control.nexloop_revoke_superseded_service_credential`：没有轮换目录时，撤销到期时间**恰好**为给定值的那一枚 active 令牌；保留的令牌（树里这枚）必须是同一凭据的另一枚 active 令牌且更晚到期 |
| 工具 | `rotate-credential --apply` 总是按当前 service-grants 重新编译 scopes 再轮换（输出 `scopes_added` / `scopes_removed`）；新增 `revoke-credential --superseded --expires-at <到期时间> --request-id <UUID>` |

**注意，scopes 变化时不是无缝重叠**：EIOS 逐字段比较令牌绑定与按 credential_id 共享的认证 fact。所以 apply 之后旧令牌**仍能认证**，
但授权判定都会因 `AUTHENTICATION_STALE` 被拒；在 register 写完新的 scope fact 之前，新令牌也用不了新增的 scope。
对 catalog_maintainer 来说这段窗口没有影响（无常驻服务），但从 apply 到 register 期间不要运行 catalog_setup / incentive_setup。

## 步骤

### 0. 前提（只读）

- 0391 已迁移：`\df control.nexloop_rotate_service_credential_scopes` 有结果。
- 当前令牌：

  ```sql
  select status, expires_at, binding->'requested_scopes' from authz.nexloop_service_credentials
   where credential_id='nexloop-service-catalog-maintainer-key' order by expires_at;
  ```

  R65 之后预期两行 active，到期分别是 2027-01-12 和 2027-01-31，scopes 都是 `["action.execute"]`。**把较早那行的 `expires_at` 原样记下**（第 1 步要用），记入证据。

### 1. 收口两枚 active 令牌

1. 管理机树里已经是 2027-01-31 那一枚（R65 已 switch、finish）。把树同步到 DATA_HOST 的 `$RUN`，做法同 run-i31 第 1 步。
   核对：`stage_secrets.py check --out <树>` 预期 ok；`$RUN/management/service-secrets.json` 的摘要与管理机一致（`sha256sum` 对比，不看内容）。
2. 撤销较早的那一枚（DATA_HOST，配置员窗口）：

   ```bash
   docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py revoke-credential --out /stage --role catalog_maintainer \
     --superseded --expires-at '<第 0 步记下的较早 expires_at>' --request-id <固定 UUID，记入证据> \
     --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID"
   ```

   - 预期：`"revoked": 1, "active_tokens": 1, "superseded": true`；同一命令再执行一次得到 `"replayed": true`。
   - 拒绝（`credential_revoke_refused:42501`）时：通常是 `$RUN` 树仍是旧令牌（保留的令牌必须更晚到期），或到期时间与库里不完全一致。先回到第 1.1 条，不要改库。
   - 第 0 步的查询预期只剩一行 active（2027-01-31）。

### 2. 带 scopes 的轮换

```bash
python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role catalog_maintainer --prepare
```

同步到 `$RUN` 后，在 DATA_HOST：

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py rotate-credential --out /stage --role catalog_maintainer --apply \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID" --credential-expires-at "$NEW_EXPIRES"
```

- `$NEW_EXPIRES` 必须晚于 2027-01-31（函数要求晚于当前到期时间），建议 `2027-02-28T00:00:00+00:00`。
- 预期：`"rotated": true, "active_tokens": 2, "scopes_added": 1, "scopes_removed": 0`；再执行一次得到 `"replayed": true`，`authority_revision` 相同。
- 核对：第 0 步的查询里，新那行 scopes 为 `["action.execute","object_type.read"]`。

然后在管理机执行 switch → check → 同步 `$RUN`：

```bash
python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role catalog_maintainer --switch
```

确认 `$RUN` 已同步后，在 DATA_HOST 撤销旧令牌，再回管理机 finish 并同步：

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py revoke-credential --out /stage --role catalog_maintainer --previous \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID"
```

```bash
python3 <检出>/scripts/deploy/stage_secrets.py rotate-credential --out <树> --role catalog_maintainer --finish
```

- `--previous` 预期 `"revoked": 1, "active_tokens": 1`；拒绝 `switch to the new token first` 表示 `$RUN` 没有同步。
- finish 之后同步时，`$RUN/management/rotation/catalog_maintainer/` 也要删除。

### 3. register 与核对

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --dry-run ...
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage ...
```

- 正式执行预期 `changed: true`，写入 R65 未写的 facts（4 个 Action grant、2 个类型 READ、property_access_rule、owner 限制，共 38 条左右）；再执行一次预期 `changed: false`。
- register-check（`service_grants --doctor`，只读）预期 `in_sync: true`，`credentials` 中没有 catalog_maintainer。
- 然后回到 run-incentive-catalog 第 3 步的核对。

## 撤回

- 第 1 步撤销和第 2 步的 `--previous` 都不能撤回。
- apply 之后、`--previous` 之前可以 `--switch-back`；但 scopes 已变，旧令牌的判定仍然是 stale。要回到旧 scopes，就用旧授权清单再做一次轮换。
