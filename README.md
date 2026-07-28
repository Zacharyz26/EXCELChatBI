# ChatBI 智能体

中文优先的对话式数据分析 Agent：通过自然语言完成知识问答、Excel 数据分析、可视化和报告生成。

> 开发约束见 [`CLAUDE.md`](./CLAUDE.md)，完整架构见
> [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)，当前开发路线见
> [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。

## 当前进度

当前可运行基线是带 **v2.4 阶段 2B 依赖图执行与动态重规划控制面** 的对话式 Agent。自然语言对话是唯一前端入口；
模型可循环调用画像、统计、图表、数据变换、知识检索和报告工具，过程与 Artifact 通过
SSE 展示。SQLite schema v3 已包含 TaskRun/Contract/Event/Snapshot、Invocation、
Evidence、Claim、Checkpoint、项目成员和报告所有权。

本轮已完成安全与可运行性加固：`dataset_ref` 只能是服务端生成的 32 位不透明标识符；
Bearer token 映射到用户/租户/角色，项目、对话、数据集、任务和报告均做成员隔离；模型、
工具和整轮任务有独立超时，断连及启动恢复不会遗留运行态；歧义需求会进入
`waiting_user`，无依据数字和知识来源会在交付前重验或确定性修复；上传源文件、临时截图、
孤儿数据和项目报告均有清理路径。根 Compose、生产认证登录页和无 Mock 全栈 E2E 也已落地。

### 已实现

- Excel 上传、数据画像、质量概况和数据集血缘；
- 趋势、异常、回归、相关性分析及中文解读；
- 结构化筛选、排序、清洗和分组聚合；
- ECharts 图表、Playwright 截图、Markdown/PDF 报告；
- DeepSeek function-calling 循环、工具 schema 校验、带错重试、调用预算和同参熔断；
- SQLite 项目、数据集、对话、消息和 Artifact 持久化；
- SQLite v1→v3 迁移/校验/受保护回滚，TaskRun、TaskContract、TaskEvent、快照、
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
- API/Web 多阶段镜像、非 root 健康检查、SSE/鉴权下载代理、根 Compose 和镜像构建 CI；
- Bearer 认证、租户/项目成员隔离，以及浏览器会话级令牌录入；
- 模型/工具/整轮超时，断连终态，启动时运行态恢复与未知调用保护；
- 上传、parquet、报告和临时截图的受限生命周期清理；
- 不使用网络 mock 的 Web→API→Excel→SQLite/parquet Playwright 全栈 E2E；
- v2 生命周期 SSE 双发以及 `GET /agent/runs/{run_id}` 和事件游标读取接口；
- fast/template/LLM 混合 Planner、持久化 TaskPlan/TaskStep、计划 capability 工具白名单、
  步骤状态绑定和未完成计划的确定性拒绝；
- 按持久化依赖图只开放当前 ready capability；可恢复失败生成不可变计划新版本，
  已完成步骤和 Evidence 不被改写，零异常可显式跳过条件清洗；
- React 对话工作区、SSE 理解/执行/图表/表格/报告/引用卡；
- bge-m3 稠密+稀疏检索、reranker、Milvus Lite/Standalone、知识文档生命周期和 CI 质量门禁；
- 固定版本 Milvus Standalone 部署、readiness、代际状态、回滚、清理、备份恢复和负载测试工具。

### 当前缺口

- 依赖图调度和 Observation 自动重规划已实现；计划编辑、结构化澄清回答和用户触发的
  单步重试尚未实现；
- 当前 TaskContract 解释器只覆盖非空答复与高置信图表/报告后置条件；语义覆盖首轮模型评测
  未通过，生产保持禁用；
- 更广泛的非数值 Claim、同值路径语义消歧、Checkpoint 自动续跑尚未完成；
- 上下文压缩、指代消解和长期项目记忆尚未实现；
- 静态 Bearer 鉴权已落地；OIDC/OAuth、成员管理 API、审批和企业审计后端尚未实现；
- 生产 Executor 仍走进程内 `Tool.invoke`；MCP stdio/Streamable HTTP 双传输探针和服务
  认证已完成，规范执行切换尚未完成；
- 单机 API/Web Compose 已提供；多实例外置状态、对象存储和独立工具服务编排尚未完成；
- 前端能展示阻塞澄清文本，但尚无结构化澄清控件、计划编辑、暂停/续跑和自主等级；
- 知识库仍是实例级共享资源，尚未做租户级索引隔离；
- 前端主包仍较大，需对 ECharts 与报告卡片做动态拆包。

### 已规划但未实现

- **v2.4 后续**：结构化澄清、Checkpoint 自动恢复和 pause/resume/cancel 任务控制；
  同时把生产执行切到 MCP Client Gateway，并交付独立工具服务 Compose；
- **v2.5**：完整记忆、可干预前端、业务指标语义层、自主探索和多数据集分析；同步扩展 MCP 的记忆/Evidence 引用、知识 Resource、审批与能力目录，并补齐状态恢复和重型工具 Docker profile；
- **独立安全项目**：以隔离 MCP Server/运行环境交付受限 SQL、受限 Code Interpreter，普通 Docker 容器不替代代码沙箱；
- **v3.0**：内部数据连接器、后台主动任务、外部 MCP 准入与企业授权、外置状态和容器发布供应链、多 Agent、多租户和企业治理。

这些能力已进入路线图，但不得在代码和交付说明中提前标记为完成。

## Agent 演进路线

```text
当前 v2.4 阶段 2B
  理解目标 → 必要澄清 → 结构化计划 → 受控执行
      ↑                      （依赖图 ready frontier）↓
  持久状态 ← 最终交付 ← Verifier ← Evidence
          └── 不可变计划版本 ← Observation 重规划

v2.5
  记忆 + 业务语义 + 自主分析 + 人机协作

横向交付轨
  MCP：单源契约/影子校验已完成 → 规范执行路径待切换
  Docker：API/Web 单机 Compose 与真实 E2E 已完成 → 重型工具 profile 待扩展

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

## 当前架构

```text
React + Zustand
      ↓ HTTP/SSE
FastAPI + Bearer/项目隔离 + Agent 控制面 + ModelGateway
      ↓
Governance → 进程内 Tool.invoke
             ↘ MCP 同源契约/影子校验
      ↓
parquet/报告文件 + SQLite v3 + Local/Milvus 知识库
```

Dify 已放弃。生产执行仍以进程内 `Tool.invoke` 挂载，但 MCP 单源契约、SDK Server adapter、
Client Gateway、影子校验和 stdio/Streamable HTTP 双传输探针已经落地；阶段 2 后续切换
生产规范执行路径。v3.0 继续完成外部 MCP 的动态发现、授权与准入治理。

## 目录速览

| 路径 | 职责 |
|---|---|
| `apps/api` | FastAPI HTTP/SSE 边界 |
| `apps/orchestrator` | 当前 Agent 循环；v2.4 控制面组件落点 |
| `apps/web` | React 18 + ECharts 5 + Zustand 对话工作区 |
| `mcp_servers` | Excel、统计、图表、报告和数据变换等确定性工具 |
| `packages/models` | 模型网关与 registry |
| `packages/governance` | schema、数据边界、项目权限、策略、审计与 trace |
| `packages/rag` | embedding、稀疏检索、rerank、Milvus |
| `packages/session` | SQLite 工作区、schema 迁移、Task/Event/Evidence；后续长期记忆 |
| `docs` | 总设计、现行路线图、安全、验收和运维文档 |
| `docs/v2.4` | Agent 控制面、SSE、评测、MCP 与 Docker 阶段 0 设计包 |
| `docs/MCP与Docker全阶段演进设计.md` | v2.5、独立安全项目和 v3.0 的 MCP/容器逐阶段设计 |
| `tests` | 后端单元/集成、Agent 控制面与安全回归 |
| `apps/web/e2e` | Playwright 浏览器 E2E |

## 快速开始

本机开发：

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

单机容器部署：

```bash
docker compose up --build
```

默认入口为 `http://127.0.0.1:8080`。生产认证、数据卷和真实 E2E 说明见
[`docs/全栈部署与E2E.md`](./docs/全栈部署与E2E.md)。

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
pnpm test:e2e:fullstack
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
