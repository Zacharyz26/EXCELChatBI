# v2.5 记忆、自主性与协作

> 状态：阶段 3–5 与 6A 工程关闭；阶段 6B-1/6B-2 已完成本地实现，等待完整 CI
> 更新日期：2026-08-11
> 基线：v2.4 阶段 2A–2E 已实现，最新 Compose/容器 CI 全绿；G7 因人工评测
> 代表性不足继续保持 `review_required`

本目录把 [`Agent自主化开发规划.md`](../Agent自主化开发规划.md) 中的 v2.5
阶段 3–6 拆成可实施、可验收的交付批次。进入 v2.5 不表示豁免 v2.4 的 G7：
现有 20 个行为场景全部使用订单、销售、利润、地区、渠道或复购率等商业分析语境，
可以继续作为控制面回归，但不足以证明产品适合尚未明确的真实工作负载。剩余人工盲评
暂缓，已完成的本地 Planner 评分只作为评分校准和商业场景回归证据，不用于伪造代表性
验收结论。

## 阶段总览

| 阶段 | 目标 | 当前决策 |
|---|---|---|
| 3：记忆系统 | 工作/对话/项目记忆、上下文压缩、指代消解、Evidence 血缘与恢复 | 已完成并通过真实 Docker/Compose CI |
| 4：人机协作 | 计划干预、审批、任务控制、恢复、自主等级、分支与反馈 | 已完成并通过真实 Docker/Compose CI |
| 5：知识与数据联合推理 | 版本化领域定义、公式、口径冲突与知识 Resource | 工程关闭并通过真实 Compose CI；CPU/GPU runtime semantic 与代表性签字是发布债务 |
| 6：自主分析 | 数据角色、结果驱动分析、多数据集、受控并行与统计护栏 | 6A 已由完整 CI 关闭；6B-1/2 本地实现完成，下一入口为代表性角色评测与双传输/Compose 门禁 |

受限 SQL 和 Code Interpreter 仍属于独立安全项目，不随 v2.5 普通功能自动启用。

## 阶段 3–5 交付记录与阶段 6 入口

阶段 3 已完成，已失去现行作用的 3A–3E“实施计划”不再单独保留；目标、实际交付、
边界和发布证据统一以实施记录为准：

| 子阶段 | 交付摘要 | 记录 |
|---|---|---|
| 3A | Memory 契约、SQLite v4、Policy、不可变快照、一致备份/恢复 | [实施记录](./阶段3A实施记录.md) |
| 3B | SQLite v5 持久压缩、固定引用、质量与恢复门禁 | [实施记录](./阶段3B实施记录.md) |
| 3C | Artifact/Dataset 指代、实体映射、双传输和固定计划恢复 | [实施记录](./阶段3C实施记录.md) |
| 3D | 项目记忆安全查询、不可变纠正、软删除和 React 治理 | [实施记录](./阶段3D实施记录.md) |
| 3E | SQLite v6 来源锚点、五阶段血缘、安全查看和联合恢复 | [实施记录](./阶段3E实施记录.md) |

提交 `0b5980c` 的 GitHub Actions
[run 30611880447](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/30611880447)
已确认 backend、frontend 和 containers/Compose 三个 job 全绿，阶段 3 正式关闭。
阶段 4A-1 已完成计划干预与审批的首批后端契约：SQLite v7、暂停边界的不可变计划修订、
ApprovalRecord 请求/决定/一次性消费，以及 subject、版本和幂等约束。4A-2 已把
`high/critical` 风险接入 Executor：工具调用前原子暂停，批准后按活动计划、步骤、契约和
参数精确消费；已消费绑定进入签名 MCP RequestContext，并由 Client Gateway 与 MCP Server
双重校验。实施范围、验证结果和未完成边界见
[阶段4A实施记录](./阶段4A实施记录.md)。4B 已在 React 工作区增加统一任务协作面板，
以类型化 API 和 POST SSE 接入计划修订、结构化澄清、暂停/恢复/取消、单步重试及
ApprovalRecord 决定；批准后仍需显式恢复。8 条浏览器测试覆盖原有 Artifact 回归和三条
协作契约。实施范围和未完成边界见 [阶段4B实施记录](./阶段4B实施记录.md)。4C 已实现
服务端最近 Run、SSE 游标重连/去重、跨浏览器恢复、工具/Evidence 审计投影以及 Web/API/
工具服务重启恢复门禁，并已由提交 `2f2771f` 的
[run 30782321622](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/30782321622)
验证关闭，详见 [阶段4C实施记录](./阶段4C实施记录.md)。4D 已完成三档自主等级、服务端
只读/审批映射、分析分支对比和追加式反馈闭环；本批进一步补齐反馈进入 LLM Planner、辅助
模式确认分支及标准只读副作用拒绝的真实 Compose 场景，详见
[阶段4D实施记录](./阶段4D实施记录.md)。竞态修复提交 `7f81fe2` 的
[run 30980088817](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/30980088817)
已确认 backend、frontend 和 Compose 浏览器/重启/离线恢复三个 job 全绿，阶段 4 正式关闭。
5A 已交付版本化定义、受控公式编译和 Evidence 接入，详见
[阶段5A实施记录](./阶段5A实施记录.md)。5B 首个切片已把领域定义接入按签名
project/subject 过滤的 MCP Resource list/read；5B-2 已贯通 Resource/Invocation/Evidence/
Claim 并提供旧报告历史定义复核；5B-3 已实现项目目录、签名分页游标、订阅/退订和版本化
变更通知；5B-4 已补 stdio/HTTP Resource 等价、Gateway 重新订阅和 Compose 服务重启门禁，
5B-5 已补原文/索引/缓存分离、索引切代回滚与 CPU/GPU 配置门禁。提交 `d5a672d` 的
[run 31063896157](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31063896157)
已确认 backend、frontend 与真实 Compose 三项全绿，阶段 5 工程关闭，详见
[阶段5B实施记录](./阶段5B实施记录.md)。CPU/GPU runtime semantic 与领域代表性签字仍是
显式发布债务。[阶段6A](./阶段6A实施记录.md) 已完成 6A-1 TaskRun 目录冻结、
6A-2 受治理 `tools/list_changed` 换代/profile unavailable 投影，以及 6A-3 受控并行基础、
任务审计投影和真实双 MCP 服务 Compose 恢复门禁；提交 `b67b704` 的
[run 31348476642](https://github.com/Zacharyz26/EXCELChatBI/actions/runs/31348476642)
已确认三个 CI 作业全绿并关闭 6A。[阶段6B](./阶段6B实施记录.md) 的 6B-1 已本地实现
`data.roles`、严格角色/质量输出契约、非自动清洗建议、Planner 路由与 React Artifact 展示；
6B-2 已把角色确认绑定 TaskRun 计划/数据版本，并在统计/聚合执行前接入确定性门禁。当前等待
完整 CI，下一入口为匿名代表性评测、双传输/Compose 和恢复门禁。

阶段 3 关闭状态：

1. **3A 契约与持久化**：已完成；
2. **3B 上下文压缩**：已完成，持久压缩、质量、并发和 Compose 恢复门禁全绿；
3. **3C 指代消解**：已完成，对象/记忆解析、23 场景质量门禁、双 MCP 传输和固定计划
   恢复探针全绿；
4. **3D 用户治理**：已完成，安全查询、不可变纠正、软删除、审计、真实本机
   full-stack E2E 和 Compose CI 全绿；
5. **3E 血缘与恢复**：已完成，Dataset → Analysis → Artifact → Evidence → Claim、
   稳定图 hash、备份、重启与离线恢复已通过真实 Compose runner 验证。

## 评测与发布纪律

- v2.4 的单元、协议、迁移、浏览器和 Compose 回归继续执行，不因盲评暂缓而跳过；
- 阶段 3–4 使用领域中立场景验证“第二个 Artifact”“刚才确认的字段”“删除旧别名”、
  跨项目隔离和重启恢复，不依赖利润/销售额等专用字段；
- 阶段 5–6 开始前，另建真实工作负载场景集；只需匿名 schema、典型请求、Required
  Artifact、澄清条件和禁止行为，不要求保存敏感原始数据；
- 现有商业场景保留为 `business regression`，不得与新的代表性验收集混合平均；
- 新场景、评分口径、重复次数和 hash 必须在运行前冻结；查看失败样本后的改进只能使用
  新的未见场景复评；
- 在人工评测、负责人签字和 ADR 接受前，v2.4 G7 状态继续为 `review_required`。

## 相关设计

- [`../Agent自主化开发规划.md`](../Agent自主化开发规划.md)：版本和阶段的唯一现行路线图；
- [`../MCP与Docker全阶段演进设计.md`](../MCP与Docker全阶段演进设计.md)：
  v2.5/v3.0 的 MCP、容器和安全边界；
- [`../v2.4/README.md`](../v2.4/README.md)：v2.4 已实现范围和未关闭门禁；
- [`../v2.4/Planner与Verifier评测设计.md`](../v2.4/Planner与Verifier评测设计.md)：
  现有冻结评测及代表性修订要求。
- [`./阶段5B实施记录.md`](./阶段5B实施记录.md)：阶段 5 工程关闭证据与发布债务。
- [`./阶段6A实施记录.md`](./阶段6A实施记录.md)：TaskRun capability 目录冻结与后续边界。
- [`./阶段6B实施记录.md`](./阶段6B实施记录.md)：数据角色、质量建议契约与未完成边界。
