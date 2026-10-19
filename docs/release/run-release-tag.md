# 发布 tag 执行单：负责人签名的 `vX.Y.Z` → LAN 仓库 → GitHub（2026-10-20）

适用于 `v0.1.0` 以及以后的版本 tag（`s5-owner-checklist` 6.5、7.10）。只用占位符，不写主机名和地址。

**类别**：**B** 负责人本人；**A** 调度员。

## 0. 机制（NX-033 切换后的 ref 规则）

- **谁能建 tag**：LAN 仓库的 pre-receive 只允许 **maintainer**（负责人）或 **release**（CI 主机的发布工具）新建 `refs/tags/vMAJOR.MINOR.PATCH`（可带 `-rc.N`）。tag **不可移动、不可删除**（`tag_immutable`）。developer、integrator 推 tag 一律拒绝。
- **负责人的 tag 在入库时验证**（`nexloop_ci.version_tag_problem`），任一不满足就拒绝，并给出原因码：
  - 名称是版本号（`tag_name_invalid`）；
  - 是 annotated tag，轻量 tag 不行（`tag_not_annotated`）；
  - tag 对象里写的名字与推送的 ref 一致（`tag_name_mismatch`）；
  - 指向的提交已经在 main 里（`tag_not_on_main`）；
  - SSH 签名能按负责人的 allowed signers 文件验证（`tag_signature_invalid`）；文件缺失时一律拒绝（`tag_signers_missing`）。
- **同步到 GitHub**：部署中的 `nexloop_ci.py sync`（每分钟一轮，用户 `nexloop-git`）在带 `--tag-signers` 时，会把 LAN 仓库里的每个 `v*` tag **再验证一次**，然后**逐个用明确的 refspec** 推送，只新建。
  - 不使用 `--tags`、`--force`、`--mirror`、`--all`，不删除任何远端 tag。
  - 已同步且对象相同的 tag 不再重复推送。
  - GitHub 上同名但对象不同的 tag 记为该 tag 的 `sync_refused`，不覆盖。
  - 验证不过的 tag 记为 `tag_unverified`，不推送。
- **备选（手动、带 §3.2 全部门禁）**：`scripts/release/sync_github.py --tag vX.Y.Z --tag-signers <ALLOWED_SIGNERS>`（NX-034 §3）。它需要私有的受限标识符清单和同步基线，目前没有部署，所以不作为主路径。

## 1. 一次性准备

| # | 谁 | 做什么 | 预期 |
|---|---|---|---|
| 1.1 | B | 准备 tag 签名用的 SSH 密钥（建议单独一把 ed25519，只用于签 tag），把**公钥**交给调度员 | 公钥一行 |
| 1.2 | A（需负责人确认） | 在 CI 主机上写 `<GIT_ROOT>/private/allowed_signers`，内容一行：`<负责人邮箱> namespaces="git" <公钥>`；属主 root、组 `nexloop-git`、权限 0640（pre-receive 与同步都以 `nexloop-git` 读取它，`nexloop-ci` 不需要） | `sudo -u nexloop-git cat <GIT_ROOT>/private/allowed_signers` 能读；其他用户不能写 |
| 1.3 | A（需负责人确认） | 受信安装先更新到包含本改动的 main（`<TRUSTED_CI_DIR>` 换成新的 `<sha12>` 目录）。pre-receive hook 的 exec 行加 `--tag-signers <GIT_ROOT>/private/allowed_signers`（例如 `exec python3 <TRUSTED_CI_DIR>/scripts/ci/nexloop_ci.py pre-receive --repo "$PWD" --identities <GIT_ROOT>/private/identities.json --tag-signers <GIT_ROOT>/private/allowed_signers`）；同步 unit 的 `ExecStart` 末尾加同样的 `--tag-signers <GIT_ROOT>/private/allowed_signers`，然后 `daemon-reload`。（`deploy/stage/ci/` 的公开示例本次未改，留待下一批示例更正时同步。） | hook 与 unit 都带 `--tag-signers`；`systemctl cat nexloop-ci-sync.service` 能看到 |
| 1.4 | B | 负责人自己的检出：`git config gpg.format ssh`，`git config user.signingkey <私钥路径或公钥>`，远端 `origin` 为 LAN 仓库，推送身份为 maintainer | `git config --get gpg.format` 为 `ssh` |

## 2. 签名并推送（B，负责人本人）

1. 确认要发布的提交 `<RELEASE_SHA>` 已在 main 上（与发布清单、验收证据中的提交一致）：

   ```bash
   git fetch origin && git merge-base --is-ancestor <RELEASE_SHA> origin/main && echo on-main
   ```

2. 签名并推送（一条命令完成签名，一条命令推送）：

   ```bash
   git tag -s -m "NexLoop v0.1.0" v0.1.0 <RELEASE_SHA>
   ```

   ```bash
   git push origin refs/tags/v0.1.0
   ```

   - 预期：`remote: {"accepted": true, ...}`。被拒时输出里有原因码，对照 §0 处理。
   - 不要用 `--no-verify`，也不要推到 `github`：推送前钩子只允许推 `origin`。
   - tag 一旦推上去就不能移动或删除。如果签错了，换一个新版本号，例如 `v0.1.1`。

## 3. 同步到 GitHub（自动，A 核对）

同步 timer 在一分钟内会推送这个 tag。核对：

```bash
sudo -u nexloop-git python3 <TRUSTED_CI_DIR>/scripts/ci/nexloop_ci.py status --queue <GIT_ROOT>/queue
```

预期 `sync.github` 里没有 `tags_not_synced`。若出现 `tags_not_synced`，在 `<GIT_ROOT>/queue/sync/github.json` 的 `tags` 下查看原因：

- `sync_pending`：GitHub 暂时不可达，属于正常的自愈状态，等下一轮；
- `sync_refused`：GitHub 上已有同名但对象不同的 tag，停下来请负责人决定，不要覆盖；
- `tag_unverified`：allowed signers 与签名不符，检查 1.2。

## 4. 在 GitHub 上验证（B 或 A）

在任意一台机器的干净目录：

```bash
git clone --no-checkout <GITHUB_URL> nexloop-verify && cd nexloop-verify
```

```bash
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=<负责人公开的 allowed signers 文件> verify-tag v0.1.0
```

预期 `Good "git" signature for <负责人邮箱>`，并且 `git rev-parse v0.1.0^{commit}` 等于 `<RELEASE_SHA>`。随后按 `s5-owner-checklist` 6.5 发布 GitHub Release（源码归档与 SHA256、公开版清单、SBOM；不附镜像与测试日志）。

## 5. 待负责人决定（D）

- **D-tag-1：是否保留 rc tag。** 选项：
  - (a) 直接打 `v0.1.0`；
  - (b) 先打 `v0.1.0-rc.<n>`，再打 `v0.1.0`。

  两种 tag 名都已支持。**推荐 (a)**，理由：
  - 验收已在发布 22 的 stage 部署上完成，再加一轮 rc 不会带来新的证据；
  - 推到 GitHub 的 tag 不可删除，rc tag 会永久留在公开仓库里；
  - 如果 `v0.1.0` 之后需要修复，按语义版本发 `v0.1.1` 即可。
