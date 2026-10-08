# NX-018 实施前差距检查

基线 NX-017 已完成（实现 d12cf1081920393370e1263e1a99f1dd6e4a4554；状态 b0a9f34）。NX-018 已进入 in_progress；以下是实施前只读检查，建议不计完成证据。读取docs/03与05全文、NX-018 task与traceability；对应AT-004角色N:M、AT-008同名身份歧义、AT-009关系证据更正。未执行新测试，建议不计passed。

## 现有能力与实际缺口

- Consumer 已有generic EIOS CREATE/EDIT/READ；business_setup.py 71创建Consumer，当前最薄切片空properties，不能宣称完整档案（external_id_refs/display_name/locale/timezone/contact_preferences/lifecycle_status）。对象ID实际64hex；docs/03的目标UUID不能直接猜新物理表，新增业务ID/ref需明确兼容映射，不重写既有published协议。
- backend.py 136–151 exposes create_object/link_relation/edit_object/read_object；object_reads.py AuthorizedObjectReader逐Object/Property解析真实当前权限，SQL0011负责受限读取，API角色无表写权限。可复用受限投影，不另造可写customer表。
- relation_actions.py GovernedRelationLinker+SQL0016是真实受治理Relation LINK，校验注册端点类型/基数、Relation CREATE与两端Object EDIT/currentAction权限，stable claim；tests/test_relation_actions.py验证幂等、并发与受限role。**仅link**，backend未见end/supersede/read_relation专门接口，不能假定可表达时效更正。SQL0016以稳定endpoint relation_id复用既有关系而非创建新的历史依据，因此不要重link更正元数据当完整修订生命周期。
- Browser identity/ConsumerOwnership把已验证会话主体绑定Consumer；MessageAssignment与recipe.role_ref绑定窄Run。role_ref是配置引用，不是完整RoleDefinition职责/ceiling/N:M消费映射；当前未见可配置角色列表/consumer-role查询/角色关系变更API。两角色Source共用一个EffectIntent已有AT037结果不证明AT004的职责配置与无常驻Agent。
- 当前已验证Consumer关联不能被名称/LLM相似度覆盖。尚未找到同名候选的持久resolution状态或受限身份resolve port；AT008需要真实两会话保留独立Consumer和ambiguity，不靠概率最高自动合并。
- 当前Context v2只四类formal对象id/revision和独立原文，尚无关系当前证据/有效期读取组织。AT009需真实关系依据更正后投影排除旧推测，不能只改展示标签。

## 最小完整切片建议（依赖完成后实施）

1. 独立新bootstrap迁移，保留所有0010/0015/0016 published SQL原checksum。通过可信operating publisher正式注册Consumer所需最小属性、RoleDefinition与ConsumerRoleLink对象（或docs03允许的nl角色配置由治理Action唯一写入），N:M唯一范围tenant/world/consumer/role；职责、作用scope、ceiling_ref、valid_from/to、revision明确列/Schema。role map不授予权限，dispatch仍走EIOS实际Source/角色ceiling/current policy。
2. Governed Role create/edit + ConsumerRole assign/end，server derived tenant/world/principal，currentAction/Property权限、expected_revision、稳定intent、同事务审计/outbox、tail权限检查；不要沿用genericEndpoint EDIT给予模型泛化权限。role→consumer不可自动展开常驻Run，event只为当前适用责任触发短Run。
3. Governed关系证据更正Action与受限read-current投影：存两种时间valid/recorded、evidence refs、epistemic kind、revision、supersedes/ends依据；原记录不当当前事实，hypothesis只原文/假设区。AT009可先Consumer↔Consumer Peer证据更正的垂直切片；不要一次生成全部Problem/Entitlement空模块。
4. Consumer identity resolve只读port：优先当前已验证ConsumerOwnership/strong id，同名检索返回tenant/world/property过滤后的候选与ambiguity，不写合并；schema不让用户名称充当verified external id。真正身份合并留独立强证据治理流程，NX018不造自动merge。
5. Backend单一typed受限入口扩展、Browser真实session映射新独立Function权限；不要直接开放PostgresOntologyRegistry/pool/DML。对象投影沿用AuthorizedObjectReader逐Property授权，关系列表用owner function锁定当前tenant/world及endpoint READ，不跨租户返回候选计数或存在性。

## 必须实际执行的验证

AT004：真实PG两个Role关联同一Consumer、同Role多Consumer、scope/职责可配置、end/revoke当前映射立即影响适用查询/dispatch；Role映射本身不生常驻Agent；并发同intent真实effect只1（复用AT037真实exec再加角色映射）。同consumer不等于grant共享，两个role单独权限否定。
AT008：同名两个Consumer及真实Human会话 ownership，resolve输出ambiguous，不改任何Consumer/Ownership，不串Message/Context；强id唯一命中以及跨tenant/world/property权限过滤。
AT009：旧hypothesis关系→明确证据更正，Governed Action当前expected_revision，旧依据可审计但不进current projection/formalContext；幂等、冲突、撤销、租约过期tail回滚、受限role拒裸业务DML。

新增接口/Schema是否需要ADR按实际实现复核。所有fixture authority metadata用真实synthetic发布流程，正式Consumer/Role/Relation写只受治理Action。S3后续NX044检查仍在NX018之后，不能NX018顺便自动发布候选Schema。
