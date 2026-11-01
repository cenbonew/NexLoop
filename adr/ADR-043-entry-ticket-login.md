# ADR-043｜入口票据登录：owner 进入合成测试账号

状态：**采用**（负责人 2026-11-01 确认 Q1–Q7 按推荐；本 ADR 的文字待负责人一句确认，不阻塞实现）。
关联：S81 设计 v2（L6 PASS）；ADR-034（测试人设标记）；ADR-023（明确拒绝硬停）；ADR-039（统一审批）；迁移 0392。

## 背景

负责人走查「管理 › 入口」时，希望两条外链点开即用：
- WebChat（合成客户登录）；
- 参考端点员工页面。

手动输入测试口令会把秘密扩散到更多地方。工作台后端代登录再转交 Cookie 的做法也不行：参考端点在另一台主机上，浏览器不允许工作台为它设置 Cookie；而且这样工作台必须持有测试口令。

## 决定

允许 owner 在 stage 上，不出示目标账号的凭据，建立以下两种会话之一：
- 一个 **WebChat 合成测试客户**的普通浏览器会话，认证方式记为 `entry_ticket`，与 password、oidc 并列；
- 一个**参考端点测试员工**的只读会话。

前提是**两个条件同时满足**，任何一个不成立就拒绝，并写审计：

1. **部署配置钉住目标**：WebChat 的目标是 `ENTRY_TEST_WEBCHAT_PRINCIPAL`，参考端点的目标是测试员工标签（默认 `entry-test`）。签发请求只能说去哪个系统（`webchat` / `reference_staff`），不能指定账号。
2. **服务端在数据库里核实目标是合成测试客户**（只适用于 WebChat，签发和兑换时各核实一次）：
   - 该 principal 在 world real 下拥有**恰好一个** Consumer（0046）；
   - 该 Consumer 的 persona 为 `simulated_agent`（ADR-034）；
   - 该 Consumer 不在擦除中（0383）；
   - 该 Human 的 membership 是 active。

其余约束：
- **调用者**：调用者的工作台会话在 SQL 里按令牌摘要重读，成员**当前**角色必须是 owner（Q2）。
- **票据**：单次使用，有效期 30 s（Q1，上限 60 s）。只经 POST 体传递，不出现在 URL、日志、前端构建或配置文件里。数据库只存 sha256。参考端点的票据用专用 HMAC 密钥签名，jti 持久化防重放。
- **开关**：进程环境变量 `NEXLOOP_ENTRY_TICKETS` 只认值 stage；只有 stage 的 compose 会传这个变量，任何公开文件都不会把它设为 stage（check_publication）。
- **会话**：生成的会话就是该 principal 的普通会话，权限不扩大。ADR-023 的联系限制和 ADR-039 的审批照常生效。
- **审计与擦除**：签发和兑换都有审计，保留 30 天（retention keeper）。consumer 擦除和 member 擦除会把审计里的 principal 假名化，并清空客户端摘要。

## 不允许

- 目标是 real persona，或任意未钉住的 principal（T13、T16）；
- 非 owner 调用（T4）；
- 非 stage 部署（T15）；
- 用这条路径执行任何 Action、写业务对象或 authority facts。

## 后果

- 多了一种"代入身份"能力，但它被限制在合成测试账号上。owner 账号即使被盗，攻击者也只能进入合成测试客户，接触不到真实客户。
- vendored EIOS 不改：会话模型对 `authentication_methods` 只要求唯一、非空，没有枚举；versions.lock 不需要新增 divergence 记录。
- 参考端点的兑换结果首版只记在端点本地，保留 30 天，不回传工作台（Q5）。

## 备选（否决）

- **后端代登录再转交 Cookie**：跨主机不可行，而且要扩散口令。
- **工作台持有测试口令代填**：违反"凭据不进前端、不在浏览器持久保存"的约束。
