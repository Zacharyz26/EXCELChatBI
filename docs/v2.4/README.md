# v2.4 设计与阶段 1 实施入口

> 状态：阶段 0 门禁待关闭；阶段 1 本地实现完成、远端镜像门禁待运行 · 更新日期：2026-07-23  
> 范围：设计依据、阶段 0 未关闭门禁和阶段 1 实施状态；本版本不新增分析工具

本目录把 `docs/Agent自主化开发规划.md` 中的 v2.4 展开为实现依据。当前生产基线仍是
v2.3；用户已授权完成阶段 1，因此本地实现已在阶段 0 门禁未全部关闭时先行收尾，未关闭项
继续作为显式债务。只有 [`阶段1实施记录.md`](./阶段1实施记录.md) 标记“已实现”的条目可以
写成交付事实，其余设计仍不得提前宣称完成。

## 设计产物

| 产物 | 内容 | 当前状态 |
|---|---|---|
| [控制面与持久化设计](./控制面与持久化设计.md) | TaskContract、AgentState、状态机、Verifier、SQLite v2、事务和迁移 | 草案完成 |
| [SSE 与任务控制协议](./SSE与任务控制协议.md) | v2 事件 envelope、事件顺序、旧事件兼容、暂停/恢复/取消接口 | 草案完成 |
| [Planner 与 Verifier 评测设计](./Planner与Verifier评测设计.md) | 混合 Planner、确定性/语义 Verifier 边界、重复评测、go/no-go 规则 | 语义 v2 首轮 NO_GO；Planner/重复评测未完成 |
| [MCP 与 Docker ADR](./MCP与Docker架构决策.md) | MCP 版本/SDK/传输/上下文边界，以及镜像、Compose、卷和安全边界 | 单源 adapter/Gateway 与基础镜像已实现；双传输探针待完成 |
| [MCP 与 Docker 全阶段演进](../MCP与Docker全阶段演进设计.md) | v2.5 阶段 3–6、安全项目和 v3.0 阶段 7–8 的扩展设计 | 规划草案 |
| `scripts/agent_eval_set.jsonl` | 20 个机器可读行为场景 | 草案完成，尚未跑真实模型基线 |
| `scripts/semantic_verifier_eval_set.jsonl` | 14 个语义覆盖正反 fixture | 已跑主模型/fallback 各一轮，均 NO_GO |
| [阶段 1 实施记录](./阶段1实施记录.md) | 已交付切片、测试证据、外部门禁和下一步 | 本地实现完成 |

## 文档边界与同步入口

- [`../Agent自主化开发规划.md`](../Agent自主化开发规划.md) 是阶段、优先级和验收的唯一现行路线图；
- [`MCP与Docker架构决策.md`](./MCP与Docker架构决策.md) 是 v2.4 项目内 MCP 与单机容器化的详细设计依据；
- [`../MCP与Docker全阶段演进设计.md`](../MCP与Docker全阶段演进设计.md) 约束 v2.5/v3.0 如何在 v2.4 基础上扩展；
- [`../ChatBI设计文档.md`](../ChatBI设计文档.md) 维护总体架构中的当前/目标边界；
- [`../数据画像安全策略.md`](../数据画像安全策略.md) 维护跨传输、容器挂载和数据可见性不变量；
- [`../知识库部署与运维.md`](../知识库部署与运维.md) 保留当前独立 Milvus 运维流程，并说明未来 `rag` profile 的迁移约束；
- [`../知识库升级验收基线.md`](../知识库升级验收基线.md) 维护 `knowledge-tools` 和容器链路不得降低的检索质量门禁。

若实现阶段改变 MCP 版本、传输、服务分组、Compose 拓扑或卷边界，应先修订 ADR，再同步
路线图、总体设计、安全策略、知识库运维和根 README；不得只修改 Docker/MCP 配置而留下
相反的文档描述。

## 已冻结的设计原则

1. 先扩展现有自研循环为类型化状态机；阶段 0 不引入 LangGraph。
2. TaskRun 是任务真相源，Conversation 只是用户交互容器，两者不能混为一张状态表。
3. 持久化采用追加 `TaskEvent` 与可重建 `TaskSnapshot`；关键状态变更使用乐观版本号。
4. Verifier 的确定性检查拥有否决权，语义模型只能判断目标覆盖等软条件。
5. Planner 规划 capability，不直接绑定工具名；简单、模板和 LLM 三条路径输出同一 TaskPlan。
6. 新 SSE 事件先与旧事件并行；生命周期事件先落库再发送，`text.delta` 不逐 token 落事件表。
7. 标准 MCP Client Gateway 是阶段 2 的规范工具执行路径；工具服务继续零 LLM。
8. Agent TaskRun 不绑定 MCP 的实验性 Tasks 能力，避免外部协议变更影响核心状态机。
9. Docker 负责可复现交付，不替代 Code Interpreter 安全沙箱，也不向业务容器开放 Docker Socket。

## 尚未完成的阶段 0 工作

- 使用真实 DeepSeek 主模型和 fallback 跑 Planner 重复实验；语义 Verifier v2 首轮已跑但
  出现 false PASS，需要新候选和新 heldout 后重新验证；
- 对 v2.3 跑 20 场景基线并记录任务成功、虚假完成、无依据 Claim、调用数、延迟和成本；
- 根据实测冻结 Planner/Verifier 数值门槛；
- 用一个现有工具完成 MCP Python SDK 的 stdio 与 Streamable HTTP 双传输探针；
- 在隔离副本上验证 SQLite v1→v2→v1 迁移和回滚设计；
- 完成设计评审并把 ADR 状态从“草案”改为“接受”。

阶段 0 的上述实测仍未全部完成。阶段 1 本地实现已收尾，但这些门禁不会因此视为通过；
涉及语义模型、MCP 规范路径和正式发布的工作仍必须先取得对应实测结论。
