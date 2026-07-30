# v2.4 设计、G7 与阶段 2 实施入口

> 状态：阶段 1 已验收通过；阶段 2A–2E 已实现，60-run 真实行为对照已通过自动门禁，
> 最新 Compose/容器 CI 全绿；G7 因现有商业场景代表性不足、Verifier 评分契约待补和
> 负责人签字未完成而保持 `review_required`
> · 更新日期：2026-07-29
> 范围：设计依据、阶段 0 未关闭人工门禁、阶段 1 验收和阶段 2 实施状态

本目录把 `docs/Agent自主化开发规划.md` 中的 v2.4 展开为实现依据。当前生产基线仍是
带 v2.4 控制面的 Agent；阶段 1 已在阶段 0 门禁未全部关闭时先行收尾，并在推送后由 GitHub Actions run #5
（commit 32e6685）关闭镜像门禁、正式验收通过；阶段 0 未关闭项继续作为进入阶段 2 的显式
前置债务。2026-07-27 经项目负责人明确授权开始 G7 收口和阶段 2A；这不等于人工评审已经
签字，也不允许把 ADR 标成“接受”。阶段 2A 与 2B 实际边界分别见
[`G7评审与阶段2A实施记录.md`](./G7评审与阶段2A实施记录.md) 和
[`阶段2B实施记录.md`](./阶段2B实施记录.md) 和
[`阶段2C实施记录.md`](./阶段2C实施记录.md) 和
[`阶段2D实施记录.md`](./阶段2D实施记录.md) 和
[`阶段2E实施记录.md`](./阶段2E实施记录.md)。

## 设计产物

| 产物 | 内容 | 当前状态 |
|---|---|---|
| [控制面与持久化设计](./控制面与持久化设计.md) | TaskContract、AgentState、状态机、Verifier、SQLite v2、事务和迁移 | 草案完成 |
| [SSE 与任务控制协议](./SSE与任务控制协议.md) | v2 事件 envelope、事件顺序、旧事件兼容、暂停/恢复/取消接口 | 草案完成 |
| [Planner 与 Verifier 评测设计](./Planner与Verifier评测设计.md) | 混合 Planner、确定性/语义 Verifier 边界、重复评测、go/no-go 规则 | 三轮及阶段 2 对照完成；商业回归保留，代表性盲评待补 |
| [MCP 与 Docker ADR](./MCP与Docker架构决策.md) | MCP 版本/SDK/传输/上下文边界，以及镜像、Compose、卷和安全边界 | 双传输探针已通过；ADR 待 G7 评审接受 |
| [MCP 与 Docker 全阶段演进](../MCP与Docker全阶段演进设计.md) | v2.5 阶段 3–6、安全项目和 v3.0 阶段 7–8 的扩展设计 | 总体设计完成；3A 计划已冻结 |
| [v2.5 实施入口](../v2.5/README.md) | 阶段 3–6 边界、评测纪律和阶段 3A 入口 | 阶段 3A 核心路径已实现，恢复门禁待完成 |
| `scripts/agent_eval_set.jsonl` | 20 个机器可读行为场景 | 已冻结并完成三轮真实基线 |
| `scripts/stage2_behavior_eval.py` | 同场景开启结构化计划控制面的阶段 2 成功率/终态对照 | 60-run 完成；成功 70.0%、终态如实 73.3%、越界 0，自动门禁 PASS |
| `scripts/semantic_verifier_eval_set.jsonl` | 语义覆盖正反 fixture | v3 新 heldout 三轮实测完成，仍 NO_GO |
| [阶段 1 实施记录](./阶段1实施记录.md) | 已交付切片、测试证据、外部门禁和下一步 | 验收通过（run #5 全绿） |
| [阶段 0 继续计划](./阶段0继续计划.md) | 未关闭门禁的执行方法、顺序、产物与 go/no-go | G1–G6 完成；G7 待人工 |
| [G7 与阶段 2A 实施记录](./G7评审与阶段2A实施记录.md) | 自动签字门禁、冻结阈值、生产 Planner/计划持久化和后续边界 | 阶段 2A 已实现；G7 待人工评分/签字 |
| [阶段 2B 实施记录](./阶段2B实施记录.md) | 依赖图调度、Observation 重规划、计划版本安全和条件跳过 | 已实现并完成定向回归 |
| [阶段 2C 实施记录](./阶段2C实施记录.md) | 后台 run 生命周期、任务控制写接口、Checkpoint 同 run 恢复和步骤重试 | 已实现并完成定向回归 |
| [阶段 2D 实施记录](./阶段2D实施记录.md) | MCP Gateway 规范执行、双传输、健康/重连/取消和受控降级 | 已实现并完成全量回归 |
| [阶段 2E 实施记录](./阶段2E实施记录.md) | 五个独立工具服务、Compose 私网/secrets/分卷及浏览器重启门禁 | 已实现；最新 Docker runner 全绿 |

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

## 阶段 0 实测已完成、仅剩人工环节

2026-07-23 已完成的自动/实测部分：

- 成本可用化（G3）、MCP stdio/Streamable HTTP 双传输探针（G5）、SQLite v1→v2→v1 隔离演练（G6）；
- Planner 三轮全量实测（G1）：240 records，**按模型选型——Flash 合格、Pro 禁止承担 Planner 路由**
  （Pro B14 遗漏 `data.aggregate`）；go/no-go 汇总已改为逐模型；
- 语义 Verifier v3 三轮全量（G4）：16 场景（含 10 新 heldout）×3×2=96 runs，仍 `NO_GO`
  （flash false_pass 3 / pro 4+2 协议错误），**语义保持禁用、生产仅确定性 Verifier**；
- v2.3 行为基线（G2）：120 runs，越界违反 0，任务成功 flash 26.7%/pro 20% 作阶段 2 对照；
- 阶段 2 Flash 对照：60 runs，任务成功 70.0%、终态如实 73.3%、越界 0，自动门禁通过；
- 据实测提议冻结门槛（评测设计 §10.6）。证据见 `.data/evaluations/v2.4/stage0-acceptance-20260723/`。

阶段 0 自身唯一未完成的是 item 6（人工，自动测试无法替代）：补充能代表真实工作负载的
Planner/Verifier 盲评、完善 Verifier 评分契约、设计评审签字，以及据评审把 ADR 从“草案”
改“接受”并正式冻结软门槛。2E Docker runner 已全绿；人工项缺失时 G7 继续保持
`review_required`。执行方法见
[`G7评审与阶段2A实施记录.md`](./G7评审与阶段2A实施记录.md)。
