# ChatBI Agent 自主化开发规划

> 状态：当前开发依据 · 制定日期：2026-07-21 · 更新日期：2026-07-23
> 基线：v2.3 对话式工具调用 Agent 与知识库第一至第四阶段已经完成
> 当前进度：v2.4 阶段 1 本地实现已完成；知识 source/空结果 Claim、报告文件对账和 Artifact 后置条件影子门禁已收尾，MCP adapter/Gateway 影子路径及 API/Web 基础镜像已进入代码；正式验收仍待提交推送后的远端镜像 CI，阶段 0 的 Planner/行为基线、双传输探针和评审债务仍未关闭

## 1. 总目标

将 ChatBI 从“反应式工具调用”升级为“目标驱动的受约束自主分析 Agent”：Agent 能理解用户目标、在必要时澄清、制定并动态修订计划、通过标准 MCP 协议执行确定性工具、验证结果是否真正达成目标，并让全过程可追溯、可审计、可恢复、可干预。项目同时以 Docker 提供可复现的开发、测试和部署环境。

```text
理解目标 → 必要澄清 → 制定计划 → 执行工具
    ↑                              ↓
持久记忆 ← 最终交付 ← 验证完成 ← 观察结果
                  ↖ 不满足则重新规划
```

“更像 Agent”不等于让模型绕过工具或安全检查。规划、取舍与重规划可以由模型参与；计算、权限、执行、证据和完成条件必须由可验证的代码约束。

## 2. 当前基线与主要缺口

v2.3 已具备模型自主选择工具、工具结果回填、多轮循环、Artifact、SQLite 对话工作区和 SSE 透明度卡片，因此不是伪 Agent；但它仍属于反应式工具调用 Agent：

- “计划”是当前一批 `tool_calls` 的展示，不是带依赖、预期证据和完成条件的任务计划；
- 阶段 1 已把循环结束收口到确定性 Verifier，并接入当前 run 的数值 Claim/`value_refs`、
  知识 source、空检索诚实回答、显式局限和同值多路径候选；受约束语义协议和评测入口已落地，
  但 DeepSeek V3/R1 首轮均出现 false PASS，因此生产语义模型保持禁用；
- TaskRun、TaskContract、事件、快照、Invocation/Evidence 和 Checkpoint 表已落地；用户消息、
  TaskRun/Contract/goal 已原子创建，工具成功的 Artifact/Evidence/Event/Checkpoint 也已原子
  提交；工具开始、失败与 unknown Observation 已原子持久化，unknown 会阻断完成；真实计划
  版本、Checkpoint 恢复和可恢复任务尚未完成；
- 工具注册表已补 capability、版本、风险、权限、幂等性和 Artifact 后置条件元数据；成本、
  前置条件与运行时健康评分仍未完成；
- 生产 Executor 当前仍调用进程内 `Tool.invoke`；标准 MCP Tool Contract、官方 SDK
  `tools/list`/`tools/call` adapter、Client Gateway 与影子比对已实现，但 stdio 子进程、
  Streamable HTTP、服务认证和规范执行切换尚未完成；
- API/Web 基础镜像和构建 smoke CI 已写入但尚未在本批代码上首次远端运行；MCP 服务镜像与统一 Compose
  尚未交付；
- 基础中央策略、结构化审计和模型/工具 trace 已落地；上下文压缩、指代消解、真实身份/租户
  权限、审批与企业审计后端仍未落地；
- 前端能展示执行过程，但不能澄清、审批、暂停、恢复或修改真实计划。

## 3. 不变量与责任归属

原有七条安全红线升级为任何执行路径都必须成立的不变量：

| 不变量 | 强制责任方 | 检查时机 |
|---|---|---|
| 数据与推理分离；`/chat` 维持已拍板的局域网助手例外，列级 `EXCLUDE` 仍生效 | Context Builder + Data Policy | 上下文装配、每次模型调用前 |
| 数值和统计结论必须来自工具，不允许模型心算或编造 | Tool + Claim/Evidence Validator | 工具执行后、最终交付前 |
| 工具入参必须通过同源 JSON Schema | Tool.invoke + Executor | 每次工具执行前 |
| 文件、检索片段、网页和工具结果是数据而不是指令 | Prompt Boundary + Tool Policy | 上下文装配、规划和执行前 |
| 代码执行必须进入禁网、限文件、限资源、强超时的沙箱 | Sandbox Boundary | 每次代码执行前后 |
| 知识问答必须带来源，检索无结果时拒绝编造 | Retriever + Evidence Validator | 检索后、最终交付前 |
| 权限必须先于取数和工具执行，敏感操作必须审计 | Central Policy Gateway | 计划解析后、每次工具执行前 |

控制面另增加五条不变量：

1. 没有通过 TaskContract 的完成验证，不得标记任务成功。
2. 每个任务必须有预算、超时、取消路径和明确终态。
3. 每个最终 Claim 必须关联 Evidence；摘要和长期记忆不能替代原始证据。
4. 长期记忆写入必须带来源、作用域、置信度、版本和删除能力。
5. 外部写操作、通知和无人值守行为必须有明确授权与审批策略。

## 4. 统一控制面模型

### 4.1 TaskContract

Goal Interpreter 将用户输入编译为结构化任务契约：

```text
TaskContract {
  goal
  success_criteria[]
  required_artifacts[]
  required_evidence[]
  constraints[]
  allowed_assumptions[]
  clarification_questions[]
}
```

### 4.2 计划与执行状态

```text
AgentState {
  run_id, conversation_id, goal, task_contract
  plan_version, current_step, observations, evidence_ids
  remaining_budget, status, checkpoint_version
}

TaskPlan {
  steps[]: {
    purpose, capability, dependencies, expected_evidence,
    completion_conditions, fallback, status
  }
}
```

任务状态至少包括：`planning`、`running`、`waiting_user`、`verifying`、`paused`、`completed`、`blocked`、`failed`、`cancelled`。

Verifier 返回 `PASS`、`NEEDS_ACTION`、`WAITING_USER`、`BLOCKED` 或 `FAILED`，不得只返回布尔值。

### 4.3 Claim 与 Evidence

```text
Claim {
  text, evidence_ids[], value_refs[], confidence, limitations[]
}

Evidence {
  source_tool, tool_version, dataset_ref, dataset_version,
  artifact_id, field_refs[], content_hash, created_at
}
```

Finalizer 从结构化 Claim 渲染最终答复。模型需要的新计算必须再次调用工具；不能用格式转换、百分比换算或摘要绕过“数值必来自工具”。

### 4.4 Tool Capability Contract

工具定义在现有名称、描述、schema 和 runner 之外增加：

- capabilities、preconditions、input/output；
- artifact_types、postconditions、side_effects；
- risk_level、required_permissions、estimated_cost；
- idempotent、fallback_tools、version。

Planner 规划“能力”，Executor 通过该契约选择具体工具；任何工具都必须经过中央策略网关。

### 4.5 MCP 协议化与 Docker 容器化

两项能力是横向工程交付轨，随 v2.4 各阶段逐步落地，不推迟到 v3.0 才开始。

**全项目 MCP 协议接口：**

- ChatBI 编排器是 MCP Host，统一 MCP Client Gateway 管理每个受信 MCP Server 的独立连接、生命周期、能力协商、调用和取消；
- `mcp_servers` 中现有确定性工具必须全部通过标准 `tools/list`、`tools/call` 暴露，工具 schema 继续作为唯一入参真相源；Dataset、Artifact 和知识条目是否暴露为 Resources 由阶段 0 的数据边界评审决定，内部系统 Prompt 不默认对外暴露；
- 本地开发支持 `stdio`，容器和远程服务支持 Streamable HTTP；不把已被替代的独立 HTTP+SSE 作为新实现目标；
- 固定并记录 MCP 协议版本和 SDK 版本，实现初始化、版本/能力协商、超时、取消、结构化结果、错误映射和优雅关闭；
- 所有传输共用同一 Tool Capability Contract、中央策略网关、幂等、Evidence、审计和 trace，不允许远程路径绕开进程内已有约束；
- 工具服务保持零 LLM，不使用 MCP sampling 绕过编排层；迁移期可保留进程内适配器，但阶段 2 后 MCP Gateway 是 Agent 的规范执行路径；
- 第三方/跨网络 MCP 的动态发现、OAuth/企业身份和管理员准入仍属于 v3.0 阶段 7，不能因接口标准化而提前信任外部服务。

**Docker 容器化：**

- 分别提供 Web 构建/静态服务镜像和 Python 运行镜像；API、MCP Server、后台 Worker 可复用同一受版本锁定的 Python 镜像，通过不同启动命令分工；
- 提供开发与单机生产 Compose，按 profile 组合 API、Web、MCP 工具服务、Milvus 及后续队列，不要求开发者一次启动所有可选重型组件；
- SQLite、上传文件、Dataset、Artifact、报告、知识库索引和模型缓存必须明确映射持久卷；配置只读挂载，密钥仅由环境或 secrets 注入；
- 镜像采用多阶段构建、非 root 用户、最小运行依赖、健康检查、优雅退出和资源限制；API/工具默认不暴露非必要端口，Streamable HTTP 必须校验 Origin 并启用认证策略；
- 容器不得成为 Code Interpreter 的弱沙箱替代品，API 也不得获得 Docker Socket；代码执行仍按独立安全项目验收；
- CI 必须构建镜像、执行容器内单元/协议/健康检查和最小 Compose 冒烟测试，并验证宿主机与容器运行结果使用同一锁定依赖。

v2.4 之后各阶段的记忆/前端/语义/自主分析、独立安全项目、外部 MCP、镜像供应链和多实例
设计见 [`docs/MCP与Docker全阶段演进设计.md`](./MCP与Docker全阶段演进设计.md)。后续阶段
只能扩展 v2.4 的统一 Gateway、契约和镜像体系，不能另起旁路。

## 5. v2.4 — Agent 控制面

版本目标：建立目标驱动的控制循环。任务是否结束由成功标准决定，而不是模型是否停止调用工具。本版本不新增分析工具。

阶段 0 的详细设计与当前完成状态见 [`docs/v2.4/README.md`](./v2.4/README.md)。设计草案不代表对应功能已经实现。

### 阶段 0：前置验证与协议设计

1. 用真实场景验证 Planner：简单、模糊、多步骤、失败恢复、追问和冲突六类场景；同一场景重复运行，并保留隐藏测试集。
2. 采用混合 Planner，而不是全局二选一：简单任务走确定性快速路径；已知任务族走模板；开放多步骤任务走 LLM；三者输出统一 TaskPlan。
3. 验证语义 Verifier 的适用边界；Artifact、权限、预算、schema、数值来源等全部由确定性检查负责。
4. 完成 TaskContract、AgentState、TaskRun、TaskPlan、TaskStep、TaskEvent、Checkpoint、Claim 和 Evidence 的模型设计。
5. 设计追加事件 + 当前快照的持久化方案、schema v1→v2 迁移和回滚方案。
6. 设计 SSE：`goal`、`clarification`、`plan.created`、`plan.updated`、`step.started`、`step.completed`、`verification`、`waiting_user`，保留现有事件兼容期。
7. 完成不变量责任矩阵和 15–20 个起步场景；基线需多次运行，记录任务成功率、无效调用、无依据 Claim、延迟和成本。
8. 完成 MCP ADR 与协议符合性矩阵：明确 Host/Client/Server 边界、协议/SDK 固定版本、stdio/Streamable HTTP、Tool/Resource 映射、鉴权、错误、取消和兼容迁移方案。
9. 完成容器部署 ADR：明确镜像边界、Compose 服务拓扑、开发/生产 profile、端口、健康检查、持久卷、密钥、CPU/GPU 与离线模型方案。

验收：Planner/Verifier 能力边界有书面结论；评测可重复运行并区分主模型与 fallback；Agent/SSE/MCP 协议、数据模型、迁移、容器拓扑和红线责任矩阵评审通过；至少选取一个现有工具完成 MCP SDK 探针并验证双传输可行性。

### 阶段 1：验证驱动的结束条件

1. 在现有循环中落地最小 AgentState、TaskContract、Claim/Evidence 和预算状态。
2. 增加确定性 Verifier 与受约束语义覆盖判断。
3. 结束条件改为 Verifier 通过；预算耗尽时进入 `blocked` 或 `failed`，明确未完成内容。
4. 图表/报告正则与新后置条件先双轨影子运行；固定回归集达到等价后再降级为兼容兜底，不能直接删除。
5. SQLite 增加任务事件、快照、计划版本、Evidence 与 Checkpoint；引入正式迁移器，不再只接受单一 schema 版本。
6. 所有工具调用写幂等键，状态更新带版本，保证重试不会重复创建 Artifact。
7. 落地基础 trace、审计事件和中央策略网关骨架。
8. 实现共用 MCP Server 适配层和 MCP Client Gateway；所有现有工具可经 `tools/list`、`tools/call` 调用，先与 `Tool.invoke` 影子比对输入、输出、异常和 Artifact 后置条件。
9. 增加后端与前端多阶段 Dockerfile、`.dockerignore` 和镜像构建 CI；容器使用非 root 用户并通过 API/Web 健康检查，锁文件变更能正确使依赖层失效。

验收：固定回归集中虚假 Artifact 完成和无依据数值 Claim 为零；预算耗尽不会伪装成功；旧 SSE 和已有端点无回归；数据库升级和回滚均可验证；全部已注册工具通过 MCP schema/调用/错误契约测试；API 与 Web 镜像可独立构建和启动。

### 阶段 2：结构化计划与动态重规划

1. 将编排层拆为 Goal Interpreter、Planner、Executor、Verifier、Replanner、Finalizer。
2. 落地 Tool Capability Contract 和能力到工具的解析。
3. 工具失败可修正参数、更换方法、采用降级方案或请求用户补充；所有重规划记录原因和计划版本。
4. 指标、时间列、维度、多数据集或合理分析方向存在阻塞性歧义时进入 `waiting_user`；非阻塞歧义记录假设后继续。
5. 简单请求走快速路径，不强制生成复杂计划。
6. 增加 `run_id`、步骤 Checkpoint、取消令牌和 pause/resume/cancel API；明确断线、重复提交、新消息打断和等待澄清的语义。
7. 前端先兼容展示真实计划和重规划事件；完整交互留到 v2.5 阶段 4。
8. Executor 切换到 MCP Client Gateway 规范路径，支持 stdio 和 Streamable HTTP 的超时、断线重连、取消、健康状态和受控降级；进程内适配器只保留为迁移兼容或测试实现。
9. 提供单机完整 Compose：Web、API、MCP 工具服务和可选 Milvus profile；验证持久卷、服务依赖、readiness、重启恢复、日志关联 ID 和一条浏览器 E2E 主链路。

验收：“检查质量→按检测结果决定是否排除异常→比较地区趋势→生成 PDF”能按实际观察修订计划；中断恢复不重复执行已完成步骤；所有终态原因可追溯；同一任务经 stdio 与 Streamable HTTP 产生等价 Evidence/Artifact；从空环境执行一次 Compose 命令即可启动完整单机应用并通过健康检查与浏览器主流程。

## 6. v2.5 — 记忆、自主性与协作

### 阶段 3：记忆系统

- 工作记忆：当前目标、计划、步骤和观察；
- 对话记忆：最近原文、滚动摘要、实体映射，落地 `compaction.py` 与 `coref.py`；
- 项目记忆：指标口径、字段别名、偏好、常用筛选和已确认决策；
- 记忆写入需有来源、作用域、置信度、有效期和冲突处理，用户可查看、修改、删除；
- 摘要只用于上下文导航，不得成为数值证据；
- 完成 Dataset→Analysis→Artifact→Claim 的完整来源图。
- MCP Observation 只把 server/tool/version、结果 hash 和受控引用写入记忆；记忆仍由 Host/Memory Policy 管理，不开放通用 `memory.write` 工具，也不让 Server 读取完整项目记忆。
- 单机容器继续保持任务状态单写入者；SQLite、TaskEvent、Memory、Dataset 和 Artifact 持久卷纳入一致备份、迁移与恢复，禁止多个 API 副本共享写 SQLite。

验收：长对话压缩后仍能正确处理“第二张图”“上次确认的口径”；错误记忆可纠正和删除；项目之间不串记忆；同一场景经 stdio/Streamable HTTP 保持相同 Evidence 引用；容器重建或服务重启后，持久记忆和未完成任务可从挂载卷恢复且不重复调用工具。

### 阶段 4：人机协作与 Agent 前端

- 展示真实目标、计划版本、步骤目的、依赖、状态、证据和简短行动理由；不展示原始内部推理；
- 澄清问题、修改计划、跳过步骤、单步重试与调参；
- 暂停、继续、取消和恢复；
- 展示假设、不确定性、局限和计划变更原因；
- 支持分析分支对比和用户反馈闭环；
- 自主等级：辅助模式、标准只读模式、自主模式；等级必须映射到后端风险和审批策略；
- 快捷指令改为目标建议，不再注入固定工作流模板。
- 前端展示经整理的 MCP Server/tool/版本、风险、权限、状态与 Evidence，但浏览器只连接 ChatBI API/SSE，不直接访问 MCP endpoint 或持有服务凭据。
- 高风险确认生成绑定 subject/run/plan/step/schema hash/参数摘要/有效期的 ApprovalRecord；按钮状态不能替代 Gateway 和 Server 的授权检查。
- Compose 浏览器 E2E 覆盖 SSE 重连、修改计划、审批、取消、容器重启和 Artifact/PDF 下载；Web 反向代理不得公开 `/mcp`。

验收：用户能看懂并干预任务，界面展示与审计记录一致；未经批准的调用在执行前被拒绝；刷新或 Web/API 重启后计划和 Artifact 卡正确恢复；浏览器网络侧无法直连内部 MCP 服务。

### 阶段 5：知识与数据联合推理

业务语义必须先于大规模自主探索：

- 建立指标定义、公式、粒度、时间口径、负责人、版本和生效日期；
- 字段与知识库概念映射；
- 口径冲突、过期和缺失检测；
- 知识证据和数据证据统一进入 Evidence Ledger；
- 指标公式必须能编译为受控工具执行，不能只停留在 RAG 文本。
- `knowledge-tools` 提供受控查询 Tool 和选择性只读 Resource；Resource 使用 opaque URI，绑定定义版本、生效期、公式 hash 和来源，不暴露宿主路径或完整文档库。
- Resource list/read/订阅按项目与主体过滤；口径冲突由 Host 进入澄清，Server 不自行选择。CPU/GPU 与 stdio/Streamable HTTP 使用同一语义版本和工具契约。
- Docker 的 `rag` profile 分离业务原文、索引和模型缓存；原文/口径是事实来源并备份，索引与模型缓存必须可重建。

验收：“按公司定义计算复购率”先解析可执行口径再计算；冲突时进入澄清，不由模型自行选择；旧报告仍能定位旧口径版本；索引切代、容器重建和 CPU/GPU 切换不破坏 Resource、Claim 和拒答语义。

### 阶段 6：自主分析能力

- 自动识别时间、指标、维度和 ID 角色；
- 数据质量诊断与清洗建议；
- 候选假设生成、筛选和结果驱动的后续分析；
- 维度贡献、分群比较、趋势、异常、相关、回归和预测组合；
- 多数据集关联、Join、版本和衍生数据集管理；
- 比较替代解释，输出证据、置信度和局限。
- 新能力先扩展 Tool Capability Contract，再由一个或多个已准入 MCP Server 实现；`tools/list_changed` 必须经 Gateway 重新校验并冻结到 TaskRun 的目录快照，不能让运行中计划无审计漂移。
- 独立分析分支可并行调用 MCP，但共享预算、数据版本、取消树和 Evidence；Server 不得递归调用其他 Server 或自行扩张分析目标。
- Docker 以 `stats`、`forecast`、`browser`、`gpu` 等 profile 隔离重依赖和资源限制；未启用的 capability 明确 unavailable，不得降级为模型心算。阶段 6 仍以单机 Compose 为正式交付边界。

统计护栏同时落地：最小样本量、缺失处理、多重检验校正、训练/验证隔离、时间泄漏检测、预测误差、异常阈值说明，以及“相关不等于因果”；异常根因只能称为候选解释因素。

验收：不同业务问题产生实质不同的分析路径；开放式探索不会无限扩张假设；结论具备证据和统计局限；不同 profile 下能力缺失有可解释计划差异；并行分支不跨数据版本、不突破预算、不重复创建 Artifact；单个重型工具耗尽资源不拖垮 API 和任务控制。

## 7. 独立安全项目

以下能力不随普通功能阶段直接上线，必须独立完成威胁建模、安全设计、对抗测试和审批。

### 项目 A：受限 SQL

- 默认只读事务、独立低权限凭证；
- AST 解析和规范化，禁止多语句、DDL、DML 和危险函数；
- 表/列/行权限、Join 权限、扫描量、结果行数、耗时和并发限制；
- `EXPLAIN` 成本检查、小群体保护、聚合保护、敏感 Join 防护；
- 只把经策略处理的结果交给模型，保留查询、参数和结果摘要审计。
- 作为独立 `sql-tools` MCP Server 接入，数据库凭据只存在服务端 secret store，Host/模型不可见且不得透传用户或 MCP token；未通过安全评审前不进入生产 Server allowlist 或 Compose profile。
- 使用专用非 root、只读根文件系统镜像和数据库 allowlist 网络，不挂载完整 Dataset/Artifact 目录；工具 annotations 只是提示，所有只读和权限约束仍由确定性代码强制。

验收：SQL 注入/方言绕过、超大扫描、敏感 Join、跨项目、错误 audience、超时取消、网络重试和审计一致性测试全部通过，失败时不返回越界数据。

### 项目 B：受限 Code Interpreter

- 禁网络、只读输入、临时可写目录、进程/文件/输出配额；
- CPU、内存和墙钟时间限制，强制取消；
- 固定依赖白名单和不可变运行镜像；
- 沙箱逃逸、恶意文件、压缩炸弹和输出敏感信息测试；
- 代码、环境版本、输入引用和输出 Artifact 全审计；
- 仅作为结构化工具无法覆盖时的补充。
- MCP Server 只作为 façade，把受控 Artifact/Dataset 引用提交给独立 sandbox runner；模型不能选择镜像、宿主路径、网络、挂载、特权参数或 Docker API。
- 一次性执行环境禁网、非 root、只读输入、临时写层、capabilities 清零并限制进程/CPU/内存/磁盘/输出；API/MCP Server 不挂 Docker Socket，普通业务容器不作为沙箱。

验收：除正常计算外，逃逸、fork bomb、路径穿越、软链接、设备文件、secret 探测、输出外带、强制取消和残留清理全部通过独立安全评审。

## 8. v3.0 — 企业级自主 Agent

### 阶段 7：数据接入与主动任务

- PostgreSQL、MySQL、数仓、对象存储、内部 REST API、BI 语义层和目录同步；
- 后台任务队列、定时分析、数据更新触发、异常监控、日报周报、通知订阅和失败重试；
- 无人值守任务必须预先配置身份、数据范围、预算、歧义处理、通知范围和审批策略；不能以“用户不在场”为由猜测；
- 在 v2.4 已完成项目内 MCP 规范执行路径的基础上，建立受信 Server Catalog；第三方和跨网络 MCP 的发现只产生候选，必须经过管理员准入、来源/版本/schema、数据分类、权限和风险检查后才能进入 Gateway；
- 远程 MCP 使用 TLS 和标准授权：交互式流程采用 OAuth 2.1、PKCE、Resource Indicators 与 audience 校验；无人值守使用企业批准的机器身份，禁止 token passthrough。外部工具变化重新隔离评审，不自动交给模型；
- 数据库、数仓、对象存储、内部 API 和 BI 语义层实现为域隔离 connector MCP Server，上游凭据留在 Server，并执行行列权限、分页、限流、结果边界和来源记录；
- 拆分 scheduler、task worker、connector、notification 等镜像；多实例前把 SQLite/本地文件职责迁移到并发任务存储、对象存储和队列，禁止多副本共享写 SQLite；
- 容器镜像进入可发布供应链：按 digest 部署，生成 SBOM/provenance，执行漏洞/许可证门禁、签名和受信 registry 策略；Compose 保留为开发/单机入口，生产编排平台通过阶段 7 ADR 选型；
- 滚动升级同时验证数据库迁移、MCP 协议/工具 schema、TaskRun 恢复和 Worker 版本兼容；不兼容版本先 drain 再替换。

验收：外部 MCP 从发现、授权、启用、变更到撤销可审计；错误 audience、过期 token、token passthrough 和未准入工具均被拒绝；无人值守任务不扩大预授权范围；签名镜像可部署并回滚，故障 Worker 重试不重复副作用或 Artifact。

### 阶段 8：多 Agent 与企业治理

前置条件：单 Agent 状态机、Evidence 和行为评测稳定。

- 可按 Supervisor、Data、Statistics、Knowledge、Visualization、Report、Reviewer 拆分；只并行真正独立的子任务；
- 所有子 Agent 共享受控 TaskContract、Evidence Ledger 和中央策略网关；最终由统一 Verifier 负责完成判定；
- 完成用户、角色、租户、项目、行列级权限、敏感字段、工具风险、写操作审批、完整审计、版本记录、结果复现、配额和并发控制；
- Supervisor 和子 Agent 共享同一受治理 MCP Gateway/Catalog；子 Agent 只获得委派步骤所需 capability、数据引用、预算和短期凭据，不能继承 Supervisor 全权限或直接连接未准入 Server；
- Agent 间传递 TaskStep、Observation 和 Evidence reference，不默认把 Agent 伪装成 MCP Tool；若引入 Agent-to-Agent 协议必须独立 ADR；
- API、Agent Worker 和 Gateway 无状态化并支持多副本，任务、锁、队列、对象和审计外置；高风险 runner/connector 与普通分析服务分离网络域和节点池；
- 多租户隔离必须落实到身份、数据库行列、对象前缀、向量分区、缓存键、加密密钥、MCP allowlist 和配额，不能只靠容器名；
- 多 Agent 不以角色数量为目标，只有质量、隔离或并行收益经评测证明后才启用。

验收：子 Agent 越权、跨租户引用、重复执行、部分失败、网络分区和取消传播均有确定性结果；任一 API/Worker/Gateway 副本重启不丢任务或重复 Artifact；租户配额能隔离 noisy neighbor；镜像、模型、Prompt、工具 schema、权限和数据版本足以复现结论。

## 9. 工程能力与落点

“贯穿全程”必须落实到具体阶段：

| 能力 | 最晚落地阶段 |
|---|---|
| Prompt/模型/工具版本化、基础 trace、评测框架 | v2.4 阶段 0 |
| MCP 架构/协议版本/符合性矩阵、容器拓扑 | v2.4 阶段 0 |
| schema 迁移、任务事件、幂等、审计、策略网关 | v2.4 阶段 1 |
| 全量 MCP Server 接口、Client Gateway、API/Web 基础镜像 | v2.4 阶段 1 |
| Checkpoint、取消、恢复、失败回放 | v2.4 阶段 2 |
| MCP 规范执行路径、双传输、完整单机 Compose | v2.4 阶段 2 |
| 记忆引用/Evidence 贯通、状态卷一致备份与重启恢复 | v2.5 阶段 3 |
| MCP 来源/审批/健康前端、代理与下载 Compose E2E | v2.5 阶段 4 |
| 版本化知识 Resource、`knowledge-tools` 与 RAG 容器生命周期 | v2.5 阶段 5 |
| capability 目录快照、受控并行、重型工具资源 profile | v2.5 阶段 6 |
| `sql-tools` / Code Interpreter façade 与隔离运行环境 | 对应独立安全项目 |
| 后台队列、主动触发、外部 MCP 准入/OAuth、外置状态、镜像供应链 | v3.0 阶段 7 |
| 共享 Gateway 委派、多实例协调、企业级配额和租户隔离 | v3.0 阶段 8 |

模型降级不得静默丢失 function calling、结构化输出或任务契约能力。trace 和评测数据不得记录密钥、整表或未经策略允许的敏感内容。

## 10. 评测与阶段门禁

阶段 0 先测基线，再冻结数值门槛；在基线未知前不填写任意百分比。至少跟踪：

- 任务成功率、计划 schema 合法率、计划步骤可执行率；
- 虚假完成数、无依据数值 Claim 数；
- 工具失败恢复率、不必要调用数、重规划成功率；
- 澄清准确率与过度澄清率；
- 指代解析准确率、中断恢复成功率；
- 模型调用次数、Token、延迟和成本；
- 主模型/fallback、单次/重复运行、公开/隐藏场景差异。
- stdio/Streamable HTTP 契约等价率、MCP schema/版本漂移和未授权调用拦截率；
- 镜像构建/启动时间、健康恢复、持久卷恢复、资源峰值、容器重启后的任务/Artifact 重复数；
- v3.0 增加外部授权成功/撤销、镜像策略门禁、滚动升级恢复和跨租户隔离指标。

任何阶段必须满足：已有回归测试不下降；新增能力有单元、集成、Agent 场景和必要的浏览器 E2E；失败路径与成功路径同等验收。

## 11. 优先级与执行纪律

### P0：决定产品是否真正成为 Agent

混合 Planner/Verifier 验证、不变量责任矩阵、行为基线、TaskContract、AgentState、任务事件、Claim/Evidence、成功标准、结构化计划、动态重规划、澄清和恢复；全项目 MCP 规范接口与 Client Gateway；可复现基础镜像和单机 Compose。

### P1：决定产品是否好用

完整记忆、可干预前端、业务语义层、自主探索、多数据集分析和统计护栏；同时补齐对应的 MCP 记忆/知识引用、工具来源展示以及单机容器资源与恢复能力。

### P2：决定产品是否能进入企业自主运行

受限 SQL、受限 Code Interpreter、内部数据接入、后台任务、监控通知、外部 MCP 授权与准入、镜像供应链、多 Agent、审批、多租户和分布式部署。

执行纪律：严格按阶段顺序推进；每阶段单独评审、测试、浏览器验证和提交。独立安全项目不得因功能阶段需要而绕过评审。

## 12. 范围变更

本规划自 2026-07-21 起废止以下旧范围限制，但不废止任何安全红线：

- “复杂多步分析暂不实现”→ 纳入 v2.4 阶段 2；
- “完整上下文压缩与指代消解不做”→ 纳入 v2.5 阶段 3；
- “MCP-over-HTTP 明确不做”→ 项目内标准 MCP 接口、stdio/Streamable HTTP 与规范执行路径提前纳入 v2.4；第三方/跨网络动态发现、企业授权和准入仍归 v3.0 阶段 7；
- “不要求全项目容器化”→ 改为 v2.4 完成基础镜像和单机 Compose，v2.5 补齐状态恢复、代理 E2E、RAG 生命周期和重型工具 profile，v3.0 再补发布供应链、外置状态、多实例与生产运维；
- “内部数据、多租户明确不做”→ 分别纳入 v3.0 阶段 7/8；
- “自由 SQL 永久不做”→ 改为独立受限 SQL 安全项目，未通过评审前仍不得接入生产 Agent；
- Code Interpreter 由占位能力改为独立安全项目，未完成沙箱验收前仍不得注册到 Agent 工具集。

第 14 章记录的 v2.3 五阶段迁移仍是有效历史和当前代码基线；本文件与设计文档第 15 章是后续功能开发的现行路线图。
