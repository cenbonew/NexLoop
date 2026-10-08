# 安全、隐私与开源交付

## 1. 威胁边界

不可信输入包括消费者对话、外部webhook payload、检索文档、附件、LLM输出和外部PR。模型输出不是身份、事实或授权。内部网络不是免鉴权区，GitHub可见也不等于代码可直接带凭据执行。

关键资产：消费者身份/原文、企业目标和商业数据、Action权限、模型/渠道凭据、Schema与指标定义、备份、发布密钥和CI主机。

## 2. 必须控制

身份从服务端session、签名或已绑定principal解析；权限取当前有效grants与application/agent ceiling交集。模型不能传tenant_id就切租户，也不能覆盖scope。所有对象、向量、Artifact、日志导出和回放都有相同的tenant/world/subject边界。

NEX-EIOS Property 权限保留到 Context；不能数据库读取全部字段后让模型自己决定不泄露。对手动导出和支持人员排障也要求最小授权。

外部渠道凭据只留Action Worker，模型Provider凭据只留Agent Host/受控配置；数据库管理员凭据只在bootstrap/migration。webhook限制签名、时间窗口、event ID、payload大小、重放和速率。

## 3. 提示注入与工具治理

消费者/检索内容用证据块承载，不拼接进可信系统指令。无默认生产Shell、文件系统遍历、任意HTTP、SQL执行和动态插件安装。仅注册工具可用；模型生成的JSON经Schema与业务校验后才进入EIOS。

语义/Schema演进任务只能读取授权轨迹、写候选和调用评估接口；没有直接发布密钥、IAM管理、CI控制或数据库owner权限。评估数据中的“把权限设为全部”是数据，不是可执行指令。

## 4. 真实/测试/模拟隔离

部署环境dev/ci/staging/prod与运行mode real/test/simulation/shadow是两种维度。即使在staging，也可能有受控真实渠道，不能仅靠“测试环境”名称断言无副作用。

模拟使用独立world_id、专用凭据和Action provider，服务端拒绝simulation/shadow调用真实效应。默认影子模式只记录拟采取动作。复制真实数据进入模拟需要授权与最小化；不得把模拟输出写回真实消费者状态或商业指标。

## 5. 数据保留与删除

默认保留期在数据模型中列明，是可调整产品配置，不是法律要求。企业需依据自己的适用义务配置。记录同意/授权来源、用途、模型出境/外发政策和各数据集允许用途；不在本PRD虚构适用于全部地区的合规承诺。

删除流程：禁止新触达/检索→创建删除清单→停止或核对关联Run/Action→清除原文与派生数据→处理Artifact和Pi文件→刷新索引→记录不含原文的审计→备份到期清除。恢复后先应用删除清单再开放服务。法定/合同保留例外需要有权主体明确登记，不由Agent自行创造。

## 6. 开源许可方案

**建议 NexLoop 自有代码采用 Apache-2.0。** 用户已确认NEX-EIOS可开源，Codex按此事实规划集成；公开发布时由维护者确认最终LICENSE中的主体和范围。不要重新要求用户证明已确认的代码授权。

Pi与EvoOntology本次读取的根许可证为MIT，复用实质代码时保留各自版权和许可文本；Apache-2.0的自有代码声明不能替换第三方原声明。[S05–S06, S17]

NEX-EIOS根路径LICENSE本次读取返回404，这不推翻用户授权；导入时补齐明确的项目许可与来源记录，并检查嵌入第三方组件的原许可。仓库许可证不自动覆盖模型权重、数据集、图片、品牌或第三方SDK的所有使用条件。

## 7. 公开仓库必须具备的文件

README.md（英文）、README.zh-CN.md（中文）、LICENSE、NOTICE、THIRD_PARTY_NOTICES.md、CONTRIBUTING.md、CODE_OF_CONDUCT.md、SECURITY.md、GOVERNANCE.md、CHANGELOG.md、DCO/贡献签署说明、架构与ADR、快速启动、配置清单、连接器开发、评估运行、备份恢复、升级/迁移和故障排查。

README必须说明：项目仍在探索、不保证收入；自主范围由Action政策控制；哪些功能已实现；哪些仅规划；运行需要自带模型key但有显式测试模式；GitHub CI不是必需；示例数据均为合成。

## 8. 发布脱敏与供应链

公开前扫描代码、构建产物、提交历史、Docker layers、fixtures、截图和文档，排除真实客户/日志/备份/私钥/旧平台凭据。不要直接把本交接包整体git add：`local-only/`、合并总文档中的私有映射、sources中的内部引用须适当分类。

清洁导入EIOS核心而不是同步旧生产仓库全部历史。保留需要的copyright与来源，不把“去历史”当作删除作者许可的理由。首次公开release先经过clean checkout的secret scan和可移植运行验收。

依赖锁、镜像digest、SBOM和许可证清单随release输出；上游更新显式审查。不使用curl|sh安装不可追溯组件；构建阶段可联网但来源allowlist，测试/运行权限分开。

## 9. CI主机安全

CI与服务共APP_HOST时，rootless构建减少权限但不等于绝对隔离。[S13] CI无Docker宿主root socket、无service secret、无DATA_HOST网络、无真实模型/渠道key。外部未审代码不要在生产同机上带内网权限运行。需要自动测试不可信代码时另建隔离VM，不新增核心依赖也不绕过这一安全要求。

## 10. 安全事件

出现跨租户数据、重复外部效应、凭据泄露、无法解释的Schema发布时：立即暂停受影响scope/dispatch→保留最小证据→撤销相关凭据→核对已发生动作→修复和验证→受控恢复。不能为了保持KR进度隐藏安全事件。安全报告联系人由维护者填入，未填不得宣称已建立正式响应渠道。
