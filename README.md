# ChatBI 智能体

中文优先的对话式数据分析 Agent：通过自然语言完成知识问答、Excel 数据分析、可视化和报告生成。

> 开发约束见 [`CLAUDE.md`](./CLAUDE.md)，完整架构见
> [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)，当前开发路线见
> [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。文档入口和现行/历史边界见
> [`docs/README.md`](./docs/README.md)。

> 文档状态：2026-08-27。当前版本为 `v2.5-closeout` 单机、单租户技术预览；v2.4 阶段
> 2A–2E、v2.5 阶段 3–6，以及 C1“启动依赖契约”和 C2“未实现工具撤出目录”均已工程完成。
> 2026-08-24 已在 WSL2/CPU 上实测完整 BGE-M3 + BGE reranker + Docker Milvus
> Standalone：API/Web readiness、2 文档 6 片段重建和 semantic 门禁均通过。

## 启动导航

先按用途选择一种方式，不要混用两套 RAG 配置：

| 目标 | RAG | Docker | 推荐入口 |
|---|---|---|---|
| 第一次跑通、日常前端/API 开发 | `hashing + lexical + local` | 不需要 | [方案 A：轻量本地启动](#方案-a轻量本地启动推荐新开发者) |
| 使用完整中文语义模型和独立向量库 | `bge-m3 + bge-reranker-v2-m3 + Milvus` | 只运行 Milvus | [方案 B：完整 BGE + Docker Milvus](#方案-b完整-bge--docker-milvus) |
| 验证生产结构、多 MCP 服务和统一入口 | 由 Compose profile 决定 | 全栈 | [方案 C：根 Compose](#方案-c根-compose-生产结构验收) |

如果你刚接触本项目，先用方案 A；需要真实 RAG 时再切方案 B。方案 B 的逐项安装、模型
侧载、RBAC、WSL2/Docker Desktop 排错见
[`docs/本地完整BGE与Milvus启动指南.md`](./docs/本地完整BGE与Milvus启动指南.md)。

成功启动后的地址：

- Web：`http://127.0.0.1:5173`；
- API 文档：`http://127.0.0.1:8000/docs`；
- API readiness：`http://127.0.0.1:8000/health/ready`；
- 根 Compose 统一入口：`http://127.0.0.1:8080`。

## 当前进度

当前可运行基线是带 **v2.4 阶段 2E 生产工具服务结构** 的对话式 Agent；最新
Compose/容器 CI 已全绿。自然语言对话是唯一前端入口；
模型可循环调用画像、统计、图表、数据变换、知识检索和报告工具，过程与 Artifact 通过
SSE 展示。SQLite schema v11 已包含 TaskRun/Contract/Event/Snapshot、Invocation、
Evidence、Claim、Checkpoint、项目成员、报告所有权，以及 v2.5 受控记忆记录、
幂等操作、不可变快照、资源关联、持久化对话压缩、版本化领域定义与字段映射；历史
ApprovalRecord 持久化结构只为旧数据库兼容保留，不再属于当前产品执行路径。
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
工程关闭。真实 CPU/GPU 模型语义等价和领域代表性签字已从收尾范围取消，未执行且不得
被描述为通过。
阶段 6A 已关闭：SQLite v9 为每个 TaskRun 固定不可变 capability/tool 目录快照，
6A-2 已接入受治理目录换代和 profile 可用性；SQLite v10 进一步固定执行作用域、数据版本、
取消树与 Evidence Ledger，并对同一 ready frontier 的受治理只读工具开启有界并行。提交
`b67b704` 的 [CI run 31348476642](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31348476642)
已确认 backend、frontend 与真实 Compose 三项全绿。阶段 6B 已由提交 `6b89ef6` 的完整 CI
关闭；阶段 6C 的有界候选、验证生命周期、确定性跟进、匿名评测和 Compose 恢复门禁已由
提交 `d5005ee` 的
[run 31659188951](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31659188951)
验证关闭。阶段 6D 已由提交 `3febd68` 的
[run 31678576324](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31678576324)
完整 CI 关闭。6E-1 已实现只读 Join 预检和双数据集三层权限校验，6E-2 已实现
固定等值 Join、SQLite v11 多父血缘与派生策略继承；6E-3 已实现精确预检/数据版本门禁
和完整双父血缘展示；6E-4 已实现 17 场景脱敏质量门禁、跨项目/敏感键发布契约、
stdio/HTTP 等价、`data-tools`
重启恢复和浏览器验收。提交 `92a6f02` 的
[CI run 31767613363](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31767613363)
已确认 backend、frontend 与真实 Compose 三项全绿，v2.5 阶段 6 全部工程关闭。

2026-08-17 的收尾修订将产品统一为一个对话式 Agent：删除辅助、标准只读、自主三档模式，
删除公开审批 API、审批界面及执行前等待审批流程。现有注册工具在 capability 白名单、JSON
Schema、项目权限、预算、数据版本、预检和 Evidence 约束通过后自动执行。该修订同时修复了
暂停任务持有对话锁、SSE 心跳掩盖无响应而导致界面长期显示“生成中”的问题：同一对话只允许
一个活动 TaskRun，前端具有首事件/空闲超时，未完成任务必须继续或取消后才能发送下一条消息。

本轮已完成安全与可运行性加固：`dataset_ref` 只能是服务端生成的 32 位不透明标识符；
Bearer token 映射到用户/租户/角色，项目、对话、数据集、任务和报告均做成员隔离；模型、
工具和整轮任务有独立超时，断连及启动恢复不会遗留运行态；歧义需求会进入
`waiting_user`，无依据数字和知识来源会在交付前重验或确定性修复；上传源文件、临时截图、
孤儿数据和项目报告均有清理路径。根 Compose、生产认证登录页和无 Mock 全栈 E2E 也已落地。

2026-08-27 的收尾审查删除了请求前的独立 LLM Planner 和失败后的 LLM Replanner。任务提纲
现在由本地确定性规则即时生成，仅用于澄清、进度和审计；所有冻结且可用的工具都交给同一次
Agent function-calling，工具错误直接返回该 Agent 修正。由 Schema、权限、预算、数据版本、
Join 前置条件、Evidence/Artifact 与最终 Verifier 继续承担硬约束。旧的单次分析/统计/知识问答
HTTP 入口和未接线占位模块也已删除，Web 只保留统一对话入口及报告下载。

> 2026-08-27 修复备注：数值 Claim 与 Evidence 的匹配现已支持确定性的显示舍入，以及
> `%`、`千`、`万`、`亿` 明确单位换算，精确值仍优先，避免浮点尾差或常规展示格式导致真实
> 结论被误删。TaskRun 完成边界现会绑定 Agent 实际选择的下游工具步骤、将已被下游成功结果
> 覆盖的前置提纲步骤显式记为 `skipped`，并由 Verifier、普通状态迁移和控制迁移共同阻止
> “运行已完成但步骤仍 pending”。若仍有独立步骤未执行，同一个 Agent 会获得一次有界修复轮次，
> 重试后仍未收敛才失败关闭。本地相关回归为 65 项通过，Ruff、MyPy 和 `git diff --check`
> 通过；完整 `test_agent_loop.py` 在当前 WSL 测试环境仍有既有用例停在 TaskRun 创建前的
> `run_in_threadpool(reference_resolver.resolve)`，因此本次不把整套 Agent 回归记为全绿。

### 已实现

2026-09-28 交接安全修复已落实于工作区：PDF 禁止读取任意文件/请求外部资源，
FastAPI/Starlette 锁定到 0.121.3/0.49.3，Excel 不再信任后缀和 dimension 元数据，
报告正文数字在发布前绑定所选分析工件。验证记录和剩余限制见
[`交接审查与修复报告`](docs/CODE_REVIEW_2026-09-28.md)，接手入口见
[`开发交接文档`](docs/PROJECT_HANDOVER.md)。上方 2026-08-27 测试备注为历史记录；
2026-09-28 已确认线程池阻塞与执行沙箱相关，沙箱外完整后端回归通过。

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
- SQLite v6→v7 的历史 ApprovalRecord/幂等操作表继续兼容旧库，但公开审批 API、React
  审批交互和 Agent 审批暂停/消费路径已经撤下；当前生产目录没有需要人工审批的工具；
- React 任务协作面板以真实 TaskRun 驱动，展示状态、版本、任务提纲、步骤、Evidence 与工具审计，
  支持结构化澄清、暂停/恢复/取消、提纲不可变修订和单步重试；
- 单一 Agent 执行路径不再接受 `autonomy_mode`。写入型已注册工具与只读工具一样先经过
  capability、Schema、权限、预算、版本和后置条件校验，通过后自动执行；
- 同一对话在进程内 RunManager 和 SQLite 原子创建层均只允许一个活动 TaskRun；重复消息会
  明确返回 409，避免后台 producer 排队占锁而不产生 SSE 事件；
- 浏览器 SSE 消费区分心跳与业务事件，首个业务事件和后续空闲均有超时；任务未完成时输入区
  会锁定并引导用户在任务面板继续或取消，不再无限显示“生成中”；
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
- 当前 17 个生产 Agent 工具共用 MCP schema/能力元数据；官方 SDK
  `tools/list`/`tools/call`、Client Gateway 发现校验和无副作用影子比对已落地；
- API/Web 多阶段镜像、非 root 健康检查、SSE/鉴权下载代理、根 Compose 和镜像构建 CI；
- data/stats/chart/report/knowledge 五个独立 MCP 服务、逐服务认证/发现/健康、私网、
  Compose secrets、最小卷权限和容器浏览器/重启门禁；
- Bearer 认证、租户/项目成员隔离，以及浏览器会话级令牌录入；
- 模型/工具/整轮超时，断连终态，启动时运行态恢复与未知调用保护；
- 上传、parquet、报告和临时截图的受限生命周期清理；
- 不使用网络 mock 的 Web→API→Excel→SQLite/parquet Playwright 全栈 E2E；
- v2 生命周期 SSE 双发以及 `GET /agent/runs/{run_id}` 和事件游标读取接口；
- fast/template 确定性任务提纲、持久化 TaskPlan/TaskStep 与步骤状态绑定；提纲不限制
  Agent 的工具选择，也不在首次模型回复前增加额外模型调用；
- 工具失败、Schema/角色门禁错误会作为结构化观察返回同一个 Agent，由它修正参数或收敛作答；
  已完成步骤和 Evidence 不被改写，最终成功仍必须通过确定性 Verifier；
- `/chat/stream` 后台 producer 与浏览器订阅解耦；断线不取消任务，澄清回答、
  pause/resume/cancel 和单步 retry 均使用项目写权限、`If-Match` 与幂等键；
- pause/等待澄清会原子写入 Checkpoint；API 宿主丢失后可在同一 `run_id` 上恢复
  Contract、活动计划、预算和已完成步骤，结果未知的活动工具调用禁止自动重试；
- 浏览器按服务端最近 TaskRun 恢复，SSE 使用 `Last-Event-ID` 游标续接并按事件 ID/sequence
  去重；工具服务、版本、权限、执行时健康及 Evidence/Artifact 在协作面板保持同源展示；
- React 对话工作区、SSE 理解/执行/图表/表格/报告/引用卡；
- bge-m3 稠密+稀疏检索、reranker、Milvus Lite/Standalone、知识文档生命周期和 CI 质量门禁；
- 固定版本 Milvus Standalone 部署、readiness、代际状态、回滚、清理、备份恢复和负载测试工具。

### 收尾结果与当前边界

- C1 已完成：基础统计依赖进入核心运行时，Prophet 独立为 `forecast` extra；锁文件、默认命令、
  Docker 依赖档位和干净核心环境 API import/lifespan/readiness 烟测保持同源；
- C2 已完成：未实现的 `multi_layout` 已从 chart MCP、Schema 和设计声明撤下；目录完整性回归
  会拒绝缺少治理元数据、完整输出契约或直接抛出 `NotImplementedError` 的公开工具；
- 2026-08-26 本地已通过干净核心环境烟测、完整后端测试、全仓 Ruff、前端 lint/build；
  提交 `eb7789c` 的 [GitHub Actions run 32883619141](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/32883619141)
  已确认 backend、frontend 与真实 Compose 三项作业全绿，项目已进入功能冻结；当前 WSL
  未启用 Docker Desktop 集成仅是本地环境限制，不再构成发布阻塞；

- 任务提纲进度映射、同一 Agent 的 Observation 纠错、结构化澄清回答、单步重试、SSE 游标重连和
  服务端最近 Run 恢复已实现；Web/API/工具服务重启浏览器门禁已通过真实 Compose CI；
- 当前 TaskContract 解释器只覆盖非空答复与高置信图表/报告后置条件；语义覆盖首轮模型评测
  未通过，生产保持禁用；
- 更广泛的非数值 Claim 和同值路径语义消歧尚未完成；
- 记忆控制面、TaskRun 快照、确定性上下文压缩和 3C 指代质量/恢复门禁已完成；
  3D 用户治理与 3E 五阶段血缘/恢复已通过真实本机 full-stack E2E 和
  Docker/Compose CI；
  长期记忆自动提取尚未设计，当前继续禁止通用模型 `memory.write`；
- 静态 Bearer 鉴权已落地；OIDC/OAuth、成员管理 API 和企业审计尚未实现；
- Agent Executor 已切到 MCP Client Gateway；支持 stdio/认证 Streamable HTTP、
  Host RequestContext、超时/取消、健康代次和只读幂等有限重连，部署环境禁止进程内降级；
- 单机生产结构 Compose、Milvus CPU/GPU RAG profile 及阶段 6 重型分析工具 profile 已提供；
  多实例外置状态和对象存储不在当前收尾范围；
- 前端已提供结构化澄清、计划编辑、暂停/续跑、工具来源/权限/健康审计视图、SSE 重连、
  分析分支对比和追加式反馈闭环；产品只保留一个自动调用受治理工具的对话式 Agent；
- SQLite v8 领域定义、字段映射、受控公式编译和 `domain_definition_lookup` 已接入 Evidence；
  `knowledge-tools` 已提供按签名 project/subject/conversation 过滤的领域定义 Resource
  `list/read`，共享服务 token 不作为用户身份；
- SQLite v9 为 TaskRun 原子保存内容寻址的 capability/tool 目录快照；新增工具只对新任务可见，
  已冻结工具缺失、版本/契约或服务路由漂移时恢复失败关闭，任务提纲与模型工具集不读
  运行中的新目录；
- SQLite v10 为 TaskRun 原子固定共享预算、数据集版本绑定和取消树；独立只读幂等分支
  可有界并行，结果由 Host 按统一 Evidence Ledger 原子提交，不跨版本、不分裂预算；
- SQLite v11 以不可变边表保存多父 Dataset 血缘和 TaskRun 双父版本绑定；受治理 Join 先预检，
  再由 Host 校验完全一致的参数和数据版本后自动执行，并继承两侧更严格的数据策略；
- 知识库仍是实例级共享资源；当前收尾版本明确限定单租户部署，不承诺跨租户索引隔离；
- 前端主包仍较大，需对 ECharts 与报告卡片做动态拆包。

### 已规划但未实现

- **v2.4 收口**：阶段 2 的 20×3 真实行为对照已完成并通过自动门禁（任务成功率
  70.0%、终态如实率 73.3%、越界 0），Compose/容器 CI 已全绿；现有评测全部使用商业
  数据语境，G7 人工盲评/签字已取消，因此仍不能宣称代表性产品验收通过；
- **v2.5**：阶段 3A–3E、4A–4D、阶段 5 和阶段 6A～6E 工程门禁已完成并通过真实 Compose CI；
  真实 CPU/GPU semantic 等价和领域签字已取消且保持“未验证”；
- **独立安全项目**：以隔离 MCP Server/运行环境交付受限 SQL、受限 Code Interpreter，普通 Docker 容器不替代代码沙箱；
- **v3.0 候选**：内部数据连接器、后台主动任务、外部 MCP 准入与企业授权、外置状态和
  容器发布供应链；多 Agent 只有在单 Agent 真实工作负载评测证明存在质量或并行收益时才考虑，
  完整多租户隔离已取消。

这些能力已进入路线图，但不得在代码和交付说明中提前标记为完成。

## Agent 演进路线

```text
当前单一 Agent 收尾基线
  理解目标 → 必要澄清 → 确定性任务提纲 → Agent function-calling
      ↑                                      ↓ 工具结果/错误回传同一 Agent
  持久状态 ← 最终交付 ← Verifier ← Evidence/Artifact
          ├── 不可变提纲版本与步骤进度
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
  SQLite v7 + 计划干预 + TaskRun 控制与恢复
  React 协作 → 服务端恢复/SSE 重连/工具审计 → 分支/反馈 → 单一 Agent 执行路径

v2.5 阶段 5（工程关闭；真实 CPU/GPU semantic 与领域签字取消且未验证）
  SQLite v8 版本化定义 + 受控公式 + Evidence → MCP Resource list/read
  定义/数据 Claim + 旧报告复核 → 目录分页/订阅/通知 → 双传输/重连 → RAG 生命周期

v2.5 阶段 6A（已关闭；完整 CI 与 Compose 恢复门禁全绿）
  SQLite v9 TaskRun capability/tool 快照 → tools/list_changed 换代 → profile 可用性
  SQLite v10 共享预算/固定数据版本/取消树/Evidence Ledger → ready frontier 有界并行

v2.5 阶段 6B（已关闭；完整 CI 与 Compose 恢复门禁全绿）
  数据角色/置信度/歧义 + 只读质量建议 → 结构化角色确认/下游前置条件 → 代表性评测/部署门禁

v2.5 阶段 6C（已关闭；完整 CI 与 Compose 恢复门禁全绿）
  有界候选筛选 → 用户选择 → Evidence/Verifier 生命周期 → 确定性跟进/发布门禁

v2.5 阶段 6D（已关闭；完整 CI 与 Compose 预测恢复门禁全绿）
  统一统计 Evidence → 受治理贡献/分群/回归诊断 → 独立预测 Tool/Profile → 发布门禁

v2.5 阶段 6E（已关闭；完整 CI 与 Compose 恢复门禁全绿）
  双数据集选择/Join 预检 → 受治理执行/多父版本血缘 → React 协作 → 发布门禁

横向交付轨
  MCP：单源契约 → Client Gateway → 五服务独立路由与认证
  Docker：仅 Web 公网入口 → 私网 API/MCP → 分卷/secrets/重启 E2E

v2.5 延伸
  MCP：记忆/Evidence 引用 → 知识 Resource → 自主分析能力目录
  Docker：状态恢复 → 代理 E2E → RAG CPU/GPU 生命周期 → 重型分析工具资源 profile

v3.0
  数据连接器 + 主动任务 + 外部 MCP 治理/OAuth
  外置状态 + 镜像供应链/多实例；多 Agent 仅保留为评测驱动候选
```

完整阶段、依赖和验收标准见 [`docs/Agent自主化开发规划.md`](./docs/Agent自主化开发规划.md)。
v2.4 详细设计与阶段 1–2E 实施状态见 [`docs/v2.4/README.md`](./docs/v2.4/README.md)。
v2.5 阶段 3–6 关闭证据与
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
parquet/报告文件 + SQLite v11 + Local/Milvus 知识库
```

Dify 已放弃。Agent 生产执行已使用 MCP 单源契约、官方 SDK Server/Client 和受治理
Client Gateway；进程内适配只用于迁移兼容/测试。根 Compose 已把 17 个 Agent 工具分配到
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

## 本地启动

### 前置环境

- Python `3.11`、[`uv`](https://docs.astral.sh/uv/)、Node.js `20`、pnpm `9`；
- 方案 B/C 还需要 Docker Engine 或 Docker Desktop，并能执行 `docker compose version`；
- 使用对话 Agent 时，在 `.env` 中填写有效的 `DEEPSEEK_API_KEY`；只检查 health/知识库
  存储不要求模型 API key；
- 完整 CPU BGE 建议给 WSL/宿主机至少 16 GiB 内存并准备约 5 GiB 模型磁盘空间。

所有命令默认从仓库根目录执行。

### 方案 A：轻量本地启动（推荐新开发者）

首次配置和安装：

```bash
cp .env.example .env
cp config/models.example.yaml config/models.yaml
cp config/data_policy.example.yaml config/data_policy.yaml
uv sync
pnpm --dir apps/web install
```

确认 `.env` 使用轻量 RAG：

```dotenv
RAG_EMBEDDER=hashing
RAG_RERANKER=lexical
RAG_STORE=local
RAG_RUNTIME_PROFILE=baseline
EMBEDDING_DEVICE=cpu
```

终端 1 启动 API：

```bash
uv run uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000
```

终端 2 启动 Web：

```bash
pnpm --dir apps/web dev --host 127.0.0.1 --port 5173
```

### 方案 B：完整 BGE + Docker Milvus

该方案让 Milvus/etcd/MinIO 运行在 Docker 中，API、BGE 和 Web 运行在本机/WSL。这样只加载
一份 BGE，适合 16 GiB 左右的 CPU 开发环境。首次安装和初始化必须按
[`本地完整 BGE 与 Milvus 启动指南`](./docs/本地完整BGE与Milvus启动指南.md)执行；完成后
日常启动只需三个终端：

```bash
# 终端 1：Milvus
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml up -d

# 终端 2：API；不要加 --reload，避免完整 BGE 重复加载
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 \
uv run --no-sync uvicorn apps.api.main:app --host 127.0.0.1 --port 8000

# 终端 3：Web
pnpm --dir apps/web dev --host 127.0.0.1 --port 5173
```

首次导入或原文变更后，通过已加载模型的 API 重建，避免另一个进程重复加载 BGE：

```bash
curl --fail --request POST http://127.0.0.1:8000/kb/rebuild \
  --header 'Content-Type: application/json' --data '{}'
curl --fail http://127.0.0.1:8000/health/ready
uv run --no-sync python scripts/kb_admin.py status
```

### 方案 C：根 Compose（生产结构验收）

默认根 Compose 使用轻量 RAG，构建 API、Web、edge 和五个 MCP 服务：

```bash
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8080/api/health/ready
```

完整 CPU/GPU RAG profile、认证、secrets、卷和真实 E2E 命令见
[`docs/全栈部署与E2E.md`](./docs/全栈部署与E2E.md)与
[`docs/知识库部署与运维.md`](./docs/知识库部署与运维.md)。

### 启动失败时先检查

```bash
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml ps
curl --fail http://127.0.0.1:9091/healthz
curl --fail http://127.0.0.1:8000/health/ready
```

- `RAG_RUNTIME_PROFILE=cpu` 必须同时使用 `bge/bge/milvus + EMBEDDING_DEVICE=cpu`；
- `ModuleNotFoundError: FlagEmbedding/pymilvus`：重新执行 `uv sync --extra rag`；
- Milvus `permission deny ... CreateCollection`：重新运行修复后的
  `scripts/milvus_bootstrap.py`，业务角色需要 `default` 数据库的 `DatabaseAdmin` 和
  `CollectionAdmin`；
- WSL/Docker credential helper、模型下载超时和内存问题见完整启动指南的排错章节。

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
uv run --no-sync python -m apps.api.workspace_admin backup --service-stopped
uv run --no-sync python -m apps.api.workspace_admin verify --input .data/workspace_backups/<backup>
uv run --no-sync python -m apps.api.workspace_admin restore \
  --input .data/workspace_backups/<backup> \
  --service-stopped --yes --replace-files
```

恢复会先在 `WORKSPACE_BACKUP_DIR` 生成 `pre-restore-*` 覆盖前副本。知识库不在这个
工作区备份中，继续使用独立的 `kb_admin` / Milvus 备份流程。

## 依赖档位

`uv sync` 会把当前虚拟环境精确同步到本次命令声明的依赖集合。不要依次执行多个只带
一个 extra 的命令来“累加”能力，后一次同步可能移除前一次安装的 extra。应在同一条命令
中声明所有需要的 extras，或者使用 `--all-extras`。

```bash
# 最小本地 API：包含基础统计和轻量 RAG
uv sync

# 基础统计 + 报告 + 截图（常用本地档位）
uv sync --extra report --extra chart-screenshot
uv run --extra report --extra chart-screenshot \
  playwright install --with-deps chromium

# 真实 bge/Milvus 语义检索
uv sync --extra rag

# 可选 Prophet 趋势预测实现（其他统计不需该 extra）
uv sync --extra forecast

# 安装全部可选能力；体积较大
uv sync --all-extras
```

启动命令应使用与安装命令相同的 `--extra ...` 或 `--all-extras`；也可以在完成同步后使用
`uv run --no-sync ...`，但此时由使用者负责保证环境没有漂移。离线环境需提前侧载模型
权重。`EMBEDDING_DEVICE=auto/cpu/cuda` 可切换推理设备而不改代码。

## 知识库运维入口

```bash
# 增量/全量重建
uv run --no-sync python scripts/kb_rebuild.py --mode incremental
uv run --no-sync python scripts/kb_rebuild.py --mode full

# 质量门禁、状态和负载测试
uv run --no-sync python scripts/kb_eval.py --enforce --json-output .data/kb-eval.json
uv run --no-sync python scripts/kb_admin.py status
uv run --no-sync python scripts/kb_load_test.py --requests 50 --concurrency 2
```

详细说明：

- [`docs/知识库升级验收基线.md`](./docs/知识库升级验收基线.md)
- [`docs/知识库部署与运维.md`](./docs/知识库部署与运维.md)
- [`docs/数据画像安全策略.md`](./docs/数据画像安全策略.md)
