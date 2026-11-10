# NX-067 工作台 SSO：IdP 组只降不升，以及 IdP 停用后的同步

- 状态：设计稿，2026-11-10，L4，**待 L6 审**。负责人 11-10 决定：NX-065 的两项缺口另开任务补上。
- 基线：迁移 0365（OIDC 登录与绑定）、0404（AUTH-DOS：会话绑定 `session_epoch`）、0116 和 0287（角色清单与授权事实）、`nexloop_eios/workbench_oidc.py`；NX-065 验收脚本 `scripts/acceptance/nx065_oidc_acceptance.py`。
- 迁移临时号从 0446 起。

## 1. 不变的部分

- 本地口令登录不变：没有配置组映射的 provider、没有绑定的成员，行为与现在完全一样。
- 租户、身份和 provider 都取自服务端：租户来自 api 配置，provider 来自登录流程开始时存下的 state，成员来自绑定表。浏览器和 IdP 送来的内容只作为待核验的声明。
- 只有受治理的 Action 能改正式对象：映射配置只能通过新的负责人 Action 修改。登录和 back-channel 只写身份侧的状态（角色上限、`session_epoch`），和现在的锁定、改密码属于同一类。
- 负责人仍然不能用 SSO 登录（0365 F1），组映射也不能产生 owner。

## 2. 组映射：只降不升

### 2.1 规则

- 映射是“IdP 组 → 角色”，角色只能是 `operator` 或 `reviewer`，**不能是 `owner`**（SQL 校验）。
- 每次 SSO 登录时，从 ID token 的组 claim 算出**上限角色集合** C：所有命中的组映射到的角色。
- 成员的有效权限 = 角色清单给的角色 R 的 Action 集合 ∩ C 中各角色 Action 集合的并集。**取的是交集**：
  - R 不在 C 里也不会换成别的角色。比如清单里是 operator、组只给 reviewer，有效权限就是 operator 和 reviewer 共有的 Action，不会因此拿到 reviewer 独有、operator 没有的 Action。
  - 所以无论 IdP 怎么配，有效权限永远是清单角色的子集。
- 上限记在绑定行上（`control.nexloop_workbench_external_identities` 新增 `role_ceiling text[]`，null 表示没有上限），每次 SSO 登录时重写。它对这个成员的**所有会话**生效，包括 `sso_or_password` 下的口令会话，否则 IdP 降了级、成员换口令登录就能绕过去。
- 上限只在 SSO 登录（以及 back-channel，见第 3 节）时更新。IdP 侧改了组，要等成员下一次 SSO 登录才生效；第 3.3 节的 SSO 会话最长时长给出了这个延迟的上限。

### 2.2 缺失、格式错误和未配置时怎么处理

| 情况 | 登录 | 上限 |
|---|---|---|
| provider 没有配置组映射 | 照常登录 | 不设上限（null，与现在一样） |
| 已配置映射，token 里没有组 claim，或者 claim 不是字符串数组（最多 256 项，每项 1–256 字符） | 拒绝，原因 `groups_invalid` | 不变（无法推断，不去改动） |
| 已配置映射，组 claim 合法，但没有命中任何映射 | 拒绝，原因 `no_mapped_role` | 设为空集合 `{}`：口令会话也没有任何 workbench Action 权限，直到下一次 SSO 登录能算出上限，或者负责人解绑 |
| 命中映射，交集为空（例如清单角色是 reviewer，组只映射到 operator，而两者没有共有 Action） | 拒绝，原因 `no_mapped_role` | 设为 `{}` |
| 命中映射，交集非空 | 照常登录 | 设为 C |

任何一种情况都不会把权限放宽到超出现在的范围。拒绝时浏览器得到的跳转和现在一样（`/workbench?sso=failed`），原因只写进审计。

### 2.3 映射配置由谁改

- 新表 `control.nexloop_workbench_oidc_group_mappings`（tenant, provider_id, revision, group_claim, mappings jsonb，`{组名: 角色}`，最多 64 项）。启用 RLS 并强制（force），policy 按租户。
- 新的负责人 Action `nexloop.workbench_member.set_sso_group_mapping:1`，走现有的受治理 Action 路径（幂等键、请求 ID、审计）。请求体包含 `provider_id`、`group_claim`（默认 `groups`）、`mappings`、`expected_revision`（并发保护）。provider 必须在 provider 文件里。`mappings` 为空表示撤销映射，所有绑定的上限清回 null。
- 需要同步更新：business-actions 注册、`test_stage_register_pg` 的计数、角色清单里 owner 的 Action 列表、契约和 example。
- provider 文件**不**承载映射：那是技术配置员管的私有文件，映射属于授权策略，归负责人。

### 2.4 在哪里执行上限

- `control.nexloop_workbench_role` 不改，它只回答清单角色。
- 实现为 `control.nexloop_workbench_action_permitted(tenant, principal, resource)`：按单个 Action 判断，结果与“清单角色的 Action 集合与上限的交集”相同。只管工作台角色里的 Action；`nexloop.workbench.read` 只要求上限非空（reviewer 的角色里没有这个 Action）。
- 在三个地方执行（实现时核实：工作台成员的授权都经过这三处）：
  1. 工作台入口挑选 Action（`authz.nexloop_workbench_action_for`）：被排除时返回 `granted: false`，入口回 403。
  2. 浏览器授权读取 grant 事实（`authz.nexloop_browser_authority_fact_snapshot`）：被排除时不返回这条事实。存储的事实自带快照校验和，不能改写，所以只能不给；于是这次授权判定无法完成，按“不可用”失败关闭（503，什么都不执行）。读接口也经过这里。
  3. 受治理 Action 的派发事务（`authz.nexloop_goal_governed_action`）：用签名命令里的租户、主体和 Action 资源再查一次，被排除时报 42501，入口回 403。这是派发时的复核。
- 守护测试 `tests/test_nx067_ceiling_guard_pg.py`：从 `pg_proc` 里找出所有读写 grant 事实的函数（27 个），逐个登记是否适用上限、以及原因；新增的函数没登记就失败。三个执行点必须调用 `nexloop_workbench_action_permitted`，被包装的原函数不能再有任何直接授权。

### 2.5 审计

- 新的只追加事件表 `control.nexloop_workbench_role_ceiling_events`（tenant, principal, provider_id, from_ceiling, to_ceiling, reason, mapping_revision, at）。上限每变一次写一条（没变就不写），同时写一条 `runtime.audit_events`，action 为 `nexloop.workbench_member.sso_role_ceiling_changed`。
- 只记角色和映射版本，**不记组名、sub 或令牌**。组名可能暴露组织结构，排查时按映射版本去对照。
- 映射配置的每次修改都由 Action 本身审计，记录请求人、修改前后的映射和版本。

## 3. 停用同步

### 3.1 结论：两个机制组合使用

1. **SSO 会话设最长时长（必做，给出硬上限）**：provider 文件新增 `max_session_seconds`（900–43200，默认 28800，即 8 小时）。SSO 会话的 `absolute_expires_at` 取 `min(创建时间 + max_session_seconds, 创建时间 + 30 天)`，空闲超时仍是 7 天和这个值中较小的。到期后必须重新经过 IdP 登录，而 IdP 会拒绝已停用的用户。
2. **OIDC back-channel logout（IdP 支持时启用，接近即时）**：IdP 停用用户或结束会话时，推送一个 logout token；验证通过后，让这个成员的 `session_epoch` 加一，结束它的全部会话。

不选“刷新时向 IdP 复核”：这需要 refresh token 或者 `prompt=none` 的静默重定向，前者要在服务端保存长期令牌（新的密钥面），后者依赖浏览器第三方 cookie，托管 IdP 下经常失败。到期后重新登录，效果相同，实现也更简单。

SCIM 不在本任务范围：上面两个机制已经能给出可测的上限；SCIM 解决的是开户和撤户，而我们本来就不自动开户。

### 3.2 可测的上限

IdP 停用用户后，这个用户已有的 SSO 会话最晚在以下时间内失效：

- 支持 back-channel：IdP 推送成功后立即失效（下一个请求就是 401）。
- 不支持或推送丢失：最晚 `max_session_seconds` 加 60 秒（时钟容差）。默认值下是 8 小时 1 分钟。

测试用 mock IdP 和可控时钟，两条都断言。验收脚本里 `disable.sync` 改为：先用 back-channel 断言立即失效；再在 back-channel 关闭的情况下，用调小的 `max_session_seconds` 断言过期后失效。

### 3.3 back-channel 端点

- `POST /api/v1/workbench/auth/oidc/backchannel/<provider_id>`，`application/x-www-form-urlencoded`，参数只有 `logout_token`，请求体不超过 16 KB。
- 不带会话 cookie，不要求 CSRF 和 Origin（这是 IdP 的服务器在调用，不是浏览器）。鉴别**完全依赖 logout token 的签名**。只有 provider 文件里开了 `backchannel_logout: true` 的 provider 才开放这个端点，否则一律 404。
- 验证（按 OpenID Connect Back-Channel Logout 1.0）：
  - RS256，用这个 provider 的 JWKS（与登录同一套缓存和刷新逻辑）；
  - `iss` 等于 provider 的 issuer，`aud` 包含 client_id；
  - `iat` 不在未来（容差 60 秒），也不早于 10 分钟以前；有 `exp` 的话不能已过期；
  - `events` 是对象，并且包含键 `http://schemas.openid.net/event/backchannel-logout`，值是对象；
  - 必须有 `sub`。只有 `sid` 的 token 拒绝，因为我们不保存 IdP 的 sid；
  - 不能有 `nonce`（规范禁止）；
  - `jti` 必填，1–255 字符。
- 防重放：新表 `control.nexloop_workbench_oidc_logout_jti`（tenant, provider_id, jti_hmac, expires_at），只存 HMAC；主键冲突即视为重放，返回 400。保留到 `iat + 10 分钟 + 容差`；过期的在每次收到 back-channel 请求时由同一个函数删除。
- 效果：在一个事务里按 (provider, issuer, sub) 找到绑定；找到就让成员账号的 `session_epoch` 加一，写登录事件 `oidc_backchannel_logout`（不含 sub 和令牌）。sub 没有绑定的也返回 200，避免通过响应探测哪些 sub 已经绑定。
- 响应：成功 200，`Cache-Control: no-store`；验证失败 400，错误体只有 `invalid_request`，原因写进审计。
- 限速：沿用已注册的 `oidc_start` 策略，按来源地址计数，但用单独的键前缀 `backchannel-ip:`，和登录的计数分开（与 callback 的 `callback-ip:` 做法相同）。没有另建 `oidc_backchannel` 策略：限速函数只接受固定的几种 action，加一种要改动已发布的函数。没有这条策略时端点返回 503（失败关闭）。验签失败也计入次数。
- 让 `session_epoch` 加一会结束这个成员的全部会话，口令会话也包括在内：IdP 停用通常意味着人已经离开，这样处理更保守，和解绑的效果一致。

## 4. 测试计划（mock IdP，没有任何外部请求）

- 组映射：没有映射时行为不变；operator 的组只给 reviewer 时，有效 Action 是交集；组试图给 owner 时，配置被拒；token 没有组 claim 或格式错误时登录被拒、上限不变；没有命中时登录被拒、上限为空集、口令会话也执行不了 Action；撤销映射后上限清回 null；事件表和审计里没有组名；`set_sso_group_mapping` 不是负责人、版本冲突、provider 未知时都被拒。
- 停用同步：token 合法时会话立即失效；重放、签名错误、iss 错、aud 错、没有 events、带 nonce、只有 sid、过期都返回 400，并且会话不受影响；sub 未绑定时返回 200；provider 没开启时 404；限速；没有策略时 503。最长时长到期后会话失效，口令会话不受这个时长影响。
- 全局守护：新表跑 db_boundary、tenant_fk、system_catalog、copy guard、擦除覆盖（成员擦除要覆盖上限事件表和 jti 表）、nx041 sim、claim_store；新 Action 更新 `test_stage_register_pg` 的计数；新契约带 example。
- 所有新测试都做变异检查。
- 验收脚本：`groups.mapping` 和 `disable.sync` 改成真正的检查，用 mock 跑到退出码 0。

## 5. 待 L6 判断的点

1. 上限对口令会话也生效（2.1）。另一种做法是只约束 SSO 会话，但那样成员换口令登录就能绕过降级。
2. 没有命中映射时把上限设为空集（2.2），这会把成员的口令会话也锁住，直到重新 SSO 或被解绑。
3. back-channel 直接让 `session_epoch` 加一，口令会话一起结束（3.3）。
4. `max_session_seconds` 默认 8 小时（3.1）。

## 负责人 2026-11-10 确认（取舍 a–d，“都按推荐”）

- a：一个映射都没命中时，锁住这个成员的全部工作台 Action（上限设为空集）。
- b：SSO 会话最长时长默认 8 小时，每个 provider 可以在 900–43200 秒之间调（`max_session_seconds`）。
- c：接受 IdP 管理员能够让成员降级，也能通过 back-channel 结束成员的全部会话，口令会话也包括在内。
- d：清空映射后，恢复角色清单给的权限（上限清回 null）。

## 6. L6 复核后补充（Z1–Z5，2026-11-10）

L6 判设计条件 PASS，第 5 节四点都同意。负责人的产品取舍先按设计现值做：没有命中映射时锁住全部权限；最长时长默认 8 小时，每个 provider 可以在 900–43200 秒之间调；只有清空映射才把上限清回 null。这几个值都写成一处常量或 provider 字段，方便以后修改。

- **Z1 上限只从当前绑定行读取**：`nexloop_workbench_effective_actions` 每次调用都直接读 `control.nexloop_workbench_external_identities.role_ceiling`，不缓存，也不另存一份到会话或授权事实里。成员被提升为 owner 时，0365 的触发器 `nx058_unlink_promoted` 会删掉绑定行，上限随之消失。之后 back-channel 再用这个 sub 也找不到绑定（返回 200，但没有任何效果），owner 的会话不受影响。测试同时断言这两点。
- **Z2 映射修改时强制重新 SSO**：`set_sso_group_mapping` 在同一个事务里：
  - 映射改成另一个非空映射时：这个 provider 的每个绑定，上限设为 `{}`，成员账号的 `session_epoch` 加一，结束它的全部会话，各写一条上限事件（原因 `mapping_changed`）。成员要重新 SSO 登录，才能按新映射算出上限。**不会**清回 null。
  - 清空映射（`mappings` 为 `{}`）：才把这个 provider 的所有上限清回 null（原因 `mapping_cleared`）。会话不结束，因为这一步只会放回清单角色，不会放宽到超出清单。
  - 第一次设置映射（之前没有映射）也按“修改”处理：上限设为 `{}`，`session_epoch` 加一。否则在成员重新登录之前，已有会话不受新映射约束。
- **Z3 JWKS 刷新限频与密钥轮换**：沿用 `OidcClient.keys` 现有的做法，写明并测试：
  - 每个 provider 缓存一份 JWKS，超过 300 秒视为过期，下次用到时重新拉取。
  - 遇到未知的 kid 时立即刷新，但同一个 provider 两次拉取至少间隔 `JWKS_MIN_REFETCH_SECONDS`（60 秒）。间隔内再遇到未知 kid 直接判 `id_token_invalid`，不发请求，所以伪造 kid 打不出拉取风暴。
  - 轮换期间，IdP 会在 JWKS 里同时发布新旧两把 key，我们按 kid 选 key，两把都能验证；旧 key 从 JWKS 中移除后，下一次刷新就不再接受它。拉取失败时保留旧缓存，但只用到它过期为止。
  - logout token 和 ID token 共用这份缓存和限频。
- **Z4 验收脚本分开报告**：
  - `groups.mapping`：provider 没有配置映射时报 `not_configured`，算 gap，退出码 3，不能报 passed。配置了映射时，按第 2 节真正检查：组只降不升、没有命中会被拒。
  - `disable.sync` 拆成两项：`disable.sync.backchannel`（provider 没开 back-channel 时报 `not_configured`，算 gap）和 `disable.sync.max_session`（证明会话在 `max_session_seconds` 加 60 秒内失效）。只有两项都通过时退出码才是 0。
- **Z5 调用点守护**：所有判断“成员能不能执行某个 workbench Action”的 SQL 函数，都必须经过 `nexloop_workbench_effective_actions`。用守护测试固定这一点：从 `pg_proc` 里列出所有引用 `nexloop_workbench_granted` 或 `nexloop_workbench_actions` 的函数，与登记表对比，多出未登记的就失败。派发时还会再调用一次，测试断言：规划时有权限、派发前上限被收窄，派发会被拒绝。

## 否决的做法

| 做法 | 否决理由 |
|---|---|
| 组 claim 直接决定角色（可以升级） | IdP 管理员就能给成员任意加权限，违反“只降不升” |
| 降级时换成映射到的那个角色 | reviewer 和 operator 的 Action 不是包含关系，换角色可能拿到清单里没有的 Action；取交集才能保证只减不增 |
| 映射写在 provider 私有文件里 | 那是技术配置，不经过受治理的 Action，也没有审计；映射属于授权策略，归负责人 |
| 登录时发起一次真正的角色变更 Action | 登录不是负责人的决定，不能伪造负责人身份去执行 Action；上限属于身份侧的状态 |
| 刷新时向 IdP 复核（refresh token 或 `prompt=none`） | 要在服务端保存长期令牌，或者依赖第三方 cookie；设定最长时长后重新登录，效果相同 |
| SCIM | 我们不自动开户，停用的上限已经由前两个机制给出；SCIM 的工作量大，也不在本任务范围 |
| back-channel 只结束 SSO 会话 | 停用通常意味着人已离开，留着口令会话等于没有同步 |
