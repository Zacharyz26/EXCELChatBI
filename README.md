# ChatBI 智能体

中文优先的对话式数据分析 Agent：通过自然语言完成知识问答、Excel 数据分析、可视化和报告生成。

> 开发约束见 [`CLAUDE.md`](./CLAUDE.md)，完整架构见
> [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)，当前开发路线见
> [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。

## 当前进度

当前可运行基线是带 **v2.4 阶段 2E 生产工具服务结构** 的对话式 Agent；最新
Compose/容器 CI 已全绿。自然语言对话是唯一前端入口；
模型可循环调用画像、统计、图表、数据变换、知识检索和报告工具，过程与 Artifact 通过
SSE 展示。SQLite schema v10 已包含 TaskRun/Contract/Event/Snapshot、Invocation、
Evidence、Claim、Checkpoint、项目成员、报告所有权，以及 v2.5 受控记忆记录、
幂等操作、不可变快照、资源关联、持久化对话压缩、ApprovalRecord、版本化领域定义与字段映射。
阶段 3A 的离线一致备份/恢复、
Memory readiness 和 Compose 联合恢复门禁已经通过完整 CI；阶段 3B-1–3B-3
与阶段 3C-1–3C-3 已由提交 `e3d51fd` 的真实 Docker/Compose CI 验证并关闭。
阶段 3D 的项目记忆治理与阶段 3E 的完整血缘/恢复已由提交 `0b5980c` 的
backend、frontend 和 Docker/Compose CI 验证并关闭，v2.5 阶段 3 已全部完成。
阶段 4C 的恢复、SSE 重连与工具审计已由提交 `2f2771f` 的 GitHub Actions
[run 30782321622](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/30782321622)
验证关闭；4D 的竞态修复提交 `7f81fe2` 已由
[run 30980088817](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/30980088817)
确认 backend、frontend 与 Compose 三任务全绿，阶段 4 正式关闭。阶段 5A、5B-1～5B-5
已完成；提交 `d5a672d` 的 GitHub Actions
[run 31063896157](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31063896157)
已确认 backend、frontend 与真实 Compose Resource 重连/CPU-GPU 配置门禁全绿，阶段 5
工程关闭。真实 CPU/GPU 模型语义等价和领域代表性签字继续作为发布债务，不由配置门禁代替。
当前进入阶段 6A：SQLite v9 为每个 TaskRun 固定不可变 capability/tool 目录快照，
6A-2 已接入受治理目录换代和 profile 可用性；SQLite v10 进一步固定执行作用域、数据版本、
取消树与 Evidence Ledger，并对同一 ready frontier 的受治理只读工具开启有界并行。

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
- SQLite v3→v4 带 hash 备份迁移，以及受来源、作用域、置信度、版本、冲突和软删除
  约束的 Memory Repository/Policy；
- TaskRun 创建/恢复时固定不可变 `memory_snapshot_id`；Agent 只读取有界记忆摘要，
  MCP 上下文只携带快照 ID 和 Evidence ledger 版本，记忆不能替代 Evidence；
- `memory-reference-v1` 把用户确认的实体映射、字段别名和确认决策绑定到唯一项目内
  Dataset/Artifact；冲突、过期、低置信度、删除和恢复漂移均失败关闭，澄清不自动写长期记忆；
- Memory 创建、冲突、修订、删除、关联、快照和拒绝均输出不含正文的结构化审计；
  SQLite/Dataset/Artifact 可生成带 schema、行数和文件 hash 的离线一致备份；
- 项目记忆治理 API 与 React 面板支持按主体安全查看、不可变纠正和软删除；
  `expected_version` 防止并发覆盖，`Idempotency-Key` 保证重试，新版本继承受控资源关联，
  tenant/subject/来源内部字段不会暴露给浏览器，固定历史快照不被改写；
- SQLite v5→v6 增加不可变 Dataset 来源锚点和删除 tombstone；项目血缘 API 与 React
  面板从现有真相表派生 Dataset → Analysis/Invocation → Artifact → Evidence → Claim，
  conversation/tenant/project 隔离、稳定图 hash、漂移检测和安全响应字段均失败关闭；
- 工作区备份 manifest 与 Compose 联合恢复探针固定 v6 checksum、锚点/Claim/Plan 行数、
  非正文 lineage hash 及项目图 hash/计数；恢复漂移不允许服务就绪；
- SQLite v6→v7 增加高风险 ApprovalRecord 与幂等授权操作；用户可在 `paused` 安全边界
  创建不可变计划新版本，审批固定绑定 subject/run/plan/step/schema/参数摘要和有效期，
  且批准只能被完全匹配的执行一次性消费；
- 阶段 4A API 已提供计划修订、当前主体授权列表和批准/拒绝；所有写操作使用
  `If-Match` 与 `Idempotency-Key`，成功后任务保持暂停并写入事件、快照和 Checkpoint；
- 阶段 4B React 任务协作面板已接入真实 TaskRun：展示状态/版本/计划/步骤/风险摘要，
  支持结构化澄清、暂停/显式恢复/取消、计划不可变修订、单步重试及 ApprovalRecord
  批准/拒绝；批准本身不会恢复任务，浏览器按钮不作为授权依据；
- `high/critical` 工具在 ToolInvocation 和 MCP 调用前原子暂停；显式恢复后只消费与当前
  subject、计划、步骤、契约和参数完全匹配的批准，已消费绑定由 MCP Client Gateway 与
  MCP Server 双重校验；当前未启用新的高风险生产工具；
- SQLite v4→v5 增加不可变 ConversationCompaction 版本、精确来源条目和策略参数；
  Agent 使用有界、脱敏的确定性历史摘要与最近原文，TaskRun 恢复固定原 `compaction_id`；
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
- data/stats/chart/report/knowledge 五个独立 MCP 服务、逐服务认证/发现/健康、私网、
  Compose secrets、最小卷权限和容器浏览器/重启门禁；
- Bearer 认证、租户/项目成员隔离，以及浏览器会话级令牌录入；
- 模型/工具/整轮超时，断连终态，启动时运行态恢复与未知调用保护；
- 上传、parquet、报告和临时截图的受限生命周期清理；
- 不使用网络 mock 的 Web→API→Excel→SQLite/parquet Playwright 全栈 E2E；
- v2 生命周期 SSE 双发以及 `GET /agent/runs/{run_id}` 和事件游标读取接口；
- fast/template/LLM 混合 Planner、持久化 TaskPlan/TaskStep、计划 capability 工具白名单、
  步骤状态绑定和未完成计划的确定性拒绝；
- 按持久化依赖图只开放当前 ready capability；可恢复失败生成不可变计划新版本，
  已完成步骤和 Evidence 不被改写，零异常可显式跳过条件清洗；
- `/chat/stream` 后台 producer 与浏览器订阅解耦；断线不取消任务，澄清回答、
  pause/resume/cancel 和单步 retry 均使用项目写权限、`If-Match` 与幂等键；
- pause/等待澄清会原子写入 Checkpoint；API 宿主丢失后可在同一 `run_id` 上恢复
  Contract、活动计划、预算和已完成步骤，结果未知的活动工具调用禁止自动重试；
- 浏览器按服务端最近 TaskRun 恢复，SSE 使用 `Last-Event-ID` 游标续接并按事件 ID/sequence
  去重；工具服务、版本、权限、执行时健康及 Evidence/Artifact 在协作面板保持同源展示；
- React 对话工作区、SSE 理解/执行/图表/表格/报告/引用卡；
- bge-m3 稠密+稀疏检索、reranker、Milvus Lite/Standalone、知识文档生命周期和 CI 质量门禁；
- 固定版本 Milvus Standalone 部署、readiness、代际状态、回滚、清理、备份恢复和负载测试工具。

### 当前缺口

- 依赖图调度、Observation 自动重规划、结构化澄清回答、单步重试、SSE 游标重连和
  服务端最近 Run 恢复已实现；Web/API/工具服务重启浏览器门禁已通过真实 Compose CI；
- 当前 TaskContract 解释器只覆盖非空答复与高置信图表/报告后置条件；语义覆盖首轮模型评测
  未通过，生产保持禁用；
- 更广泛的非数值 Claim 和同值路径语义消歧尚未完成；
- 记忆控制面、TaskRun 快照、确定性上下文压缩和 3C 指代质量/恢复门禁已完成；
  3D 用户治理与 3E 五阶段血缘/恢复已通过真实本机 full-stack E2E 和
  Docker/Compose CI；
  长期记忆自动提取尚未设计，当前继续禁止通用模型 `memory.write`；
- 静态 Bearer 鉴权已落地；OIDC/OAuth、成员管理 API、企业审计和完整审批策略尚未实现；
- Agent Executor 已切到 MCP Client Gateway；支持 stdio/认证 Streamable HTTP、
  Host RequestContext、超时/取消、健康代次和只读幂等有限重连，部署环境禁止进程内降级；
- 单机生产结构 Compose 与 Milvus CPU/GPU RAG profile 已提供；多实例外置状态、对象存储和
  阶段 6 重型分析工具 profile 尚未完成；
- 前端已提供结构化澄清、计划编辑、审批、暂停/续跑、工具来源/权限/健康审计视图、
  SSE 重连、三档自主等级、分析分支对比和追加式反馈闭环；辅助确认、反馈后 LLM 分支和
  标准只读副作用拒绝的 Compose 场景已通过真实 Docker runner 验证；
- SQLite v8 领域定义、字段映射、受控公式编译和 `domain_definition_lookup` 已接入 Evidence；
  `knowledge-tools` 已提供按签名 project/subject/conversation 过滤的领域定义 Resource
  `list/read`，共享服务 token 不作为用户身份；
- SQLite v9 为 TaskRun 原子保存内容寻址的 capability/tool 目录快照；新增工具只对新任务可见，
  已冻结工具缺失、版本/契约或服务路由漂移时恢复失败关闭，Planner/Replanner 与模型工具集不读
  运行中的新目录；
- SQLite v10 为 TaskRun 原子固定共享预算、数据集版本绑定和取消树；独立只读幂等分支
  可有界并行，结果由 Host 按统一 Evidence Ledger 原子提交，不跨版本、不分裂预算；
- 知识库仍是实例级共享资源，尚未做租户级索引隔离；
- 前端主包仍较大，需对 ECharts 与报告卡片做动态拆包。

### 已规划但未实现

- **v2.4 收口**：阶段 2 的 20×3 真实行为对照已完成并通过自动门禁（任务成功率
  70.0%、终态如实率 73.3%、越界 0），Compose/容器 CI 已全绿；现有评测全部使用商业
  数据语境，人工盲评暂缓，需补代表性场景和 Verifier 评分契约后再完成 G7 签字；
- **v2.5**：阶段 3A–3E、4A–4D 和阶段 5 工程门禁已完成并通过真实 Compose CI；阶段 6A-1～6A-3
  已完成本地实现，并已加入双 MCP 服务受控并行及重启/离线恢复门禁，等待完整 CI 确认。
  真实 CPU/GPU semantic 等价、领域代表性签字、
  数据角色/质量能力和多数据集自主分析仍未完成；
- **独立安全项目**：以隔离 MCP Server/运行环境交付受限 SQL、受限 Code Interpreter，普通 Docker 容器不替代代码沙箱；
- **v3.0**：内部数据连接器、后台主动任务、外部 MCP 准入与企业授权、外置状态和容器发布供应链、多 Agent、多租户和企业治理。

这些能力已进入路线图，但不得在代码和交付说明中提前标记为完成。

## Agent 演进路线

```text
当前 v2.4 阶段 2E（代码与容器 CI 已完成，G7 代表性评审债务保留）
  理解目标 → 必要澄清 → 结构化计划 → 受控执行
      ↑                      （依赖图 ready frontier）↓
  持久状态 ← 最终交付 ← Verifier ← Evidence
          ├── 不可变计划版本 ← Observation 重规划
          └── Checkpoint ← 暂停/断线/重启后同 run 恢复

v2.5 阶段 3A（已完成，完整 CI 与 Docker 恢复门禁全绿）
  记忆契约 + SQLite v4 + Memory Policy + 不可变快照 + 项目隔离 + 一致恢复

v2.5 阶段 3B（已完成，Docker/Compose CI 全绿）
  持久化压缩快照 + 最近原文窗口 + TaskRun 固定引用 + 安全脱敏
  领域中立质量门禁 + 并发幂等 + Compose 固定版本恢复探针

v2.5 阶段 3C-1–3C-3（已完成，Docker/Compose CI 全绿）
  Artifact/Dataset 确定性指代 + 固定快照实体映射 + 歧义失败关闭
  TaskPlan 恢复绑定 + 工具血缘约束 + 23 场景质量门禁 + 双传输/Compose 恢复探针

v2.5 阶段 3D（已完成，Docker/Compose CI 全绿）
  项目记忆安全查询 + 不可变纠正 + 软删除 + 版本/幂等并发控制
  React 治理面板 + subject/tenant 隔离 + 固定快照不变 + 真实本机 full-stack E2E

v2.5 阶段 3E（已完成，Docker/Compose CI 全绿）
  SQLite v6 不可变 Dataset 锚点 + 五阶段只读来源图 + 安全 React 血缘面板
  领域中立质量门禁 + readiness/备份 hash + API 重启/离线恢复图一致性

v2.5 阶段 4（已完成；真实 Compose 门禁全绿）
  SQLite v7 + 计划干预/审批授权边界 + Executor/Gateway/Server 双重校验
  React 协作 → 服务端恢复/SSE 重连/工具审计 → 自主等级/分支/反馈 → Compose 关闭门禁

v2.5 阶段 5（工程关闭；真实 CPU/GPU semantic 与领域签字债务保留）
  SQLite v8 版本化定义 + 受控公式 + Evidence → MCP Resource list/read
  定义/数据 Claim + 旧报告复核 → 目录分页/订阅/通知 → 双传输/重连 → RAG 生命周期 → 代表性签字

v2.5 阶段 6A（开发中；6A-1～6A-3 已实现，待完整 CI）
  SQLite v9 TaskRun capability/tool 快照 → tools/list_changed 换代 → profile 可用性
  SQLite v10 共享预算/固定数据版本/取消树/Evidence Ledger → ready frontier 有界并行

横向交付轨
  MCP：单源契约 → Client Gateway → 五服务独立路由与认证
  Docker：仅 Web 公网入口 → 私网 API/MCP → 分卷/secrets/重启 E2E

v2.5 延伸
  MCP：记忆/Evidence 引用 → 前端审批 → 知识 Resource → 自主分析能力目录
  Docker：状态恢复 → 代理 E2E → RAG CPU/GPU 生命周期 → 重型分析工具资源 profile

v3.0
  数据连接器 + 主动任务 + 外部 MCP 治理/OAuth
  外置状态 + 镜像供应链/多实例 + 多 Agent/租户隔离
```

完整阶段、依赖和验收标准见 [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。
v2.4 详细设计与阶段 1–2E 实施状态见 [`docs/v2.4/README.md`](./docs/v2.4/README.md)。
v2.5 当前入口、阶段 3 交付记录与
[阶段 4A](./docs/v2.5/阶段4A实施记录.md)/
[阶段 4B](./docs/v2.5/阶段4B实施记录.md)实施记录见
[`docs/v2.5/README.md`](./docs/v2.5/README.md)。
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
Governance → MCP Client Gateway
             ├─ stdio（本地）
             └─ 认证 Streamable HTTP（部署）
      ↓
parquet/报告文件 + SQLite v9 + Local/Milvus 知识库
```

Dify 已放弃。Agent 生产执行已使用 MCP 单源契约、官方 SDK Server/Client 和受治理
Client Gateway；进程内适配只用于迁移兼容/测试。根 Compose 已把 11 个 Agent 工具分配到
五个独立服务，v3.0 再扩展外部 MCP 的动态发现、授权与准入治理。

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
| `packages/session` | SQLite 工作区、schema 迁移、Task/Event/Evidence、受控记忆与快照 |
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

# 生产结构 Compose 浏览器、重启与离线破坏/恢复门禁（从仓库根目录执行）
cd ../..
./scripts/run_compose_e2e.sh
```

工作区离线备份/恢复要求先停止 API 和所有写服务。宿主机部署可使用：

```bash
uv run python -m apps.api.workspace_admin backup --service-stopped
uv run python -m apps.api.workspace_admin verify --input .data/workspace_backups/<backup>
uv run python -m apps.api.workspace_admin restore \
  --input .data/workspace_backups/<backup> \
  --service-stopped --yes --replace-files
```

恢复会先在 `WORKSPACE_BACKUP_DIR` 生成 `pre-restore-*` 覆盖前副本。知识库不在这个
工作区备份中，继续使用独立的 `kb_admin` / Milvus 备份流程。

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
