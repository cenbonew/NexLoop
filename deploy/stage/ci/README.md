# 本地 Git 队列与隔离 CI：部署模板（NX-033）

这里的文件都是**示例**。尖括号中的值（主机、地址、端口、路径、用户、公钥、镜像）都来自 APP_HOST 的私有 env 文件或私有目录，公开仓库不写任何真实值。

建用户、建 bare 仓库、安装 hook、改防火墙、切换 remote，都是 B 类步骤，每一步都要负责人本人确认（设计稿 §8）。

| 文件 | 用途 |
|---|---|
| `pre-receive.example`、`post-receive.example` | bare 仓库 `<GIT_ROOT>/nexloop.git` 的 hook。pre-receive 按设计稿 §2.1 强制 ref 命名空间；post-receive 只入队，不构建 |
| `authorized_keys.example` | 每把公钥对应一个推送身份。身份由 forced command 固定，客户端无法自己声明 |
| `identities.example.json` | 推送身份与角色的对照：developer、integrator、maintainer、controller、release。真实文件为私有，权限 0600 |
| `runner.example.json` | rootless Podman 运行器配置。控制器启动时用 `check-runner` 检查，不通过就拒绝运行任何 job |
| `facts.example.env` | 运行器检查要比对的私有事实：受保护目录、数据主机 |
| `probe-targets.example.json` | 隔离探测（§3.3）的目标代号，以及每个代号对应的路径或端点 |
| `nexloop-ci-controller.service/.timer.example` | 控制器，用户为 `nexloop-ci`，同一时间只跑 1 个构建，CPU 权重低于运行服务（D5） |
| `nexloop-ci-sync.service.example` | GitHub 同步，用户为 `nexloop-git`：只快进、不 force、不 mirror；GitHub 不可达时记为 `sync_pending` |
| `release-lane.env.example`、`release-lane-identities.example.json` | NX-034 在现有 CI 车道上产出发布用 CI 结果（不切换合并控制器）：车道阶段需要的非秘密环境、入队身份。用法见 `docs/implementation/NX-034-release-ci-queue.md` |

程序和 `.ci-policy/` 都来自受信安装，即 main 上已发布的版本，从不使用候选代码中的副本（设计稿 §4.3）。
