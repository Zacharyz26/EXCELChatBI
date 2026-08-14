# MCP 与 Docker 全阶段演进设计

> 状态：总体设计已完成；阶段 3–5、6A～6D 工程关闭，6E-1～6E-4 已本地实现
> · 更新日期：2026-08-14
> 范围：v2.4 基础能力完成后的 v2.5 阶段 3–6、独立安全项目和 v3.0 阶段 7–8

## 1. 文档定位

本文件回答“v2.4 把 MCP 和 Docker 跑通之后，每个后续阶段如何继续演进”。三份文档的
职责不同：

- [`Agent自主化开发规划.md`](./Agent自主化开发规划.md) 决定阶段顺序、范围和验收；
- [`v2.4/MCP与Docker架构决策.md`](./v2.4/MCP与Docker架构决策.md) 决定 v2.4 的协议、
  Client Gateway、Server、镜像和单机 Compose 基础；
- 本文件决定 v2.5/v3.0 如何在该基础上扩展，不重复定义另一套传输、工具 schema 或部署方式。

v2.4 阶段 2D 已把 Agent Executor 切到受治理 MCP Client Gateway，阶段 2E 已落地五个
独立工具服务、私网 Compose、分卷/secrets 和容器浏览器/重启门禁。本文描述的
v2.5 阶段 3–4 的实际交付与证据见
[`v2.5/README.md`](./v2.5/README.md)及其索引的实施记录；
阶段 3 已由真实 CI 关闭；阶段 4A–4D 已由提交 `7f81fe2` 的
[run 30980088817](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/30980088817)
完成 backend、frontend 与 Compose 三任务验证并正式关闭。阶段 5A 已完成版本化定义、
受控公式 Tool 和 Evidence 工程切片；5B-1 已实现按签名 project/subject 过滤的选择性 MCP
Resource list/read，5B-2 已贯通定义/数据 Claim 和旧报告复核，5B-3 已实现目录分页与
Resource 订阅通知；5B-4/5 的真实 Compose Resource 重连与 CPU/GPU 配置门禁已由提交
`d5a672d` 的 [run 31063896157](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31063896157)
确认全绿，阶段 5 工程关闭；runtime semantic 与领域签字债务保留。6A-1 已固定 TaskRun
capability/tool 目录，6A-2 已实现严格通知换代与 profile unavailable 投影，6A-3 已以
SQLite v10 建立共享预算、固定数据版本、取消树和统一 Evidence Ledger，并启用受限并行；
提交 `b67b704` 的完整 CI 与真实 Compose 恢复门禁全绿，6A 已关闭。
6B-1 已把 `data.roles`、确定性置信/歧义和只读质量建议接入 `get_data_profile` 1.1.0，
并以严格输出 schema、Planner 单次路由和 React Artifact 展示落地；6B-2 已补绑定计划/数据
版本的结构化确认和统计/聚合前置门禁；6B-3 已接入匿名代表性评测与
`data-role-quality` CI 门禁；6B-4 已接入 stdio/HTTP 契约等价、真实 `data-tools` Compose
调用、目录重验和重启恢复门禁；提交 `6b89ef6` 的
[run 31467721161](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31467721161)
已确认 backend、frontend 与 Compose 三项全绿并关闭 6B。6C-1 已把有界候选筛选结果
绑定 TaskRun 数据版本、Planner 审计与 React 人工选择；6C-2 已继续绑定不可变计划步骤、
Invocation、Evidence Ledger 与 Verifier 结果状态；6C-3 已实现受预算、重规划、取消树和
用户确认约束的确定性跟进；6C-4 已加入匿名评测、CI 强制门禁及 Compose 浏览器/三次恢复
探针；提交 `d5005ee` 的
[run 31659188951](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31659188951)
已确认 backend、frontend 与真实 Compose 三项全绿并关闭 6C。6D-1～6D-4 的统计
Evidence、高级统计、独立 forecast Tool/Profile、匿名质量门禁和预测传输/恢复探针已由
提交 `3febd68` 的
[run 31678576324](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31678576324)
完整 CI 关闭。当前 6E-1～6E-4 的只读预检、高风险固定 Join/多父血缘、React 协作、
脱敏质量门禁、双传输和 Compose 恢复已本地实现，待远程完整 CI。

## 2. 不随阶段变化的边界

### 2.1 MCP 边界

1. Agent Host 始终拥有目标、计划、记忆、TaskRun、审批和 Evidence Ledger；MCP Server
   只执行被授权的一次能力调用，不能读取完整对话或自行结束任务。
2. Planner 依赖 capability，Executor 经 MCP Client Gateway 选择具体工具。任何 Agent、
   后台 Worker 或兼容 API 都不能绕过 Gateway 直接调用远程 Server。
3. `inputSchema`、`outputSchema`、工具版本和后置条件单源生成；MCP annotations 只作为提示，
   不能代替服务器端权限、风险和结果检查。
4. Dataset、Artifact、知识和记忆默认使用 opaque reference。MCP Resource 只暴露经过评审的
   最小只读视图，不暴露本地路径、数据库凭据、完整会话或系统 Prompt。
5. stdio 用于本机和契约测试；Streamable HTTP 用于容器和远程服务。产品 SSE、MCP
   Streamable HTTP 和后台任务状态是三个不同协议，不复用 session/task ID。
6. 外部 HTTP MCP 必须使用受支持的授权流程，token 绑定目标 Server，禁止 token passthrough；
   Server 调用上游系统时使用独立凭据。
7. MCP Tasks、sampling、elicitation 或未来扩展只有经过独立 ADR、能力协商和安全评审后才能
   使用；不能靠协议新能力绕过 ChatBI 的 TaskRun、零 LLM 工具或审批边界。

### 2.2 Docker 边界

1. 镜像是不可变运行物，不是状态存储。Task、记忆、Dataset、Artifact、知识索引和审计必须
   写入声明的持久存储，并有升级、备份和恢复路径。
2. 自研应用容器默认非 root、只读根文件系统（临时/状态写入只走声明的 tmpfs 或 volume）、删除
   不需要的 Linux capabilities，并按服务最小化网络、卷和 secrets；第三方镜像逐项评审，业务
   容器不得访问 Docker Socket。
3. Compose 是本地、CI 和单机部署入口，不承担多机调度或高可用承诺。v3.0 多实例继续复用
   同一 OCI 镜像和配置契约，但生产编排平台在阶段 7 另行 ADR 选型。
4. Docker 隔离不等于 Code Interpreter 沙箱。任意代码执行仍需要独立执行边界、禁网、只读
   输入、临时输出、资源配额和逃逸测试。
5. secrets 按服务显式授予，不能烘焙进镜像、前端静态文件或普通环境样例；日志不得打印凭据。
6. 任何拆服务都必须有可测收益。不能为了“微服务化”把每个 tool 拆成一个容器，也不能把
   RAG、Chromium、统计和代码执行依赖重新塞回一个超大镜像。

## 3. 阶段总览

| 阶段 | MCP 演进重点 | Docker/部署演进重点 | 阶段结束形态 |
|---|---|---|---|
| v2.4 阶段 0–2 | 项目内标准 Server、Gateway、stdio/Streamable HTTP、能力契约 | API/Web/工具镜像、单机 Compose、RAG profile | 单 Agent 在单机规范调用内部 MCP |
| v2.5 阶段 3 | 记忆引用与 Evidence 贯通，记忆仍由 Host 管理 | 持久卷、迁移、备份和重启恢复 | 容器重建不丢任务与记忆 |
| v2.5 阶段 4 | 工具来源、权限、审批和健康状态进入 Agent 前端 | Web 代理、SSE、下载和控制操作的 Compose E2E | 用户可看见并干预 MCP 执行 |
| v2.5 阶段 5 | 版本化语义/知识 Resource 与 Evidence | `knowledge-tools`、RAG profile、索引/模型生命周期 | 业务口径先约束工具计算 |
| v2.5 阶段 6 | 能力目录扩展、内部工具变更通知、受控并行 | stats/GPU/浏览器资源 profile 和容量门禁 | 自主探索可控且可扩容 |
| 安全项目 A/B | `sql-tools` 与代码执行 façade，经独立准入 | 专用低权限镜像/网络；代码沙箱与业务容器分离 | 高风险能力可单独启停和审计 |
| v3.0 阶段 7 | 外部 MCP 目录、OAuth/企业身份、连接器和无人值守身份 | Worker/调度/通知、外部状态、镜像供应链和滚动发布 | 可接企业数据并主动运行 |
| v3.0 阶段 8 | 多 Agent 共享 Gateway、租户级工具目录和委派 | 无状态副本、HA、网络策略、租户配额和隔离 | 企业级多 Agent 数据平面 |

## 4. v2.5 阶段 3：记忆系统

### MCP 设计

- 工作记忆、对话摘要、实体映射和项目记忆属于 Host 状态，不注册成可被模型任意调用的通用
  `memory.write` 工具；写入由 Memory Policy 根据来源、作用域、置信度和用户授权决定。
- MCP Observation 进入记忆前只保存 `server_id/tool/version/invocation_id`、结果 hash、
  Dataset/Artifact/Resource reference 和允许的摘要，不复制大结果或凭据。
- 完整血缘由 Host 从 Dataset、Invocation、Artifact、Evidence 和 Claim 真相表只读派生；
  MCP Server 不获得图写权限，也不通过 Resource 枚举完整项目血缘。
- `RequestContext` 增加 `memory_snapshot_id` 和 `evidence_ledger_version`。Server 只看到执行所需
  的引用，不能查询全部项目记忆。
- 用户可查看的项目知识未来可由 `knowledge-tools` 提供只读 Resource；个人偏好、审批记录和
  对话摘要不通过 MCP Resource 暴露。

### Docker 设计

- v2.5 单机部署继续保持一个任务状态写入者。SQLite、TaskEvent、Memory、Artifact、
  Dataset 和最小血缘锚点必须有一致的备份顺序和恢复演练，禁止把 SQLite 放到多个
  API 容器共享写入。
- compaction/coref 可作为 Agent Worker 内部模块，不因“记忆系统”单独拆容器；只有独立负载、
  权限或扩缩容收益经压测证明后才拆分。
- 容器启动先执行兼容性检查/迁移，失败不得用空库启动；恢复后从 Checkpoint 继续且不重复调用工具。

### 验收

- 同一记忆场景在宿主机、stdio 和 Compose/Streamable HTTP 路径下得到相同实体与 Evidence 引用；
- 重建 API/工具容器后，长期记忆、未完成 TaskRun 和 Artifact 仍可定位；
- 项目 A 的记忆、Resource URI 和挂载卷不能被项目 B 的请求读取。
- API 重启和离线恢复后，项目血缘图 hash、节点数、关系数及完整性保持一致。

## 5. v2.5 阶段 4：人机协作与 Agent 前端

### MCP 设计

- 前端只连接 ChatBI API/SSE，不直接连接任何 MCP Server，也不持有 MCP 服务凭据。
- 计划/步骤卡展示经过整理的 `server title`、tool、版本、风险等级、所需权限、状态、耗时、
  Evidence/Artifact 和简短行动理由；不展示原始内部推理或 Server 私有 `_meta`。
- 修改计划、跳过、重试、调参、暂停和取消先写入 TaskEvent，再由 Host/Gateway 取消或重新发起
  MCP 调用。浏览器断线不能被解释为任务取消。
- 高风险工具确认生成不可伪造的 ApprovalRecord，绑定 subject、run、plan version、step、
  tool schema hash、参数摘要和有效期；前端按钮本身不是授权依据。
- 内部 Server 发出 `tools/list_changed` 时，Gateway 重新校验目录；未通过 schema/权限检查的
  变化不进入 Planner，也不能只靠前端隐藏。

### Docker 设计

- Web 反向代理统一转发 API、SSE 和 Artifact/PDF 下载路径，关闭 SSE 缓冲并保留断线重连游标；
  MCP endpoint 不经公共 Web 路由暴露。
- Web 运行时配置只包含公开 API 基址和版本，不包含模型或 MCP secrets。
- Compose 浏览器 E2E 覆盖计划变更、审批、取消、页面刷新、代理重启和报告下载，避免再次出现
  “后端已生成但前端没有卡片/下载入口”的跨容器回归。

### 验收

- 用户看到的工具来源、权限和结果与审计记录一致；未经批准的高风险调用在 Server 前被拒绝；
- Web/API 重启或 SSE 重连后，计划版本、步骤和 Artifact 卡不丢失、不重复；
- 从浏览器网络侧无法直接访问内部 MCP endpoint。

## 6. v2.5 阶段 5：知识与数据联合推理

### MCP 设计

> 当前实现：5B-1 已交付 opaque 领域定义 Resource、签名 Host 上下文、项目/主体/对话过滤及
> SDK `list/read`；5B-2 已贯通定义版本、编译参数、数据 Evidence、Claim 和旧报告复核。
> 5B-3 已交付项目目录、签名分页游标、订阅/退订与版本化通知；5B-4 已交付双传输等价、
> Gateway 重新订阅及 Compose 重启门禁；5B-5 已交付原文/索引/模型缓存分离、Local/Milvus
> 切代回滚和 CPU/GPU profile。真实 Compose 配置/重连门禁已全绿；runtime semantic 等价与
> 领域代表性签字仍待目标机器和领域负责人确认。

- `knowledge-tools` 同时提供受控查询 Tool 和选择性只读 Resource。领域定义（业务指标只是
  其中一种）使用稳定 opaque URI，携带定义版本、生效时间、粒度、公式 hash、负责人和来源，
  不暴露宿主文件路径。
- 指标公式先编译成受控 capability/Tool 调用，再生成数据 Evidence；知识片段本身不能作为数值
  计算结果。Claim 同时引用语义版本与数据工具调用。
- Resource list/read、订阅和变更通知按项目/主体过滤。口径冲突或版本过期进入 Host 澄清流程，
  Server 不自行选择定义。
- 知识库返回内容继续按外部数据处理，不能通过 Resource 或 Tool description 注入新的系统指令。

### Docker 设计

- `knowledge-tools` 独立于 API，按 `rag` profile 使用模型缓存和只读知识索引/Milvus 检索；
  API 管理受权摄入、原文和索引发布，其他分析服务通过受控 Tool/Resource 读取结果。
- 模型权重、索引和原始业务文档分别管理。权重可重建，索引可由原文重建，原文和口径版本属于
  需要备份的事实来源。
- CPU/GPU profile 使用相同工具契约和评测集；切换设备不能改变 Resource URI、Evidence 格式
  或拒答语义。

### 验收

- 同一指标通过 stdio/Streamable HTTP、CPU/GPU profile 返回相同版本与公式 hash；
- 口径更新后旧报告仍能定位旧版本，新任务使用当前有效版本，冲突时等待用户确认；
- RAG 容器重建、索引切代和回滚不破坏 MCP Resource/Claim 引用。

## 7. v2.5 阶段 6：自主分析能力

> 当前实现：6A-1 已增加 SQLite v9 TaskRun capability/tool 目录快照，6A-2 已完成
> `tools/list_changed` 的运行时接收/新任务目录换代和 profile 可用性；6A-3 已增加 SQLite v10
> 受控并行控制面和 ready-frontier 并行执行。提交 `b67b704` 的完整异步链与 Compose 门禁
> 已通过 CI 并关闭 6A。6B-1 数据角色与质量建议、6B-2 结构化确认与统计/聚合门禁及
> 6B-3 匿名代表性评测与 6B-4 双传输/Compose 恢复门禁已由完整 CI 关闭。6C-1～6C-4 的
> 有界候选、角色/capability 筛选、Plan/Invocation/Evidence/Verifier 状态绑定、确定性跟进、
> 匿名评测和 Compose 恢复门禁已由提交 `d5005ee` 的完整 CI 关闭。6D-1～6D-4 的统一
> Evidence、高级统计、独立 `stats.forecast` Tool/Profile、匿名质量门禁、React 局限展示
> 和预测双传输/Compose 恢复探针已由完整 CI 关闭。当前 6E-1 已实现只读 Join 预检、
> 双数据集 Host/MCP/TaskRun 授权和显式数据集/关联键澄清；6E-2 已本地实现参数哈希绑定
> 的高风险授权、固定 Join 执行、SQLite v11 双父血缘和派生策略继承；6E-3 已实现
> 精确预检/版本 Evidence 门禁和 React 协作恢复；6E-4 已实现 17 场景脱敏评测、项目/敏感键/
> 授权发布契约、stdio/HTTP 等价与 `data-tools` 重启恢复，待远程完整 CI。

### MCP 设计

- 新增数据角色识别、质量、分群、贡献、预测、多数据集关联等能力时，先扩展 Tool Capability
  Contract，再由一个或多个 Server 实现；Planner 不硬编码容器名或工具名。
- `tools/list_changed` 只用于已准入的项目内 Server 版本变化。Gateway 对 schema、capability、
  风险和后置条件重新校验后生成不可变目录快照，TaskRun 固定使用该快照直到结束。
- 独立步骤可并行调用，但共享调用预算、数据版本和取消树。所有分支结果先进入 Evidence Ledger，
  Verifier 决定是否继续，不允许 Server 自主递归调用其他 Server。
- 多数据集只传受控 Dataset reference、join policy 和版本；Server 必须校验项目归属、列权限、
  小群体和结果上限。

### Docker 设计

- 通过 `stats`、`forecast`、`browser`、`gpu` 等 profile 组合重依赖；未启用 profile 时 Gateway
  明确把对应 capability 标为 unavailable，不静默改成模型计算。
- 为 stats、chart/Chromium 和 knowledge 设置独立 CPU、内存、并发、超时和队列上限；容量不足
  返回可重试/不可重试的稳定错误，让 Replanner 决定等待、降级或停止。
- 阶段 6 仍以单机 Compose 为正式交付边界。可以复制无状态工具容器做容量实验，但不能宣称
  已具备跨主机调度、HA 或多租户隔离。

### 验收

- 同一任务在不同可用 capability/profile 下产生可解释的计划差异，能力缺失时不伪装成功；
- 并行分支不会跨数据版本、突破预算或重复创建 Artifact；
- 重型工具达到资源上限时只影响对应服务，API、任务状态和取消路径仍可用。

## 8. 独立安全项目

### 8.1 受限 SQL

MCP 形态为独立 `sql-tools` Server，只暴露参数化、只读、已通过策略的查询能力。Tool annotation
可以声明只读/开放世界等提示，但服务端仍必须执行 AST、权限、成本、扫描量、小群体和结果边界。
数据库凭据只存在于 Server secret store，Host 和模型都看不到，也不能把用户/MCP token 原样透传
到数据库。

容器使用专用非 root 镜像、只读根文件系统、无 Dataset/Artifact 全盘挂载，只能访问 allowlist
数据库地址和审计端点。不同数据源/租户是否共用实例由威胁模型决定；高敏源默认隔离凭据和网络。

验收必须覆盖 SQL 注入、方言绕过、超时取消、超大扫描、敏感 Join、凭据泄露、跨项目访问、
网络重试和审计一致性。安全评审通过前不加入生产 allowlist/profile。

### 8.2 受限 Code Interpreter

MCP Server 只作为受控 façade：接收 Artifact/Dataset reference 和受限任务描述，提交给独立
sandbox runner，再返回结构化结果和输出 Artifact。模型不能指定镜像、宿主路径、网络、挂载、
特权参数或 Docker API 请求。

沙箱任务使用一次性执行环境、只读输入、临时可写层、禁网、固定依赖、非 root、capabilities
清零、进程/CPU/内存/磁盘/输出/墙钟限制，结束后销毁。业务 API 和 MCP Server 不挂 Docker
Socket；若底层使用容器运行时，创建权限属于独立受审计的 runner 控制面。

验收除功能用例外，还必须覆盖逃逸、fork bomb、压缩炸弹、软链接/路径穿越、设备文件、模型权重
与 secret 探测、输出外带、取消和残留清理。普通 Docker 容器启动成功不算沙箱验收。

## 9. v3.0 阶段 7：数据接入与主动任务

### MCP 设计

- 建立受信 Server Catalog，记录 owner、canonical URI、协议/SDK、工具与 Resource schema hash、
  数据分类、风险、授权方式、健康、版本和审批状态。发现只产生候选，管理员准入后才进入 Gateway。
- 第三方/跨网络 Streamable HTTP 使用 TLS 和标准授权。交互式用户授权遵循 OAuth 2.1、PKCE、
  Resource Indicators 和 audience 校验；无人值守任务使用企业批准的机器身份/扩展，权限不超过
  触发器预授权范围。禁止 token passthrough。
- 外部 `tools/list_changed` 触发重新隔离和审核，不能自动把新增/变更工具交给模型。失去授权、
  schema 漂移或来源不可信时，相关 capability 立即不可用并记录审计。
- 数据库、数仓、对象存储、REST API 和 BI 语义层优先实现为域隔离的 connector MCP Server。
  Connector 自己持有上游凭据并执行行列权限、限流、分页、结果边界和来源记录。
- 外部 Server 请求用户凭据时不得让用户在聊天中粘贴 secret；只有协议版本、客户端和企业策略
  都支持时，才可采用受控的浏览器外授权流程。

### Docker/生产交付设计

- 新增 scheduler、task worker、connector、notification 等独立镜像；交互式 API 与后台任务
  共享 TaskContract/Gateway/Evidence 契约，但使用不同队列、并发、预算和服务身份。
- 多实例前把 SQLite/本地文件职责迁移到支持并发事务的任务存储、对象存储和队列；具体产品、
  数据迁移和回滚在阶段 7 ADR 决定。不得把 SQLite 文件放在网络盘供多个副本写入。
- Compose 继续作为开发和单机验收入口；生产运行使用同一 OCI 镜像、health/readiness、配置和
  secret 契约，编排平台另行选型，不在路线图中提前锁定 Kubernetes/Nomad/ECS。
- 发布流水线生成 SBOM 和 provenance，执行漏洞/许可证/恶意包门禁，镜像按 digest 部署并签名；
  生产只允许来自受信 registry、通过策略验证的镜像。
- 滚动升级必须同时验证数据库迁移、MCP 协议/工具 schema 兼容、TaskRun 恢复和旧 Worker
  消费新旧任务的边界；不兼容版本使用 drain/停接而不是混跑。

### 验收

- 外部 MCP 从发现、评审、授权、启用、schema 变更到撤销全程可审计；错误 audience、过期 token、
  token passthrough 和未准入工具均被拒绝；
- 定时任务在无人值守下只使用预授权身份、数据范围和预算，权限不足时停止而不是扩大权限；
- 已签名镜像可从空环境部署，升级/回滚不丢 TaskRun、Evidence、Dataset 和 Artifact，故障 Worker
  可被重试且幂等。

## 10. v3.0 阶段 8：多 Agent 与企业治理

### MCP 设计

- Supervisor 和专业 Agent 共享同一受治理的 MCP Client Gateway/Server Catalog。子 Agent 只获得
  当前委派步骤所需的 capability、数据引用、预算和短期凭据，不能继承 Supervisor 全部权限。
- Agent 间传递的是 TaskStep、Observation、Claim/Evidence reference 和取消信号，不把另一个
  Agent 默认伪装成 MCP Tool。若未来需要 Agent-to-Agent 协议，必须独立 ADR，不复用 Tool 的
  幂等、权限或完成语义。
- 每个调用绑定 tenant/project/subject/agent/run/step/invocation 和 permission snapshot；MCP
  session ID 不是身份。租户级 allowlist、配额、风险审批和审计由 Gateway 强制执行。
- 统一 Verifier 对所有子 Agent 的 Evidence 做最终校验；任何一个子 Agent 都不能直接把任务标记
  为成功或绕过主 TaskContract。

### Docker/多实例设计

- API、Agent Worker 和 Gateway 尽量无状态化并支持多副本；任务状态、锁、队列、对象和审计外置。
  Gateway 的 MCP 连接/session 需要明确副本所有权、粘性路由或重建语义；MCP Server 是否多副本
  取决于幂等性、会话粘性和底层依赖。
- 生产编排需要滚动升级、Pod/任务中断处理、反亲和/故障域、网络策略、secret manager、集中日志、
  trace、指标、配额和自动扩缩；具体平台以阶段 7 选型为基础。
- 高风险 runner、连接器和普通分析工具分离节点池/网络域；GPU、Chromium 和代码沙箱不能与 API
  默认共享权限或宿主目录。
- 多租户不得依赖“容器名不同”实现隔离。身份、数据库行列权限、对象前缀、向量分区、加密密钥、
  缓存键和审计都必须带租户边界，并进行跨租户对抗测试。

### 验收

- 子 Agent 越权、跨租户引用、重复执行、部分失败、网络分区和取消传播都有确定性结果；
- 任一 API/Worker/Gateway 副本重启不会丢任务或造成 Artifact 重复，租户配额能抑制 noisy neighbor；
- 版本、镜像 digest、模型、Prompt、工具 schema、Server、权限快照和数据版本足以复现最终结论。

## 11. 兼容、发布与测试矩阵

### 11.1 版本兼容

- 每个发布固定 MCP 协议和 SDK 版本；升级先跑 conformance/契约测试，再更新兼容矩阵和 lock；
- Tool 增加可选字段可原版本演进；删除/改义、收紧必填或改变后置条件必须升工具版本；
- TaskRun 固定 tool catalog snapshot、schema hash 和镜像 revision，运行中不得无审计地漂移；
- Client Gateway 至少支持当前发布所需的协议版本；旧版本淘汰需要告警、迁移窗口和回滚方案。

### 11.2 环境矩阵

| 环境 | MCP | Docker/状态 | 必测内容 |
|---|---|---|---|
| 本机开发 | stdio，必要时进程内测试适配 | 可不启容器；使用隔离测试数据 | schema、单工具、Planner/Verifier |
| CI | stdio + Streamable HTTP | 临时镜像/Compose/volume | 契约等价、健康、权限、失败和重启 |
| 单机部署 | 内部 Streamable HTTP | Compose + 持久卷 + profiles | 浏览器主链路、备份恢复、端口和 secrets |
| 企业部署 | 受信远程 Streamable HTTP | OCI 镜像 + 外置状态 + 生产编排 | OAuth、准入、滚动升级、HA、租户隔离 |

每阶段必须同时维护宿主机与容器测试；容器测试不能替代行为评测，stdio 测试也不能替代远程认证、
网络故障和反向代理测试。

## 12. 后续必须单独拍板的 ADR

- 阶段 5：哪些知识对象可成为 MCP Resource，以及 URI/订阅/版本策略；
- 安全项目 A：首批 SQL 数据源、方言、凭据和隔离粒度；
- 安全项目 B：沙箱运行时、内核隔离级别和允许依赖；
- 阶段 7：企业 IdP、机器身份、Server Catalog、生产编排平台、任务存储/队列/对象存储；
- 阶段 8：租户隔离模型、Gateway 高可用、Agent 间协议和跨区域灾备。

这些 ADR 未完成前可以做接口探针和威胁建模，但不能用默认值直接上线。

## 13. 规范依据

- [MCP 2025-11-25 Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [MCP 2025-11-25 Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- [Docker Compose profiles](https://docs.docker.com/compose/how-tos/profiles/)
- [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [Docker Engine security](https://docs.docker.com/engine/security/)
- [Docker build attestations](https://docs.docker.com/build/metadata/attestations/attestation-storage/)
