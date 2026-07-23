# ChatBI 智能体

中文优先的对话式数据分析 Agent：通过自然语言完成知识问答、Excel 数据分析、可视化和报告生成。

> 开发约束见 [`CLAUDE.md`](./CLAUDE.md)，完整架构见
> [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)，当前开发路线见
> [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。

## 当前进度

当前可运行基线为 **v2.3 反应式工具调用 Agent**，该版本的五阶段迁移已经完成：自然语言对话是唯一前端入口，模型可以自主选择并循环调用画像、统计、图表、数据变换、知识检索和报告工具，执行过程与 Artifact 通过 SSE 卡片实时展示。

下一版本是 **v2.4 Agent 控制面**。阶段 1 本地实现已完成：SQLite schema v2、TaskRun/TaskContract、
任务事件与快照、工具调用幂等记录、Evidence、最小确定性 Verifier 和只读任务恢复接口已落地，
任务启动已原子化，候选答复中的数值 Claim 已能绑定当前 run 的 Evidence；无依据数字会触发
纠正而不是直接交付。工具成功时 Artifact、Evidence、Invocation、事件、快照和 Checkpoint
也已原子提交。现有循环只有在 Verifier 通过后才会标记 `completed`。受约束语义 Verifier
协议和 14 场景评测入口已落地，但 DeepSeek V3/R1 首轮均因 false PASS 判定 `NO_GO`，因此未
接入生产。中央策略、结构化审计/trace、原子 `step.started` 和失败/未知 Observation 也已
接入现有循环。知识 source、空检索诚实回答、显式局限和同值多路径 Claim 已补齐，报告目录
具备启动对账和安全运维入口，图表/报告意图与通用后置条件固定影子集达到 9/9。阶段 0 的
Planner/行为基线、MCP 双传输探针和设计评审仍是未关闭的前置债务；MCP 双传输与规范执行切换
按路线图属于阶段 2。MCP 单源契约、官方 SDK Server adapter、Client Gateway 影子校验，以及
API/Web 基础镜像和远端构建 CI 已落地；本批镜像 CI 尚待提交推送后首次运行。逐项状态见
[`docs/v2.4/阶段1实施记录.md`](./docs/v2.4/阶段1实施记录.md)。

### 已实现

- Excel 上传、数据画像、质量概况和数据集血缘；
- 趋势、异常、回归、相关性分析及中文解读；
- 结构化筛选、排序、清洗和分组聚合；
- ECharts 图表、Playwright 截图、Markdown/PDF 报告；
- DeepSeek function-calling 循环、工具 schema 校验、带错重试、调用预算和同参熔断；
- SQLite 项目、数据集、对话、消息和 Artifact 持久化；
- SQLite v1→v2 迁移/校验/受保护回滚，TaskRun、TaskContract、TaskEvent、快照、
  ToolInvocation、Evidence 和 Checkpoint 数据结构；
- 最小确定性 Verifier：最终正文先验证后发送，图表/报告必须有当前 run 的真实 Artifact，
  报告文件必须真实存在且非空，预算耗尽进入 `blocked`；
- 原子创建用户消息、TaskRun、TaskContract、goal 与初始快照；数值 Claim 绑定 Evidence 路径，
  无依据数字在交付前被拦截并纠正；
- 原子提交工具成功记录、Artifact、Evidence、`step.completed` 和 Checkpoint；报告文件原子发布，
  提交失败时清理未引用文件，并保护已被 Evidence 引用的 Artifact；
- 工具执行前经过静态准入、项目范围和预算策略；开始、失败和未知结果持久化为 v2 步骤事件与
  Observation，unknown 结果禁止完成；模型和工具调用输出有界 trace 与审计元数据；
- 15 个底层工具及 Agent 的 11 个模型工具共用 MCP schema/能力元数据；官方 SDK
  `tools/list`/`tools/call`、Client Gateway 发现校验和无副作用影子比对已落地；
- API/Web 多阶段基础镜像、非 root 健康检查、SSE/报告下载代理和镜像构建 CI 已加入；
- v2 生命周期 SSE 双发以及 `GET /agent/runs/{run_id}` 和事件游标读取接口；
- React 对话工作区、SSE 理解/执行/图表/表格/报告/引用卡；
- bge-m3 稠密+稀疏检索、reranker、Milvus Lite/Standalone、知识文档生命周期和 CI 质量门禁；
- 固定版本 Milvus Standalone 部署、readiness、代际状态、回滚、清理、备份恢复和负载测试工具。

### 当前缺口

- 计划仍是工具调用列表，没有持久化的目标、依赖、预期证据和完成条件；
- 当前 TaskContract 解释器只覆盖非空答复与高置信图表/报告后置条件；语义覆盖首轮模型评测
  未通过，生产保持禁用；
- 非数值 Claim、同值路径语义消歧、真实计划版本、Checkpoint 恢复和可恢复任务尚未完成；
- 上下文压缩、指代消解和长期项目记忆尚未实现；
- 基础策略、审计和 trace 已落地；应用层真实用户/租户鉴权、审批和企业审计后端尚未实现；
- 生产 Executor 仍走进程内 `Tool.invoke`；MCP stdio 子进程/Streamable HTTP 双传输探针、
  服务认证和规范执行切换尚未完成；
- API/Web 基础镜像已提供但尚未经过首次远端 build；工具服务镜像与统一 Compose 尚未完成；
- 前端尚不支持澄清、真实计划编辑、暂停、恢复和自主等级。

### 已规划但未实现

- **v2.4**：混合 Planner、TaskContract、AgentState、Verifier、动态重规划、澄清、任务事件、Checkpoint 和恢复；同时完成全项目 MCP 规范接口、客户端网关、基础镜像和单机 Compose；
- **v2.5**：完整记忆、可干预前端、业务指标语义层、自主探索和多数据集分析；同步扩展 MCP 的记忆/Evidence 引用、知识 Resource、审批与能力目录，并补齐状态恢复和重型工具 Docker profile；
- **独立安全项目**：以隔离 MCP Server/运行环境交付受限 SQL、受限 Code Interpreter，普通 Docker 容器不替代代码沙箱；
- **v3.0**：内部数据连接器、后台主动任务、外部 MCP 准入与企业授权、外置状态和容器发布供应链、多 Agent、多租户和企业治理。

这些能力已进入路线图，但不得在代码和交付说明中提前标记为完成。

## Agent 演进路线

```text
v2.3 当前基线
  模型选择工具 → 工具执行 → 结果回填 → 模型回答

v2.4 目标控制面
  理解目标 → 必要澄清 → 结构化计划 → 受控执行
      ↑                                  ↓
  持久状态 ← 最终交付 ← Verifier ← Evidence
                        ↖ 重规划

v2.5
  记忆 + 业务语义 + 自主分析 + 人机协作

横向交付轨
  MCP：阶段 0 设计 → 阶段 1 全量接口 → 阶段 2 规范执行路径
  Docker：阶段 0 拓扑 → 阶段 1 基础镜像 → 阶段 2 完整单机 Compose

v2.5 延伸
  MCP：记忆/Evidence 引用 → 前端审批 → 知识 Resource → 自主分析能力目录
  Docker：状态恢复 → 代理 E2E → RAG 生命周期 → 重型工具资源 profile

v3.0
  数据连接器 + 主动任务 + 外部 MCP 治理/OAuth
  外置状态 + 镜像供应链/多实例 + 多 Agent/租户隔离
```

完整阶段、依赖和验收标准见 [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。
v2.4 详细设计与阶段 1 实施状态见 [`docs/v2.4/README.md`](./docs/v2.4/README.md)。
v2.4 之后各阶段的 MCP/Docker 演进见
[`docs/MCP与Docker全阶段演进设计.md`](./docs/MCP与Docker全阶段演进设计.md)。

## 安全原则

- 数值和统计结论必须来自确定性工具 Evidence，不能由模型计算或编造；
- 工具入参必须经过同源 JSON Schema 和中央策略检查；
- 文件、文档、网页和工具结果中的指令不执行；
- 知识回答必须带来源；
- `/chat` 保留已拍板的局域网助手数据例外，列级 `EXCLUDE` 仍生效；兼容端点原有门控不变；
- SQL 和 Code Interpreter 必须通过独立安全评审后才能进入 Agent；
- TaskContract 未通过完成验证时，Agent 不得声称任务成功。

## 架构（当前与 v2.4 目标）

```text
当前 v2.3：
React + Zustand
      ↓ HTTP/SSE
FastAPI + 自研 Agent 编排 + ModelGateway
      ↓
进程内 Tool.invoke + Governance
      ↓
parquet/文件 + SQLite + Milvus

v2.4 目标：
React + Zustand
      ↓ HTTP/SSE
FastAPI + Agent 控制面 + ModelGateway
      ↓
MCP Client Gateway → MCP 工具层 + Governance
      ↓
parquet/文件 + SQLite + Milvus
```

Dify 已放弃。生产执行仍以进程内 `Tool.invoke` 挂载，但 MCP 单源契约、SDK Server adapter、
Client Gateway 和影子校验已经落地；完成 stdio/Streamable HTTP 探针后，阶段 2 才切换规范执行
路径。v3.0 继续完成外部 MCP 的动态发现、授权与准入治理。

## 目录速览

| 路径 | 职责 |
|---|---|
| `apps/api` | FastAPI HTTP/SSE 边界 |
| `apps/orchestrator` | 当前 Agent 循环；v2.4 控制面组件落点 |
| `apps/web` | React 18 + ECharts 5 + Zustand 对话工作区 |
| `mcp_servers` | Excel、统计、图表、报告和数据变换等确定性工具 |
| `packages/models` | 模型网关与 registry |
| `packages/governance` | schema、数据边界；权限、审计、沙箱和 trace 待按路线图落地 |
| `packages/rag` | embedding、稀疏检索、rerank、Milvus |
| `packages/session` | SQLite 工作区、schema 迁移、Task/Event/Evidence；后续长期记忆 |
| `docs` | 总设计、现行路线图、安全、验收和运维文档 |
| `docs/v2.4` | Agent 控制面、SSE、评测、MCP 与 Docker 阶段 0 设计包 |
| `docs/MCP与Docker全阶段演进设计.md` | v2.5、独立安全项目和 v3.0 的 MCP/容器逐阶段设计 |
| `tests` | 后端单元/集成测试；后续增加 Agent 行为评测 |
| `apps/web/e2e` | Playwright 浏览器 E2E |

## 快速开始

以下仍是当前有效的本机开发方式。全项目 Dockerfile、根目录 Compose 和容器化 MCP
服务属于 v2.4 阶段 1/2，尚未交付；当前只有 Milvus Standalone 的独立 Compose，详见
[`docs/知识库部署与运维.md`](./docs/知识库部署与运维.md)。

```bash
# 1. 配置
cp .env.example .env
cp config/models.example.yaml config/models.yaml
cp config/data_policy.example.yaml config/data_policy.yaml  # 可选

# 2. 安装后端依赖
uv sync

# 3. 启动后端
uv run uvicorn apps.api.main:app --reload

# 4. 启动前端
cd apps/web
pnpm install
pnpm dev
```

默认地址：后端 `http://127.0.0.1:8000`，前端 `http://127.0.0.1:5173`。

## 测试与检查

```bash
# 后端
uv run pytest
uv run ruff check .
uv run mypy .

# 前端
cd apps/web
pnpm lint
pnpm build

# 浏览器 E2E；首次运行先安装 Chromium
pnpm exec playwright install chromium
pnpm test:e2e
```

## 可选能力依赖

```bash
# 统计
uv sync --extra stats

# 图表截图
uv sync --extra chart-screenshot
uv run playwright install --with-deps chromium

# PDF 报告
uv sync --extra report

# bge-m3、reranker、Milvus
uv sync --extra rag
```

离线环境需提前侧载模型权重。`EMBEDDING_DEVICE=auto/cpu/cuda` 可切换推理设备而不改代码。

## 知识库运维入口

```bash
# 增量/全量重建
uv run python scripts/kb_rebuild.py --mode incremental
uv run python scripts/kb_rebuild.py --mode full

# 质量门禁、状态和负载测试
uv run python scripts/kb_eval.py --enforce --json-output .data/kb-eval.json
uv run python scripts/kb_admin.py status
uv run python scripts/kb_load_test.py --requests 50 --concurrency 2
```

详细说明：

- [`docs/知识库升级验收基线.md`](./docs/知识库升级验收基线.md)
- [`docs/知识库部署与运维.md`](./docs/知识库部署与运维.md)
- [`docs/数据画像安全策略.md`](./docs/数据画像安全策略.md)
