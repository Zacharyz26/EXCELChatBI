# v2.4 SSE 与任务控制协议

> 状态：草案 · schema version：`2`  
> 目标：让任务过程可追踪、可恢复、可干预，同时保持 v2.3 前端兼容

## 1. 现状与边界

当前 `/chat/stream` 发送 `meta`、`understanding`、`plan`、`tool_start`、`tool_end`、`artifact`、`text.delta`、`error` 和 `done`。其中 `plan` 只是当前模型轮次的工具调用列表，事件没有 run ID、持久序号或恢复游标。

v2.4 保留 `/chat/stream`，在响应中增加 `run_id` 并并行发送 v2 生命周期事件。浏览器与 API 的 SSE 是产品协议，不等同于 MCP Streamable HTTP 中可选的 SSE 响应流。

## 2. v2 事件 envelope

除 `text.delta` 外，新事件统一使用：

```json
{
  "schema_version": 2,
  "event_id": "01J...",
  "run_id": "run_...",
  "conversation_id": "conv_...",
  "sequence": 7,
  "occurred_at": "2026-07-22T10:00:00.000Z",
  "payload": {}
}
```

SSE 帧：

```text
id: run_...:7
event: step.completed
data: {完整 envelope JSON}
```

约束：

- `sequence` 在单个 run 内从 1 严格递增，不要求跨 run 全局有序；
- 生命周期事件先与 AgentState 在同一事务落库，提交后发送；
- 客户端按 `(run_id, sequence)` 去重；收到缺口先拉取事件，再更新 UI；
- `event_id` 全局唯一，SSE `id` 是可读游标，不作为数据库主键；
- payload 不包含密钥、原始整表、完整内部 Prompt 或模型思维链；
- `text.delta` 是瞬时渲染数据，不逐 token 写 `task_events`。断线后通过持久化消息恢复最终文本，不承诺重放每个 token。

## 3. 事件目录

### 3.1 `goal`

TaskContract 已建立。

```json
{
  "goal": "比较各地区月度销售趋势并生成 PDF",
  "success_criteria": [
    {"id": "c1", "description": "包含地区月度趋势证据", "required": true},
    {"id": "c2", "description": "生成可下载 PDF", "required": true}
  ],
  "assumptions": [],
  "status": "planning"
}
```

只展示目标摘要和明确假设，不展示 Goal Interpreter 的内部推理。

### 3.2 `clarification`

发出一个可回答问题；一个事件只包含一个问题，便于独立校验和恢复。

```json
{
  "question_id": "q_metric",
  "text": "销售指标应使用销售额还是订单量？",
  "reason": "两列都符合用户表述，选择会改变结论",
  "answer_schema": {
    "type": "string",
    "enum": ["销售额", "订单量"]
  }
}
```

客户端不得把自由文本答案直接写入计划，必须经服务端 answer schema 和权限校验。

### 3.3 `plan.created`

```json
{
  "plan_id": "plan_...",
  "version": 1,
  "route": "llm",
  "summary": "先检查数据质量，再比较趋势，最后生成报告",
  "steps": [
    {
      "step_id": "quality",
      "purpose": "确认分析数据可用",
      "capability": "data.quality",
      "dependencies": [],
      "status": "pending"
    }
  ]
}
```

### 3.4 `plan.updated`

```json
{
  "plan_id": "plan_...",
  "version": 2,
  "supersedes_version": 1,
  "reason": "质量检查发现 3 个异常点，按用户目标增加受控排除步骤",
  "steps": []
}
```

`reason` 是简短行动理由，不是模型原始思维链。旧版本不可覆盖或删除。

### 3.5 `step.started`

```json
{
  "plan_version": 2,
  "step_id": "trend",
  "attempt": 1,
  "purpose": "比较各地区趋势",
  "capability": "stats.trend",
  "tool": "trend_analysis",
  "invocation_id": "inv_...",
  "fields": "时间列: 月份；指标: 销售额；分组: 地区"
}
```

只发送允许展示的参数摘要；完整参数由有权限的调试入口读取，不默认发浏览器。

### 3.6 `step.completed`

```json
{
  "plan_version": 2,
  "step_id": "trend",
  "attempt": 1,
  "status": "completed",
  "summary": "已生成地区趋势证据",
  "observation_id": "obs_...",
  "evidence_ids": ["ev_..."],
  "artifact_ids": ["art_..."]
}
```

失败时 `status=failed`，增加稳定 `error_code`、用户可读 `message`、`retryable` 和下一步建议；不向浏览器发送堆栈或密钥。

### 3.7 `verification`

```json
{
  "verification_id": "ver_...",
  "verdict": "NEEDS_ACTION",
  "criteria": [
    {"criterion_id": "c1", "status": "pass", "checker": "deterministic"},
    {"criterion_id": "c2", "status": "fail", "checker": "artifact", "reason": "PDF 文件不存在"}
  ],
  "next_actions": [
    {"kind": "replan", "capability": "report.generate", "reason": "重新生成 PDF"}
  ]
}
```

### 3.8 `waiting_user`

```json
{
  "status": "waiting_user",
  "question_ids": ["q_metric"],
  "resume_token": "opaque-short-lived-token",
  "expires_at": null
}
```

`resume_token` 绑定 run、用户和问题，不包含权限信息，不能替代重新授权。

### 3.9 继续保留的事件

- `artifact`：沿用当前 `ArtifactResponse` 结构，必须在数据库和文件后置条件通过后发送；
- `text.delta`：沿用 `{delta}`，只用于最终文本和兼容提示；
- `error`：表示流或协议错误，不自动等于 TaskRun=`failed`，增加 `run_status` 和 `terminal`；
- `done`：表示本次 SSE 流结束，增加 `run_id`、`run_status`、`last_sequence`。`run_status` 可以是 `waiting_user` 或 `paused`，不能再默认等于任务完成。

## 4. v1 兼容映射

阶段 1/2 采用双发，不要求旧前端理解新事件：

| v2 事件 | v2.3 兼容事件 | 规则 |
|---|---|---|
| `goal` | `understanding` | 使用目标摘要，不重复模型开场白 |
| `plan.created` | `plan` | 把步骤快照映射为旧卡片结构 |
| `plan.updated` | `plan` | 发送最新完整快照，旧 UI 替换当前计划 |
| `step.started` | `tool_start` | 使用解析后的 tool 和字段摘要 |
| `step.completed` | `tool_end` | 状态映射为 ok/error |
| `verification` | 无 | 旧客户端忽略 |
| `clarification` | `text.delta` | 显示问题正文 |
| `waiting_user` | `done` | `run_status=waiting_user`，结束当前连接 |
| `artifact/text.delta/error/done` | 同名 | 只发送一次，不做重复事件 |

兼容窗口至少覆盖一个稳定版本。固定浏览器回归通过后，才可删除旧 `understanding/plan/tool_*`；服务端事件表永久保留 v2 语义，不存 v1 派生事件。

## 5. 顺序与故障语义

正常顺序：

```text
meta(v1 扩展 run_id)
goal
plan.created
step.started → artifact? → step.completed
plan.updated? → step.started ...
verification (可能重复)
text.delta*
done(run_status=completed)
```

约束：

- `artifact` 必须早于引用它的 `step.completed`；
- `verification=PASS` 必须早于 `done(completed)`；
- `done(completed)` 之后不得再产生该 run 的事件；
- 工具失败不是流错误，先用 `step.completed(status=failed)` 表达，再由 Replanner 决定；
- 模型网关、数据库或事件协议本身不可用才发送 `error`；
- 客户端断开不自动取消任务。阶段 2 前当前同步请求可按旧行为结束；阶段 2 后由后台 run 生命周期继续或按配置暂停。

## 6. 事件恢复接口

阶段 1 增加只读接口：

```text
GET /agent/runs/{run_id}
GET /agent/runs/{run_id}/events?after_sequence=17&limit=200
```

第一项返回 AgentState 当前快照和活动计划；第二项按 sequence 返回已持久生命周期事件。用户必须属于 run 所在项目。

阶段 2 增加控制接口：

```text
POST /agent/runs/{run_id}/clarifications/{question_id}/answer/stream
POST /agent/runs/{run_id}/pause
POST /agent/runs/{run_id}/resume/stream
POST /agent/runs/{run_id}/cancel
POST /agent/runs/{run_id}/steps/{step_id}/retry/stream
```

写接口要求 `Idempotency-Key` 和 `If-Match: <state_version>`。版本不匹配返回 `409`，已在终态的 run 返回 `409`，权限不足返回 `403`，不存在或不可见统一返回 `404`。

## 7. 安全与数据最小化

- SSE 只展示行动理由、参数摘要、Evidence ID 和 Artifact；
- clarification 的枚举选项不得包含用户无权查看的数据值；
- 原始工具结果保存在受控结果存储，不通过事件广播；
- Event payload 进入日志前再次执行密钥和路径脱敏；
- `resume_token` 使用短期签名或服务端随机 token，单次消费并绑定主体；
- 项目删除时按本地数据策略级联删除事件；企业审计留存另走受控审计存储。

## 8. 协议测试

必须覆盖：

1. sequence 严格递增与重复消费幂等；
2. 事件落库失败时不向客户端提前发送；
3. Artifact 事件不早于文件和数据库记录；
4. v2→v1 映射不重复渲染 Artifact 或正文；
5. 断线后生命周期事件可从游标补齐，最终文本可由 Conversation 恢复；
6. waiting_user、paused、cancelled、blocked、failed 和 completed 均正确结束 SSE；
7. 旧前端忽略未知事件仍可结束 loading 状态；
8. 非法 `If-Match`、重复回答、重复取消和重复重试不会重复执行工具；
9. 事件与日志不泄漏原始整表、密钥、内部 Prompt 或堆栈。
