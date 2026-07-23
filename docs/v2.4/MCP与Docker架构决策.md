# MCP 与 Docker 架构决策

> 状态：双传输技术决策已由阶段 0 探针验证；ADR 仍待 G7 正式评审接受 · 日期：2026-07-23
> 决策范围：项目内 MCP 标准化与单机容器交付；外部服务治理仍归 v3.0

本 ADR 只冻结 v2.4 基础。v2.5 阶段 3–6、独立安全项目和 v3.0 阶段 7–8 如何继续
扩展，见 [`../MCP与Docker全阶段演进设计.md`](../MCP与Docker全阶段演进设计.md)。

## 1. 决策摘要

| 项目 | 决策 |
|---|---|
| MCP 协议 | 阶段 0 探针固定 `2025-11-25`；不跟随 draft/latest 漂移 |
| Python SDK | 已复核并精确锁定 `mcp==1.28.0`；不使用 v2 alpha；升级必须重跑协议契约测试 |
| MCP 实现 | 官方 Python SDK low-level Server/Client；现有 Tool schema 单源转换，不复制定义 |
| 本地传输 | stdio |
| 容器传输 | stateful Streamable HTTP；不新增旧 HTTP+SSE transport |
| 工具发现 | v2.4 只从管理员静态 allowlist 连接，`tools/list` 后再做版本、schema、权限校验 |
| Agent 长任务 | 使用自有 TaskRun；不依赖当前仍属实验性的 MCP Tasks |
| 运行上下文 | Host 以不可由模型控制的请求元数据注入 run/project/conversation/权限/幂等信息 |
| 镜像 | 一个 Python 多阶段 Dockerfile、多个运行 target；独立 Web 静态镜像 |
| Compose | 根目录单机 Compose，profile 控制 RAG/Milvus 和重型工具；阶段 2 一条命令启动完整环境 |
| 安全 | 非 root、只读配置、持久卷、最小端口、Origin 校验、服务认证、无 Docker Socket |

官方 MCP 当前定义的标准传输是 stdio 与 Streamable HTTP，后者已替代旧 HTTP+SSE。Python SDK v1.x 是当前稳定线，v2 在 2026-07-22 仍为预发布。因此本项目先固定稳定版本完成协议探针，不用临近发布的预览版本承载控制面。

当前实现边界：Tool Capability Contract、15 个底层工具的 SDK Server adapter、stdio 入口、
认证的 stateful Streamable HTTP 入口、Client Gateway、官方 SDK 会话和生产影子比对已落地；
`aggregate_preview` 双传输探针已通过，API/Web 基础镜像的远端构建与非 root smoke 也已通过。
生产 Executor 仍使用进程内 runner；上下文签名、规范执行切换、完整 Compose 和容器 E2E 属于
阶段 2，ADR 的“接受”状态仍须等待 G7 设计评审。

参考：

- <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- <https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle>
- <https://github.com/modelcontextprotocol/python-sdk>

## 2. MCP 目标架构

```text
Agent State Machine (MCP Host)
        │
        ▼
MCP Client Gateway
  ├─ static server allowlist
  ├─ lifecycle/version negotiation
  ├─ capability + schema validation
  ├─ policy / permission / budget
  ├─ idempotency / timeout / cancel
  └─ result → Observation/Evidence/Artifact
        │
        ├─ stdio（本机开发/协议测试）
        └─ Streamable HTTP（Compose/远程）
                 │
                 ▼
       MCP Servers（零 LLM）
       data / stats / chart / report / knowledge
```

MCP Host 保留完整对话、模型、计划和 Evidence Ledger。Server 只接收完成一次能力调用所需的最小数据引用与受控上下文，不能读取整个对话，也不能连接其他 Server。

## 3. 服务分组

不是每个 tool 一个容器。按依赖、数据访问和扩缩容特征分组：

| Server | 初始工具/能力 | 主要依赖 | 数据权限 |
|---|---|---|---|
| `data-tools` | Excel 解析/画像、结构化变换、聚合 | pandas/openpyxl/DuckDB | Dataset 读；衍生 Dataset 写 |
| `stats-tools` | trend/anomaly/regression/correlation | statsmodels/sklearn/Prophet | Dataset 只读 |
| `chart-tools` | gen_chart/chart_screenshot | ECharts/Playwright | Dataset 读；截图 Artifact 写 |
| `report-tools` | Markdown/PDF 生成 | WeasyPrint | 只读允许的 Artifact；报告写 |
| `knowledge-tools` | kb_search；后续受控 Resources | embedding/reranker/Milvus | 知识索引读 |

`get_data_profile`、`transform_dataset` 和 `generate_report` 当前含编排层闭包逻辑。迁移时拆成两部分：Host 解析对话引用、校验所属项目并生成显式受控输入；Server 只执行确定性计算。Server 不直接接收模型可填写的 `conversation_id/project_id`。

## 4. Tool Capability Contract 到 MCP

现有 `Tool.input_schema` 继续是输入 schema 真相源。阶段 1 为每个工具补齐输出 schema 和元数据，然后生成 MCP Tool：

```text
MCP Tool {
  name
  description
  inputSchema           <- Tool.input_schema
  outputSchema          <- 新增 Tool.output_schema
  annotations           <- readOnly/destructive/idempotent 等标准提示
  _meta.com.chatbi/*    <- capability、版本、风险、权限、Artifact 后置条件
}
```

ChatBI 扩展键使用命名空间，不把内部字段混入模型可控 arguments。至少包含：

- `com.chatbi/capabilities`；
- `com.chatbi/tool-version`；
- `com.chatbi/risk-level`；
- `com.chatbi/required-permissions`；
- `com.chatbi/artifact-types`；
- `com.chatbi/idempotent`；
- `com.chatbi/postconditions-version`。

Client Gateway 在 `tools/list` 后校验工具名、输入/输出 schema hash、版本兼容和风险声明，与静态 allowlist 不一致时把 Server 标记 unhealthy，不把工具交给 Planner。

工具成功优先返回 `structuredContent` 并同时保留简短文本摘要以兼容客户端。大结果、文件和数据集只返回 opaque reference、hash 和元数据，不把整表或文件 base64 放进 MCP 消息。

初始 capability 目录：

| Capability | 当前工具解析目标 |
|---|---|
| `data.profile` | `get_data_profile` |
| `data.quality` | `get_data_profile` 的质量结果；阶段 6 再评估独立工具 |
| `data.aggregate` | `aggregate_preview` |
| `dataset.transform` | `transform_dataset` |
| `stats.trend` | `trend_analysis` |
| `stats.anomaly` | `anomaly_detect` |
| `stats.regression` | `regression` |
| `stats.correlation` | `correlation` |
| `visualization.chart` | `gen_chart` |
| `visualization.screenshot` | `chart_screenshot` |
| `knowledge.search` | `kb_search` |
| `report.generate` | `generate_report` |

同一个工具可以实现多个 capability，但 Planner 不得依赖这种偶然映射。解析器按权限、版本、健康、后置条件和成本选择工具；若没有满足条件的实现，必须返回 capability unavailable，而不是选择名称相似的工具。

## 5. 受控 RequestContext

每次调用需要以下 Host 上下文：

```text
RequestContext {
  subject_id?
  project_id
  conversation_id
  run_id
  plan_version
  step_id
  invocation_id
  idempotency_key
  permission_snapshot_id
  trace_id
  deadline_at
}
```

该结构由 Client Gateway 生成并通过 MCP request `_meta` 的 ChatBI 命名空间传递；它不出现在模型看到的 input schema。Server 必须拒绝缺少上下文、上下文签名无效、项目与 Dataset/Artifact 不一致或 deadline 已过的请求。

stdio 模式依赖父进程建立的单客户端连接，敏感凭据通过最小环境变量传入。Streamable HTTP 模式的 MCP session ID 仅用于协议会话，不能作为身份或权限依据。

## 6. 生命周期、错误与取消

- Client 与每个 Server 建立独立 session，完成 initialize 和能力/协议版本协商后才能发现工具；
- 协商版本不在 allowlist 时 fail closed；
- 工具调用使用 Agent deadline，Client 超时后发取消并把 invocation 标为 cancelled 或 unknown；
- JSON-RPC/传输错误、工具业务错误、schema 错误和策略拒绝映射为不同稳定 error code；
- 网络重试沿用同一个 idempotency key；Server 返回已提交结果时 Client 不重复创建 Artifact；
- Server 日志写 stderr/stdout 规则服从 transport：stdio 的 stdout 只能出现协议消息；
- 健康检查不代替 initialize；Server 健康但协议不兼容时仍不可用。

MCP 2025-11-25 的 Tasks 仍属实验能力。ChatBI 的暂停、恢复、澄清、Checkpoint 和长任务由自有 TaskRun 管理；MCP 一次调用只代表一个 ToolInvocation。未来若使用 MCP Tasks，必须单独 ADR 和兼容迁移，不复用同一 task ID 语义。

## 7. Streamable HTTP 安全

v2.4 单机 Compose：

- MCP endpoint 只在内部 Docker network 监听，不映射宿主机端口；调试 profile 也默认绑定 `127.0.0.1`；
- 校验 `Origin` allowlist，非法 Origin 返回 403；
- API→MCP 使用独立服务凭据，凭据由 Compose secret/部署系统注入，不写镜像、Compose 或日志；
- 服务凭据绑定 audience 和 server，不能用一个 token 横向访问全部服务；
- session ID 视为不可信输入，认证变化时重建 session；
- 限制请求体、连接数、并发、调用时间和结构化输出大小；
- 反向代理不得公开 `/mcp`。外部公开、OAuth 发现和企业 IdP 集成归阶段 7。

## 8. MCP 阶段 0 探针

> 2026-07-23 实施记录：`scripts/mcp_transport_probe.py` 已在官方 SDK `1.28.0`、协议
> `2025-11-25` 下通过。直接调用、stdio 与 stateful Streamable HTTP 的结果哈希一致；合法调用、
> schema/未知工具/业务错误/deadline/取消、Origin/认证/协议/session 拒绝和进程退出均通过。

选择 `aggregate_preview`，因为它有确定性输入、结构化输出且不依赖浏览器或模型。探针必须：

1. 用同一 schema 注册 low-level MCP Tool；
2. 分别通过 stdio 和 stateful Streamable HTTP 完成 initialize、tools/list、tools/call、shutdown；
3. 验证合法调用、schema 错误、未知工具、业务错误、超时和取消；
4. 验证 structuredContent 与直接 `Tool.invoke` 结果等价；
5. 验证 Origin、无认证、错误协议版本和失效 session 被拒绝；
6. 记录 SDK/协议版本、延迟和退出行为；
7. 用官方 Inspector 或 conformance 工具做补充检查，但 CI 仍保留项目自己的契约测试。

探针通过后才把 `pyproject.toml` 的宽范围改为已验证下界并更新 lock；阶段 0 不直接升级到 SDK v2 预发布。

## 9. Docker 镜像设计

### 9.1 构建文件

```text
Dockerfile                 Python 多阶段、多 target
apps/web/Dockerfile        Node 20 + pnpm 9.15.9 构建，非 root 静态服务器运行
.dockerignore
compose.yaml               单机统一入口
compose.dev.yaml           开发覆盖：源码挂载、热更新、调试端口
deploy/.env.example        非敏感配置模板
```

Python 构建建议：

- 基础镜像 Python 3.11 slim，发布时按 digest 固定；
- builder 使用 `uv sync --frozen`，运行镜像不带编译器和包缓存；
- 通过 target 安装 core、stats、chart、report、rag 等不同 extra；
- API 与各 MCP Server 可以共享基础层，但不强迫轻服务携带 torch、Chromium 或 Prophet；
- Playwright 浏览器及 WeasyPrint 系统库放对应 target；
- 使用固定 UID/GID 的非 root 用户，应用目录和配置只读，只有声明的状态目录可写；
- OCI label 记录源码 revision、构建时间、应用版本和依赖锁 hash。

### 9.2 Compose 拓扑

```text
public network
  web :8080
    └─ /api + 兼容下载路径 → api:8000

private app network
  api
  data-tools
  stats-tools
  chart-tools
  report-tools
  knowledge-tools

profile rag
  milvus + etcd + milvus-minio
```

默认只发布 Web 端口。API 仅在开发 profile 可绑定 localhost；MCP 服务永不发布到公网。生产 Web 反向代理必须关闭 SSE 响应缓冲，并覆盖当前 Artifact 下载兼容路径，防止出现“报告已生成但下载 URL 落到前端静态服务器”的回归。

现有 `deploy/milvus/docker-compose.yml` 在阶段 1 合并为根 Compose 的 `rag` profile，同时保留独立运维命令兼容入口；Milvus 镜像版本、鉴权、卷和备份流程不降级。

### 9.3 持久卷

| 数据 | 容器路径建议 | 要求 |
|---|---|---|
| SQLite | `/var/lib/chatbi/db` | 单 API writer；备份、权限和锁检查 |
| uploads | `/var/lib/chatbi/uploads` | 不可执行、大小限制、清理策略 |
| datasets | `/var/lib/chatbi/datasets` | API/data/stats/chart 按最小读写挂载 |
| reports/artifacts | `/var/lib/chatbi/artifacts` | report/chart 写，API/Web 下载只读 |
| KB index/backups | `/var/lib/chatbi/kb` | knowledge 写，其他服务不挂载 |
| model cache | `/var/cache/chatbi/models` | 可重建；与业务数据卷分开 |
| Milvus/etcd/minio | 保留当前独立卷 | 备份恢复继续按知识库运维文档 |

不同服务不得统一挂载整个 `.data` 为读写。模型缓存不是业务真相源，不进入业务恢复承诺。

## 10. 配置与 CPU/GPU profile

- `.env` 只作本地开发，生产密钥使用 secrets；
- 容器内服务地址使用 DNS 名，不使用 `127.0.0.1` 指向其他容器；
- `EMBEDDING_DEVICE=cpu/cuda` 保持配置切换，不改业务代码；
- 默认 CPU profile 必须能运行 hashing/local 或远程模型 API 基线；
- GPU profile 只给需要的 knowledge/vision worker 分配设备，不把 GPU 暴露给 API/Web；
- 离线部署先构建/导入镜像和模型包，启动时禁止隐式联网下载模型；缺模型时 readiness 明确失败。

## 11. 健康、启动与退出

- liveness 只检查进程事件循环；readiness 检查 SQLite/卷、模型 registry、受信 MCP 协议协商和启用的知识后端；
- Compose 使用健康依赖，不用固定 sleep；
- API 未连接 required MCP Server 时 readiness 失败，可选能力按配置明确 degraded；
- SIGTERM 先停止接收新任务，再等待有界时间、持久化 Checkpoint、取消 MCP 调用并关闭 session；
- MCP Server 关闭前停止新 call，等待或取消当前调用；
- Web 静态服务健康检查必须实际读取入口文件；
- 所有日志写 stdout/stderr，带 trace/run/invocation ID，不写本地容器日志文件。

## 12. 容器 CI 与验收

阶段 1：

- 校验 Dockerfile/Compose；
- 构建 API 和 Web 镜像；
- 镜像内运行 Python import、Ruff/单测子集、前端静态文件检查；
- 以非 root 启动并通过健康检查；
- 修改 `uv.lock`/`pnpm-lock.yaml` 能使依赖层失效。

阶段 2：

- 从空 Docker volume 一条 Compose 命令启动 core profile；
- 上传→分析→图表→PDF→下载浏览器 E2E 通过；
- 同一工具经 stdio 与 Streamable HTTP 输出/Evidence 等价；
- 重启 API/MCP/Web 后会话、TaskRun、Dataset 和 Artifact 可恢复；
- MCP 不可用、失效凭据、错误 Origin、只读卷、磁盘满和 SIGTERM 路径有测试；
- 只有 Web 端口对非本机网络可见；
- 不存在 Docker Socket、特权容器或无声明的宿主目录写入。

后续阶段继续复用本节的镜像与契约测试：v2.5 增加状态恢复、前端审批/代理、知识 Resource
和重型工具 profile；安全项目增加 SQL/代码执行隔离；阶段 7/8 再增加外部授权、SBOM、
漏洞门禁、镜像签名、来源证明、镜像仓库、滚动升级、多实例与租户隔离。逐阶段门禁见
[`../MCP与Docker全阶段演进设计.md`](../MCP与Docker全阶段演进设计.md)。

## 13. 被否决方案

| 方案 | 原因 |
|---|---|
| 继续把 `Tool.invoke` 称作 MCP | 没有生命周期、协商、发现和标准传输，无法互操作 |
| v2.4 只实现远程 HTTP、不做 stdio | 本地测试和第三方 MCP 客户端兼容性差 |
| 新建旧 HTTP+SSE transport | 已被 Streamable HTTP 替代，增加无价值兼容债务 |
| 每个 tool 一个容器 | 服务数量、启动时间和依赖重复过高，扩缩容收益不足 |
| 所有能力塞进一个超大镜像 | RAG/浏览器/统计依赖拖累 API，扩大漏洞面和冷启动 |
| 把 MCP Tasks 当 AgentState | 协议能力仍实验，且无法表达 ChatBI 的证据、计划和审批不变量 |
| 让 MCP Server 读取完整 Conversation | 破坏 Host/Server 隔离和最小上下文原则 |
| API 挂 Docker Socket 启动工具 | 等同宿主机高权限，违反沙箱和最小权限 |
| 仅把应用打成一个容器 | 无法独立扩缩容、隔离 Chromium/统计/RAG 依赖，也不利于 MCP 互操作 |
