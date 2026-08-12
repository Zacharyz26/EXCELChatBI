import { expect, test, type Page, type Route } from "@playwright/test";

const NOW = "2026-07-17T08:00:00Z";

interface MockWorkspaceState {
  project: Record<string, unknown>;
  conversation: Record<string, unknown>;
  datasets: Array<Record<string, unknown>>;
  messages: Array<Record<string, unknown>>;
  artifacts: Array<Record<string, unknown>>;
  kbDocuments: Array<Record<string, unknown>>;
  agentRuns: Record<string, MockAgentRunState>;
  chatRequests: Array<{
    message: string;
    autonomy_mode: string;
    parent_run_id: string | null;
  }>;
  reconnectCursors: string[];
  turn: number;
}

interface MockAgentRunState {
  detail: Record<string, any>;
  approvals: Array<Record<string, any>>;
  events: Array<Record<string, any>>;
}

function profile(datasetRef: string): Record<string, unknown> {
  return {
    dataset_ref: datasetRef,
    row_count: 24,
    column_count: 2,
    columns: [
      {
        name: "月份",
        dtype: "object",
        null_ratio: 0,
        distinct_count: 12,
        min: null,
        max: null,
        mean: null,
        std: null,
        median: null,
        sample_values: ["1月", "2月"],
      },
      {
        name: "销售额",
        dtype: "float64",
        null_ratio: 0,
        distinct_count: 24,
        min: 80,
        max: 180,
        mean: 125,
        std: 30,
        median: 120,
        sample_values: ["100", "140"],
      },
    ],
    sample_rows: [],
  };
}

function dataset(ref = "sales-ref", filename = "sales.xlsx"): Record<string, unknown> {
  return {
    ref,
    project_id: "project-1",
    filename,
    profile: profile(ref),
    parent_ref: null,
    transform: null,
    created_at: NOW,
  };
}

function message(
  id: string,
  role: string,
  content: string,
  toolCalls: Array<Record<string, unknown>> | null = null,
): Record<string, unknown> {
  return {
    id,
    conversation_id: "conversation-1",
    role,
    content,
    tool_calls: toolCalls,
    created_at: NOW,
  };
}

function artifact(
  id: string,
  messageId: string,
  type: string,
  payload: Record<string, unknown>,
  sourceTool: string,
): Record<string, unknown> {
  return {
    id,
    conversation_id: "conversation-1",
    message_id: messageId,
    type,
    payload,
    file_ref: null,
    source_tool: sourceTool,
    params: { analysis_id: `${id}-analysis` },
    dataset_ref: type === "report" || type === "citations" ? null : "sales-ref",
    created_at: NOW,
  };
}

function sse(frames: Array<[string, Record<string, unknown>]>): string {
  return frames
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join("");
}

function taskEvent(
  runId: string,
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
): Record<string, unknown> {
  return {
    schema_version: "2.0",
    event_id: `${runId}-event-${sequence}`,
    run_id: runId,
    sequence,
    event_type: eventType,
    payload,
    occurred_at: NOW,
  };
}

function taskSse(
  runId: string,
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
): [string, Record<string, unknown>] {
  return [eventType, {
    schema_version: "2.0",
    event_id: `${runId}-event-${sequence}`,
    run_id: runId,
    conversation_id: "conversation-1",
    sequence,
    occurred_at: NOW,
    payload,
  }];
}

function mockAgentRun(
  runId: string,
  goal: string,
  status: string,
  options: {
    approvals?: Array<Record<string, any>>;
    waitingPayload?: Record<string, unknown>;
    autonomyMode?: string;
    parentRunId?: string | null;
  } = {},
): MockAgentRunState {
  const definition = {
    schema_version: 1,
    summary: goal,
    steps: [{
      step_id: "prepare",
      purpose: "准备并核对数据范围",
      capability: "data.profile",
      dependencies: [],
      expected_evidence: ["范围摘要"],
      completion_conditions: ["范围已确认"],
      fallback: [{ when: "失败", action: "block" }],
    }],
    assumptions: [],
    clarifications: [],
  };
  const stepStatus = status === "completed" ? "completed" : "pending";
  const events = options.waitingPayload
    ? [taskEvent(runId, 3, "waiting_user", options.waitingPayload)]
    : [];
  return {
    detail: {
      run: {
        run_id: runId,
        project_id: "project-1",
        conversation_id: "conversation-1",
        user_message_id: `${runId}-user`,
        parent_run_id: options.parentRunId ?? null,
        goal,
        status,
        state_version: 4,
        plan_version: 1,
        budget: {
          max_tool_calls: 4,
          autonomy_mode: options.autonomyMode ?? "autonomous",
        },
        usage: { tool_calls: 0 },
        terminal_reason: null,
        created_at: NOW,
        updated_at: NOW,
        finished_at: status === "completed" ? NOW : null,
        autonomy_mode: options.autonomyMode ?? "autonomous",
      },
      contract: { goal },
      plan: {
        plan_id: `${runId}-plan-1`,
        version: 1,
        reason: "initial:template",
        definition,
        created_at: NOW,
      },
      steps: [{
        step_id: "prepare",
        persisted_step_id: `${runId}-step-1`,
        position: 0,
        status: stepStatus,
        definition: definition.steps[0],
        started_at: null,
        completed_at: stepStatus === "completed" ? NOW : null,
      }],
      tool_audits: [],
      execution_control: {
        schema_version: 1,
        max_tool_calls: 4,
        max_parallelism: 2,
        data_version_hash: "a".repeat(64),
        dataset_version_count: 1,
        evidence_ledger_version: 0,
        root_status: status === "completed" ? "completed" : "active",
        active_branch_count: 0,
        cancel_requested_branch_count: 0,
      },
      related_runs: [],
      feedback: [],
      state: { last_sequence: 3 },
    },
    approvals: options.approvals ?? [],
    events,
  };
}

function refreshRelatedRuns(state: MockWorkspaceState): void {
  const related = Object.values(state.agentRuns).map((item) => item.detail.run);
  for (const item of Object.values(state.agentRuns)) {
    item.detail.related_runs = related;
  }
}

function persistCollaborationTurn(
  state: MockWorkspaceState,
  prompt: string,
): { frames: Array<[string, Record<string, unknown>]> } {
  const suffix = String(++state.turn);
  const runId = `collaboration-run-${suffix}`;
  const userId = `${runId}-user`;
  const assistantId = `${runId}-assistant`;
  state.messages.push(
    message(userId, "user", prompt),
    message(assistantId, "assistant", "任务已进入协作安全边界。"),
  );

  if (prompt.includes("选择指标")) {
    const waitingPayload = {
      question_id: "metric",
      question: "请选择本次分析指标：销售额或订单量。",
      reason: "不同指标会改变分析结果。",
      about: "metric",
      answer_schema: { type: "string", minLength: 1 },
      resume_token: "resume-token-metric-0001",
      plan_id: `${runId}-plan-1`,
      plan_version: 1,
    };
    state.agentRuns[runId] = mockAgentRun(
      runId,
      prompt,
      "waiting_user",
      { waitingPayload },
    );
    return {
      frames: [
        ["meta", { run_id: runId, conversation_id: "conversation-1" }],
        taskSse(runId, 2, "plan.created", {
          plan_id: `${runId}-plan-1`,
          plan_version: 1,
          summary: prompt,
          steps: [],
        }),
        taskSse(runId, 3, "waiting_user", waitingPayload),
        ["text.delta", { delta: waitingPayload.question }],
        ["done", {
          run_id: runId,
          conversation_id: "conversation-1",
          run_status: "waiting_user",
          last_sequence: 3,
        }],
      ],
    };
  }

  const approvals = prompt.includes("高风险")
    ? [{
      approval_id: "a".repeat(32),
      run_id: runId,
      plan_id: `${runId}-plan-1`,
      plan_version: 1,
      step_id: "prepare",
      tool_name: "high_risk_export",
      tool_schema_hash: "b".repeat(64),
      parameter_summary_hash: "c".repeat(64),
      risk_level: "high",
      status: "pending",
      version: 1,
      expires_at: "2026-07-31T12:00:00Z",
      decision_reason: null,
      requested_at: NOW,
      updated_at: NOW,
      decided_at: null,
      consumed_at: null,
    }]
    : [];
  state.agentRuns[runId] = mockAgentRun(runId, prompt, "paused", { approvals });
  if (approvals.length > 0) {
    state.agentRuns[runId].events.push(
      taskEvent(runId, 4, "approval.requested", {
        approval_id: approvals[0].approval_id,
      }),
    );
  }
  return {
    frames: [
      ["meta", { run_id: runId, conversation_id: "conversation-1" }],
      taskSse(runId, 2, "plan.created", {
        plan_id: `${runId}-plan-1`,
        plan_version: 1,
        summary: prompt,
        steps: [{
          step_id: "prepare",
          purpose: "准备并核对数据范围",
          capability: "data.profile",
          dependencies: [],
          status: "pending",
        }],
      }),
      ...(approvals.length > 0 ? [
        taskSse(runId, 4, "approval.requested", {
          approval_id: approvals[0].approval_id,
          plan_version: 1,
          step_id: "prepare",
          risk_level: "high",
        }),
        ["approval_required", {
          approval_id: approvals[0].approval_id,
          run_id: runId,
          plan_version: 1,
          step_id: "prepare",
          tool: "high_risk_export",
          risk_level: "high",
          expires_at: approvals[0].expires_at,
        }] as [string, Record<string, unknown>],
      ] : []),
      ["done", {
        run_id: runId,
        conversation_id: "conversation-1",
        run_status: "paused",
        last_sequence: approvals.length > 0 ? 4 : 2,
      }],
    ],
  };
}

function persistReconnectTurn(
  state: MockWorkspaceState,
  prompt: string,
): { frames: Array<[string, Record<string, unknown>]> } {
  const suffix = String(++state.turn);
  const runId = `reconnect-run-${suffix}`;
  const planPayload = {
    plan_id: `${runId}-plan-1`,
    plan_version: 1,
    summary: "断线恢复分析",
    steps: [{
      step_id: "prepare",
      purpose: "完成可恢复分析",
      capability: "data.profile",
      dependencies: [],
      status: "pending",
    }],
  };
  const run = mockAgentRun(runId, prompt, "completed");
  run.events = [
    taskEvent(runId, 2, "plan.created", planPayload),
    taskEvent(runId, 3, "run.completed", { terminal_reason: null }),
  ];
  run.detail.state.last_sequence = 3;
  state.agentRuns[runId] = run;
  state.messages.push(
    message(`${runId}-user`, "user", prompt),
    message(`${runId}-assistant`, "assistant", "断线后已从持久事件恢复完成。"),
  );
  return {
    // 故意在 seq=2 后无 done 结束，客户端必须从只读重连端点继续，不能重放 POST。
    frames: [
      ["meta", { run_id: runId, conversation_id: "conversation-1" }],
      taskSse(runId, 2, "plan.created", planPayload),
    ],
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function appendTaskEvent(
  run: MockAgentRunState,
  runId: string,
  eventType: string,
  payload: Record<string, unknown>,
): Record<string, any> {
  const sequence = Number(run.detail.state.last_sequence ?? 0) + 1;
  const event = taskEvent(runId, sequence, eventType, payload);
  run.events.push(event);
  run.detail.state.last_sequence = sequence;
  return event;
}

function eventToSse(
  event: Record<string, any>,
): [string, Record<string, unknown>] {
  return [String(event.event_type), {
    ...event,
    conversation_id: "conversation-1",
  }];
}

async function fulfillTaskStream(
  route: Route,
  runId: string,
  frames: Array<[string, Record<string, unknown>]>,
): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream; charset=utf-8",
    headers: {
      "Cache-Control": "no-cache",
      "X-ChatBI-Run-ID": runId,
    },
    body: sse(frames),
  });
}

function persistChartTurn(state: MockWorkspaceState, prompt: string): {
  frames: Array<[string, Record<string, unknown>]>;
} {
  const suffix = String(++state.turn);
  const runId = `run-${suffix}`;
  const userId = `chart-user-${suffix}`;
  const toolMessageId = `chart-tool-message-${suffix}`;
  const callId = `chart-call-${suffix}`;
  const chart = artifact(
    `chart-artifact-${suffix}`,
    toolMessageId,
    "chart",
    {
      chart_id: `chart-${suffix}`,
      chart_type: "line",
      option: {
        title: { text: "月度销售趋势" },
        tooltip: { trigger: "axis" },
        xAxis: { type: "category", data: ["1月", "2月", "3月"] },
        yAxis: { type: "value" },
        series: [{ name: "销售额", type: "line", data: [100, 140, 180] }],
      },
    },
    "gen_chart",
  );
  state.messages.push(
    message(userId, "user", prompt),
    message(toolMessageId, "assistant", "我将按月份生成销售趋势图。", [
      {
        id: callId,
        name: "gen_chart",
        arguments: (
          '{"dataset_ref":"sales-ref","chart_type":"line",'
          + '"encoding":{"x":"月份","y":"销售额","agg":"sum"}}'
        ),
      },
    ]),
    message(
      `chart-outcome-${suffix}`,
      "tool",
      JSON.stringify({
        tool_call_id: callId,
        tool: "gen_chart",
        status: "ok",
        summary: "已生成 line 图表",
        fields: "数据集: sales-re · 图型: line · X轴: 月份 · Y轴: 销售额",
      }),
    ),
    message(`chart-final-${suffix}`, "assistant", "趋势图已生成。"),
  );
  state.artifacts.push(chart);
  state.agentRuns[runId] = mockAgentRun(
    runId,
    "生成月度销售趋势图",
    "completed",
  );

  return {
    frames: [
      ["meta", {
        conversation_id: "conversation-1",
        message_id: `chart-final-${suffix}`,
        user_message_id: userId,
        title: "月度销售趋势",
      }],
      ["understanding", { text: "我将按月份生成销售趋势图。" }],
      ["plan.created", {
        schema_version: "2.0",
        event_id: `plan-event-${suffix}`,
        run_id: runId,
        conversation_id: "conversation-1",
        sequence: 2,
        occurred_at: NOW,
        payload: {
          plan_id: `plan-${suffix}`,
          plan_version: 1,
          summary: "生成月度销售趋势图",
          steps: [{
            step_id: "monthly_chart",
            purpose: "生成月度销售趋势图",
            capability: "visualization.chart",
            dependencies: [],
            status: "pending",
          }],
        },
      }],
      ["tool_start", {
        id: callId,
        step_id: "monthly_chart",
        tool: "gen_chart",
        fields: "数据集: sales-re · 图型: line · X轴: 月份 · Y轴: 销售额",
        args_preview: "{}",
      }],
      ["artifact", chart],
      ["tool_end", {
        id: callId,
        step_id: "monthly_chart",
        tool: "gen_chart",
        status: "ok",
        summary: "已生成 line 图表",
      }],
      ["text.delta", { delta: "趋势图已生成。" }],
      ["done", {
        conversation_id: "conversation-1",
        message_id: `chart-final-${suffix}`,
        tool_calls: 1,
      }],
    ],
  };
}

function persistReportTurn(state: MockWorkspaceState, prompt: string): {
  frames: Array<[string, Record<string, unknown>]>;
} {
  const suffix = String(++state.turn);
  const userId = `report-user-${suffix}`;
  const toolMessageId = `report-tool-message-${suffix}`;
  const callId = `report-call-${suffix}`;
  const report = artifact(
    `report-artifact-${suffix}`,
    toolMessageId,
    "report",
    {
      report_id: "report-1",
      md_url: "/analyze/report/report-1.md",
      pdf_url: "/analyze/report/report-1.pdf",
      skipped_charts: 0,
    },
    "generate_report",
  );
  state.messages.push(
    message(userId, "user", prompt),
    message(toolMessageId, "assistant", "我将汇总分析并导出 PDF。", [
      {
        id: callId,
        name: "generate_report",
        arguments: (
          '{"title":"销售分析报告","analysis_ids":["chart-analysis"],'
          + '"include_pdf":true}'
        ),
      },
    ]),
    message(
      `report-outcome-${suffix}`,
      "tool",
      JSON.stringify({
        tool_call_id: callId,
        tool: "generate_report",
        status: "ok",
        summary: "报告已生成（report_id=report-1）",
        fields: "标题: 销售分析报告 · 导出PDF: 是",
      }),
    ),
    message(`report-final-${suffix}`, "assistant", "报告和 PDF 已生成。"),
  );
  state.artifacts.push(report);

  return {
    frames: [
      ["meta", {
        conversation_id: "conversation-1",
        message_id: `report-final-${suffix}`,
        user_message_id: userId,
        title: "销售分析报告",
      }],
      ["understanding", { text: "我将汇总分析并导出 PDF。" }],
      ["plan", {
        message_id: toolMessageId,
        steps: [{ id: callId, tool: "generate_report", label: "生成报告" }],
      }],
      ["tool_start", {
        id: callId,
        tool: "generate_report",
        fields: "标题: 销售分析报告 · 导出PDF: 是",
        args_preview: "{}",
      }],
      ["artifact", report],
      ["tool_end", {
        id: callId,
        tool: "generate_report",
        status: "ok",
        summary: "报告已生成（report_id=report-1）",
      }],
      ["text.delta", { delta: "报告和 PDF 已生成。" }],
      ["done", {
        conversation_id: "conversation-1",
        message_id: `report-final-${suffix}`,
        tool_calls: 1,
      }],
    ],
  };
}

function persistKnowledgeTurn(state: MockWorkspaceState, prompt: string): {
  frames: Array<[string, Record<string, unknown>]>;
} {
  const suffix = String(++state.turn);
  const userId = `kb-user-${suffix}`;
  const toolMessageId = `kb-tool-message-${suffix}`;
  const callId = `kb-call-${suffix}`;
  const citations = artifact(
    `kb-artifact-${suffix}`,
    toolMessageId,
    "citations",
    {
      is_empty: false,
      hits: [{
        source: "指标口径.md",
        section: "活跃用户",
        text: "活跃用户指统计周期内有效登录的去重用户数。",
      }],
    },
    "kb_search",
  );
  state.messages.push(
    message(userId, "user", prompt),
    message(toolMessageId, "assistant", "我先查询知识库中的指标口径。", [
      { id: callId, name: "kb_search", arguments: '{"query":"活跃用户怎么定义"}' },
    ]),
    message(
      `kb-outcome-${suffix}`,
      "tool",
      JSON.stringify({
        tool_call_id: callId,
        tool: "kb_search",
        status: "ok",
        summary: "命中 1 条片段",
        fields: "检索词: 活跃用户怎么定义",
      }),
    ),
    message(
      `kb-final-${suffix}`,
      "assistant",
      "活跃用户指统计周期内有效登录的去重用户数（来源：指标口径.md）。",
    ),
  );
  state.artifacts.push(citations);
  return {
    frames: [
      ["meta", {
        conversation_id: "conversation-1",
        message_id: `kb-final-${suffix}`,
        user_message_id: userId,
        title: "活跃用户口径",
      }],
      ["understanding", { text: "我先查询知识库中的指标口径。" }],
      ["plan", {
        message_id: toolMessageId,
        steps: [{ id: callId, tool: "kb_search", label: "知识库检索" }],
      }],
      ["tool_start", {
        id: callId,
        tool: "kb_search",
        fields: "检索词: 活跃用户怎么定义",
        args_preview: '{"query":"活跃用户怎么定义"}',
      }],
      ["artifact", citations],
      ["tool_end", {
        id: callId,
        tool: "kb_search",
        status: "ok",
        summary: "命中 1 条片段",
      }],
      ["text.delta", {
        delta: "活跃用户指统计周期内有效登录的去重用户数（来源：指标口径.md）。",
      }],
      ["done", {
        conversation_id: "conversation-1",
        message_id: `kb-final-${suffix}`,
        tool_calls: 1,
      }],
    ],
  };
}

async function installMockApi(
  page: Page,
  options: { withDataset?: boolean } = { withDataset: true },
): Promise<MockWorkspaceState> {
  const state: MockWorkspaceState = {
    project: { id: "project-1", name: "E2E 项目", created_at: NOW },
    conversation: {
      id: "conversation-1",
      project_id: "project-1",
      title: "E2E 验收对话",
      created_at: NOW,
      updated_at: NOW,
    },
    datasets: options.withDataset === false ? [] : [dataset()],
    messages: [],
    artifacts: [],
    kbDocuments: [],
    agentRuns: {},
    chatRequests: [],
    reconnectCursors: [],
    turn: 0,
  };

  await page.route(/^https?:\/\/[^/]+\/api\//, async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api/, "");

    if (method === "GET" && path === "/auth/config") {
      await json(route, { mode: "disabled" });
      return;
    }
    if (method === "GET" && path === "/projects") {
      await json(route, [state.project]);
      return;
    }
    if (method === "GET" && path === "/projects/project-1/conversations") {
      await json(route, [state.conversation]);
      return;
    }
    if (method === "GET" && path === "/projects/project-1/datasets") {
      await json(route, state.datasets);
      return;
    }
    if (method === "GET" && path === "/conversations/conversation-1") {
      await json(route, {
        conversation: state.conversation,
        messages: state.messages,
        artifacts: state.artifacts,
      });
      return;
    }
    if (method === "GET" && path === "/kb/overview") {
      await json(route, {
        chunk_count: state.kbDocuments.length * 2,
        sources: state.kbDocuments.map((item) => item.source),
        topics: state.kbDocuments.length > 0 ? ["指标口径"] : [],
        documents: state.kbDocuments,
      });
      return;
    }
    if (method === "POST" && path === "/kb/ingest") {
      state.kbDocuments = [{
        document_id: "metrics-doc",
        source: "metrics.md",
        content_hash: "abc123",
        version: 1,
        updated_at: NOW,
        chunk_count: 2,
      }];
      await json(route, {
        ingested_docs: 1,
        chunks: 2,
        total_chunks: 2,
        created: ["metrics.md"],
        updated: [],
        skipped: [],
        deleted: [],
      });
      return;
    }
    if (method === "POST" && path === "/kb/rebuild") {
      state.kbDocuments = state.kbDocuments.map((item) => ({ ...item, version: 2 }));
      await json(route, {
        ingested_docs: state.kbDocuments.length,
        chunks: state.kbDocuments.length * 2,
        total_chunks: state.kbDocuments.length * 2,
        created: [],
        updated: [],
        skipped: state.kbDocuments.map((item) => item.source),
        deleted: [],
      });
      return;
    }
    if (method === "DELETE" && path === "/kb/documents/metrics-doc") {
      state.kbDocuments = [];
      await json(route, { document_id: "metrics-doc", removed_chunks: 2 });
      return;
    }
    if (method === "POST" && path === "/upload/excel") {
      const uploaded = dataset("uploaded-ref", "uploaded-sales.xlsx");
      const uploadMessage = message("upload-assistant", "assistant", "数据画像已生成。\n");
      const uploadArtifact = artifact(
        "upload-profile",
        "upload-assistant",
        "profile",
        profile("uploaded-ref"),
        "infer_schema",
      );
      uploadArtifact.dataset_ref = "uploaded-ref";
      state.datasets.push(uploaded);
      state.messages.push(
        message("upload-user", "user", "上传了文件：uploaded-sales.xlsx"),
        uploadMessage,
      );
      state.artifacts.push(uploadArtifact);
      await json(route, {
        dataset_ref: "uploaded-ref",
        profile: profile("uploaded-ref"),
        messages: [uploadMessage],
        artifact: uploadArtifact,
      });
      return;
    }
    const runDetailMatch = path.match(/^\/agent\/runs\/([^/]+)$/);
    if (method === "GET" && runDetailMatch) {
      refreshRelatedRuns(state);
      const run = state.agentRuns[decodeURIComponent(runDetailMatch[1])];
      await json(route, run?.detail ?? { detail: "TaskRun 不存在" }, run ? 200 : 404);
      return;
    }
    const runFeedbackMatch = path.match(/^\/agent\/runs\/([^/]+)\/feedback$/);
    if (method === "POST" && runFeedbackMatch) {
      const runId = decodeURIComponent(runFeedbackMatch[1]);
      const run = state.agentRuns[runId];
      if (!run) {
        await json(route, { detail: "TaskRun 不存在" }, 404);
        return;
      }
      const expectedRunVersion = Number(request.headers()["if-match"]);
      if (expectedRunVersion !== run.detail.run.state_version) {
        await json(route, { detail: "TaskRun 状态版本冲突" }, 409);
        return;
      }
      const body = request.postDataJSON() as {
        rating: "helpful" | "not_helpful";
        comment: string | null;
        evidence_ids: string[];
        artifact_ids: string[];
      };
      run.detail.run.state_version += 1;
      run.detail.run.updated_at = NOW;
      const event = appendTaskEvent(run, runId, "user.feedback", {
        feedback_id: `${runId}-feedback-${run.detail.feedback.length + 1}`,
        subject_user_id: "e2e-user",
        ...body,
      });
      const feedback = {
        feedback_id: event.payload.feedback_id,
        event_id: event.event_id,
        sequence: event.sequence,
        rating: body.rating,
        comment: body.comment,
        evidence_ids: body.evidence_ids,
        artifact_ids: body.artifact_ids,
        created_at: NOW,
      };
      run.detail.feedback.push(feedback);
      await json(route, {
        run: run.detail.run,
        event,
        feedback,
        replayed: false,
      });
      return;
    }
    const latestRunMatch = path.match(
      /^\/agent\/runs\/by-conversation\/([^/]+)\/latest$/,
    );
    if (method === "GET" && latestRunMatch) {
      const latest = Object.values(state.agentRuns).at(-1);
      await json(route, { run: latest?.detail.run ?? null });
      return;
    }
    const runApprovalsMatch = path.match(/^\/agent\/runs\/([^/]+)\/approvals$/);
    if (method === "GET" && runApprovalsMatch) {
      const run = state.agentRuns[decodeURIComponent(runApprovalsMatch[1])];
      await json(route, run?.approvals ?? []);
      return;
    }
    const runEventsMatch = path.match(/^\/agent\/runs\/([^/]+)\/events$/);
    if (method === "GET" && runEventsMatch) {
      const runId = decodeURIComponent(runEventsMatch[1]);
      const run = state.agentRuns[runId];
      await json(route, {
        run_id: runId,
        events: run?.events ?? [],
        last_sequence: Number(run?.detail.state.last_sequence ?? 0),
      });
      return;
    }
    const approvalDecisionMatch = path.match(
      /^\/agent\/runs\/([^/]+)\/approvals\/([^/]+)\/decision$/,
    );
    if (method === "POST" && approvalDecisionMatch) {
      const runId = decodeURIComponent(approvalDecisionMatch[1]);
      const approvalId = decodeURIComponent(approvalDecisionMatch[2]);
      const run = state.agentRuns[runId];
      if (!run) {
        await json(route, { detail: "TaskRun 不存在" }, 404);
        return;
      }
      const expectedRunVersion = Number(request.headers()["if-match"]);
      if (expectedRunVersion !== run.detail.run.state_version) {
        await json(route, { detail: "TaskRun 状态版本冲突" }, 409);
        return;
      }
      const body = request.postDataJSON() as {
        expected_version: number;
        decision: "approved" | "denied";
        reason: string;
      };
      const approval = run.approvals.find(
        (item) => item.approval_id === approvalId,
      );
      if (!approval || approval.version !== body.expected_version) {
        await json(route, { detail: "ApprovalRecord 版本冲突" }, 409);
        return;
      }
      approval.status = body.decision;
      approval.version += 1;
      approval.decision_reason = body.reason;
      approval.decided_at = NOW;
      approval.updated_at = NOW;
      run.detail.run.state_version += 1;
      run.detail.run.updated_at = NOW;
      const event = appendTaskEvent(run, runId, "approval.decided", {
        approval_id: approvalId,
        decision: body.decision,
        approval_version: approval.version,
      });
      await json(route, {
        run: run.detail.run,
        approval,
        event,
        replayed: false,
      });
      return;
    }
    const planRevisionMatch = path.match(
      /^\/agent\/runs\/([^/]+)\/plan\/revisions$/,
    );
    if (method === "POST" && planRevisionMatch) {
      const runId = decodeURIComponent(planRevisionMatch[1]);
      const run = state.agentRuns[runId];
      if (!run) {
        await json(route, { detail: "TaskRun 不存在" }, 404);
        return;
      }
      const expectedRunVersion = Number(request.headers()["if-match"]);
      if (expectedRunVersion !== run.detail.run.state_version) {
        await json(route, { detail: "TaskRun 状态版本冲突" }, 409);
        return;
      }
      const body = request.postDataJSON() as {
        plan: Record<string, any>;
        reason: string;
        skipped_step_ids: string[];
      };
      const nextPlanVersion = run.detail.run.plan_version + 1;
      run.detail.run.plan_version = nextPlanVersion;
      run.detail.run.state_version += 1;
      run.detail.run.updated_at = NOW;
      run.detail.plan = {
        plan_id: `${runId}-plan-${nextPlanVersion}`,
        version: nextPlanVersion,
        reason: body.reason,
        definition: body.plan,
        created_at: NOW,
      };
      run.detail.steps = body.plan.steps.map(
        (definition: Record<string, any>, position: number) => ({
          step_id: definition.step_id,
          persisted_step_id: `${runId}-step-${nextPlanVersion}-${position + 1}`,
          position,
          status: body.skipped_step_ids.includes(definition.step_id)
            ? "skipped"
            : "pending",
          definition,
          started_at: null,
          completed_at: body.skipped_step_ids.includes(definition.step_id)
            ? NOW
            : null,
        }),
      );
      const event = appendTaskEvent(run, runId, "plan.revised", {
        plan_id: run.detail.plan.plan_id,
        plan_version: nextPlanVersion,
        reason: body.reason,
      });
      await json(route, {
        run: run.detail.run,
        plan: run.detail.plan,
        steps: run.detail.steps,
        event,
        replayed: false,
      });
      return;
    }
    const clarificationMatch = path.match(
      /^\/agent\/runs\/([^/]+)\/clarifications\/([^/]+)\/answer\/stream$/,
    );
    if (method === "POST" && clarificationMatch) {
      const runId = decodeURIComponent(clarificationMatch[1]);
      const questionId = decodeURIComponent(clarificationMatch[2]);
      const run = state.agentRuns[runId];
      if (!run) {
        await json(route, { detail: "TaskRun 不存在" }, 404);
        return;
      }
      const expectedRunVersion = Number(request.headers()["if-match"]);
      if (expectedRunVersion !== run.detail.run.state_version) {
        await json(route, { detail: "TaskRun 状态版本冲突" }, 409);
        return;
      }
      const body = request.postDataJSON() as {
        answer: unknown;
        resume_token: string;
      };
      if (body.resume_token !== "resume-token-metric-0001") {
        await json(route, { detail: "恢复令牌无效" }, 409);
        return;
      }
      run.detail.run.status = "completed";
      run.detail.run.state_version += 1;
      run.detail.run.finished_at = NOW;
      run.detail.run.updated_at = NOW;
      run.detail.steps[0].status = "completed";
      run.detail.steps[0].completed_at = NOW;
      const answered = appendTaskEvent(run, runId, "clarification.answered", {
        question_id: questionId,
        answer: body.answer,
      });
      const completed = appendTaskEvent(run, runId, "run.completed", {
        terminal_reason: null,
      });
      state.messages.push(
        message(`${runId}-answer`, "user", String(body.answer)),
        message(`${runId}-completed`, "assistant", "已按所选指标完成任务。"),
      );
      await fulfillTaskStream(route, runId, [
        eventToSse(answered),
        eventToSse(completed),
        ["text.delta", { delta: "已按所选指标完成任务。" }],
        ["done", {
          run_id: runId,
          conversation_id: "conversation-1",
          run_status: "completed",
          last_sequence: completed.sequence,
        }],
      ]);
      return;
    }
    const resumeMatch = path.match(/^\/agent\/runs\/([^/]+)\/resume\/stream$/);
    if (method === "POST" && resumeMatch) {
      const runId = decodeURIComponent(resumeMatch[1]);
      const run = state.agentRuns[runId];
      if (!run) {
        await json(route, { detail: "TaskRun 不存在" }, 404);
        return;
      }
      const expectedRunVersion = Number(request.headers()["if-match"]);
      if (expectedRunVersion !== run.detail.run.state_version) {
        await json(route, { detail: "TaskRun 状态版本冲突" }, 409);
        return;
      }
      if (run.approvals.some((approval) => approval.status === "pending")) {
        await json(route, { detail: "仍有待决定授权" }, 409);
        return;
      }
      run.detail.run.status = "completed";
      run.detail.run.state_version += 1;
      run.detail.run.finished_at = NOW;
      run.detail.run.updated_at = NOW;
      run.detail.steps.forEach((step) => {
        if (step.status === "pending") {
          step.status = "completed";
          step.completed_at = NOW;
        }
      });
      const resumed = appendTaskEvent(run, runId, "run.resumed", {});
      const streamEvents: Array<[string, Record<string, unknown>]> = [
        eventToSse(resumed),
      ];
      for (const approval of run.approvals) {
        if (approval.status !== "approved") continue;
        approval.status = "consumed";
        approval.version += 1;
        approval.consumed_at = NOW;
        approval.updated_at = NOW;
        streamEvents.push(eventToSse(appendTaskEvent(
          run,
          runId,
          "approval.consumed",
          { approval_id: approval.approval_id },
        )));
      }
      const completed = appendTaskEvent(run, runId, "run.completed", {
        terminal_reason: null,
      });
      streamEvents.push(
        eventToSse(completed),
        ["done", {
          run_id: runId,
          conversation_id: "conversation-1",
          run_status: "completed",
          last_sequence: completed.sequence,
        }],
      );
      state.messages.push(
        message(`${runId}-completed`, "assistant", "任务已在授权边界内完成。"),
      );
      await fulfillTaskStream(route, runId, streamEvents);
      return;
    }
    const reconnectMatch = path.match(/^\/agent\/runs\/([^/]+)\/stream$/);
    if (method === "GET" && reconnectMatch) {
      const runId = decodeURIComponent(reconnectMatch[1]);
      const run = state.agentRuns[runId];
      if (!run) {
        await json(route, { detail: "TaskRun 不存在" }, 404);
        return;
      }
      const cursor = request.headers()["last-event-id"] ?? "";
      state.reconnectCursors.push(cursor);
      const completed = run.events.find(
        (event) => event.event_type === "run.completed",
      );
      // 包含一个边界重复事件，验证浏览器自身也按 sequence/event_id 抑制重复。
      await fulfillTaskStream(route, runId, [
        eventToSse(run.events[0]),
        ...(completed ? [eventToSse(completed)] : []),
        ["done", {
          run_id: runId,
          conversation_id: "conversation-1",
          run_status: run.detail.run.status,
          last_sequence: Number(run.detail.state.last_sequence),
        }],
      ]);
      return;
    }
    if (method === "POST" && path === "/chat/stream") {
      const body = request.postDataJSON() as {
        message?: string;
        autonomy_mode?: string;
        parent_run_id?: string | null;
      };
      const prompt = body.message ?? "";
      const autonomyMode = body.autonomy_mode ?? "autonomous";
      const parentRunId = body.parent_run_id ?? null;
      state.chatRequests.push({
        message: prompt,
        autonomy_mode: autonomyMode,
        parent_run_id: parentRunId,
      });
      const turn = autonomyMode === "assisted" && parentRunId === null
        ? persistCollaborationTurn(state, prompt)
        : prompt.includes("断线恢复")
        ? persistReconnectTurn(state, prompt)
        : (
        prompt.includes("高风险")
        || prompt.includes("修改计划")
        || prompt.includes("选择指标")
      )
        ? persistCollaborationTurn(state, prompt)
        : prompt.includes("报告")
        ? persistReportTurn(state, prompt)
        : prompt.includes("定义") || prompt.includes("口径")
          ? persistKnowledgeTurn(state, prompt)
          : persistChartTurn(state, prompt);
      const runId = turn.frames
        .map(([, data]) => data.run_id)
        .find((value): value is string => typeof value === "string");
      const run = runId ? state.agentRuns[runId] : undefined;
      if (run) {
        run.detail.run.parent_run_id = parentRunId;
        run.detail.run.autonomy_mode = autonomyMode;
        run.detail.run.budget.autonomy_mode = autonomyMode;
        refreshRelatedRuns(state);
      }
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream; charset=utf-8",
        headers: {
          "Cache-Control": "no-cache",
          ...(runId ? { "X-ChatBI-Run-ID": runId } : {}),
        },
        body: sse(turn.frames),
      });
      return;
    }
    if (method === "GET" && path === "/analyze/report/report-1.pdf") {
      await route.fulfill({
        status: 200,
        contentType: "application/pdf",
        headers: { "Content-Disposition": 'attachment; filename="report-1.pdf"' },
        body: "%PDF-1.4\n% ChatBI E2E fixture\n",
      });
      return;
    }
    if (method === "GET" && path === "/analyze/report/report-1.md") {
      await route.fulfill({
        status: 200,
        contentType: "text/markdown; charset=utf-8",
        body: "# 销售分析报告\n",
      });
      return;
    }

    await json(route, { detail: `E2E mock 未实现: ${method} ${path}` }, 501);
  });

  return state;
}

async function send(page: Page, prompt: string): Promise<void> {
  const input = page.getByRole("textbox", { name: "消息内容" });
  await input.fill(prompt);
  await page.getByRole("button", { name: "发送消息" }).click();
}

test("上传 Excel 后渲染画像卡和数据集", async ({ page }) => {
  await installMockApi(page, { withDataset: false });
  await page.goto("/");
  await expect(page.getByRole("textbox", { name: "消息内容" })).toBeEnabled();

  await page.locator('input[type="file"]').setInputFiles({
    name: "uploaded-sales.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from("ChatBI E2E fixture"),
  });

  await expect(page.locator(".profile-artifact")).toContainText("数据画像");
  await expect(page.locator(".profile-artifact")).toContainText("24");
  await expect(page.locator(".dataset-item__main")).toContainText("uploaded-sales.xlsx");
});

test("角色与质量建议 Artifact 展示置信边界且不宣称自动清洗", async ({ page }) => {
  const state = await installMockApi(page);
  const assistant = message("profile-advice-message", "assistant", "角色和质量诊断已完成。");
  state.messages.push(assistant);
  state.artifacts.push(artifact(
    "profile-advice",
    "profile-advice-message",
    "profile",
    {
      profile: profile("sales-ref"),
      roles: {
        summary: { time: 1, metric: 1, dimension: 0, identifier: 0, ambiguous: 1 },
        columns: [
          { column: "月份", primary_role: "time", confidence: 0.9, ambiguous: false },
          { column: "销售额", primary_role: "metric", confidence: 0.95, ambiguous: false },
        ],
      },
      quality: {
        duplicate_rows: 0,
        high_null_columns: [{ name: "销售额", null_ratio: 0.3 }],
        constant_columns: [],
        summary: { issue_count: 1 },
        issues: [{
          issue_id: "quality-001",
          code: "missing_values",
          recommendation: "先确认缺失机制，再选择删除或受治理填补；不得静默用 0 替代。",
        }],
      },
    },
    "get_data_profile",
  ));

  await page.goto("/");

  const card = page.locator(".profile-artifact");
  await expect(card).toContainText("时间 1");
  await expect(card).toContainText("指标 1");
  await expect(card).toContainText("仅建议，不自动修改数据");
  await expect(card).toContainText("不得静默用 0 替代");
});

test("Chart Artifact 在生成后及刷新后都可见", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "请按月份生成销售额折线图");

  const chart = page.locator('[data-chart-id="chart-1"]');
  await expect(chart).toBeVisible();
  await expect(chart.locator("canvas")).toBeVisible();
  await expect(page.getByText("趋势图已生成。", { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.locator('[data-chart-id="chart-1"] canvas')).toBeVisible();
});

test("Report Artifact 渲染 PDF 下载入口并可在刷新后恢复", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "请把本次分析生成报告并导出 PDF");

  const report = page.locator(".report-artifact");
  await expect(report).toContainText("分析报告");
  const pdfButton = report.getByRole("button", { name: "下载 PDF" });
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/analyze/report/report-1.pdf"),
  );
  await pdfButton.click();
  const pdfResponse = await responsePromise;
  expect(pdfResponse.headers()["content-type"]).toContain("application/pdf");
  expect(pdfResponse.headers()["content-disposition"]).toContain(
    'filename="report-1.pdf"',
  );

  await page.reload();
  await expect(page.locator(".report-artifact")).toBeVisible();
  await expect(page.getByRole("button", { name: "下载 PDF" })).toBeVisible();
});

test("知识库支持同步、全量重建和按来源删除", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "同步样例" }).click();
  await expect(page.getByText("metrics.md", { exact: true })).toBeVisible();
  await expect(page.locator(".context-knowledge__notice")).toContainText("新增 1");

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "全量重建" }).click();
  await expect(page.locator(".context-knowledge__notice")).toContainText("重建完成");

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除 metrics.md" }).click();
  await expect(page.getByText("知识库为空，可先摄入样例文档。")).toBeVisible();
  await expect(page.locator(".context-knowledge__notice")).toContainText("已删除 metrics.md");
});

test("知识库问答生成并持久化来源引用卡", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "活跃用户怎么定义？");

  const citation = page.locator(".citation-artifact");
  await expect(citation).toContainText("知识库来源");
  await expect(citation).toContainText("指标口径.md");
  await expect(citation).toContainText("有效登录的去重用户数");
  await expect(page.getByText(/来源：指标口径\.md/)).toBeVisible();

  await page.reload();
  await expect(page.locator(".citation-artifact")).toContainText("指标口径.md");
});

test("高风险审批只记录决定，用户显式继续后才执行", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "请执行高风险导出");
  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();

  const panel = page.getByRole("dialog", { name: "任务协作" });
  await expect(panel).toContainText("high_risk_export");
  await expect(panel.getByRole("button", { name: "继续执行" })).toBeDisabled();

  await panel.getByLabel("授权原因 high_risk_export").fill("仅允许本轮导出");
  await panel.getByRole("button", { name: "批准一次" }).click();

  await expect(panel).toContainText("显式点击“继续执行”");
  await expect(panel).toContainText("已批准");
  await expect(panel.getByRole("button", { name: "继续执行" })).toBeEnabled();
  await panel.getByRole("button", { name: "继续执行" }).click();

  await expect(panel.locator(".agent-status")).toHaveText("已完成");
  await expect(panel.getByRole("region", { name: "任务执行边界" })).toContainText(
    "并行上限",
  );
  await expect(panel).toContainText("已消费");
});

test("暂停态可以提交不可变计划新版本", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "请暂停并修改计划");
  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();

  const panel = page.getByRole("dialog", { name: "任务协作" });
  await panel.getByRole("button", { name: "修改计划" }).click();
  await panel.getByLabel("计划摘要").fill("先核对范围，再生成结果");
  await panel.getByLabel("步骤目的").fill("核对销售数据范围与统计口径");
  await panel.getByLabel("修改原因").fill("用户要求先确认统计口径");
  await panel.getByRole("button", { name: "保存新版本" }).click();

  await expect(panel).toContainText("计划已保存为不可变新版本");
  await expect(panel).toContainText("先核对范围，再生成结果");
  await expect(panel).toContainText("计划 v2");
  await expect(panel.locator(".agent-status")).toHaveText("已暂停");
});

test("阻塞澄清提交答案后继续同一个 TaskRun", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "请让我选择指标");
  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();

  const panel = page.getByRole("dialog", { name: "任务协作" });
  await expect(panel).toContainText("请选择本次分析指标");
  await panel.getByRole("textbox", { name: "澄清答案" }).fill("销售额");
  await panel.getByRole("button", { name: "提交答案并继续" }).click();

  await expect(panel.locator(".agent-status")).toHaveText("已完成");
  await expect(page.getByText("已按所选指标完成任务。")).toBeVisible();
});

test("6C 候选假设验证链展示计划、Evidence 与 Verifier 终态", async ({ page }) => {
  const state = await installMockApi(page);
  await page.goto("/");

  await send(page, "请生成趋势图");
  await expect(page.getByText("趋势图已生成。", { exact: true })).toBeVisible();
  const active = Object.values(state.agentRuns).at(-1);
  expect(active).toBeTruthy();
  active!.detail.hypothesis_execution = {
    schema: "chatbi-hypothesis-execution-v1",
    schema_version: 1,
    hypothesis_id: "hyp_0123456789abcdef",
    kind: "trend",
    statement: "销售额可能随月份呈现时间变化，需要趋势 Evidence 验证。",
    capability: "stats.trend",
    dataset_ref: "sales-ref",
    data_version_hash: "a".repeat(64),
    selection_plan_version: 1,
    execution_plan_id: "run-1-plan-1",
    execution_plan_version: 2,
    logical_step_id: "verify-selected-trend",
    persisted_step_id: "run-1-step-1",
    status: "not_supported",
    tested: true,
    evidence_outcome: "not_supported",
    outcome: "not_supported",
    invocation_ids: ["invocation-1"],
    failed_invocation_ids: [],
    evidence_ids: ["evidence-1"],
    evidence_ledger_sequences: [1],
    verification: {
      verdict: "PASS",
      check_codes: [],
      event_sequence: 9,
    },
    last_failure_code: null,
    updated_at: NOW,
  };
  active!.detail.hypothesis_followup = {
    schema: "chatbi-hypothesis-followup-v1",
    schema_version: 1,
    hypothesis_id: "hyp_0123456789abcdef",
    data_version_hash: "a".repeat(64),
    source_status: "not_supported",
    source_outcome: "not_supported",
    decision: "propose_next",
    reason_codes: ["hypothesis_not_supported", "next_eligible_candidate"],
    automatic_execution: false,
    requires_user_confirmation: true,
    proposed_candidate: {
      hypothesis_id: "hyp_fedcba9876543210",
      kind: "anomaly",
      statement: "销售额可能存在异常点，需要异常 Evidence 验证。",
      capability: "stats.anomaly",
      expected_evidence: "异常点数量与位置",
      priority: 2,
    },
    suggested_goal: "验证有界候选：销售额可能存在异常点，需要异常 Evidence 验证。",
    limits: {
      tool_attempts_used: 1,
      max_tool_calls: 12,
      tool_calls_remaining: 11,
      replans_used: 0,
      max_replans: 3,
      replans_remaining: 3,
      cancellation_root_status: "completed",
    },
    updated_at: NOW,
  };

  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();
  const panel = page.getByRole("dialog", { name: "任务协作" });
  const lifecycle = panel.getByRole("region", { name: "候选假设验证状态" });
  await expect(lifecycle).toContainText("假设验证链");
  await expect(lifecycle).toContainText("Evidence 未支持");
  await expect(lifecycle).toContainText("未支持候选");
  await expect(lifecycle).toContainText("v2 · verify-selected-trend");
  await expect(lifecycle).toContainText("PASS");
  const followup = panel.getByRole("region", { name: "结果驱动跟进" });
  await expect(followup).toContainText("建议下一候选");
  await expect(followup).toContainText("销售额可能存在异常点");
  await expect(followup).toContainText("自动执行");
  await followup.getByRole("button", { name: "填入新分支目标" }).click();
  await expect(panel.getByRole("textbox", { name: "新分支目标" })).toHaveValue(
    "验证有界候选：销售额可能存在异常点，需要异常 Evidence 验证。",
  );
  await expect(panel).toContainText("不会自动调用工具");
});

test("清空浏览器 Run 缓存后仍从服务端恢复最近 TaskRun", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await send(page, "请暂停并修改计划");
  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();
  await expect(page.getByRole("dialog", { name: "任务协作" })).toContainText(
    "请暂停并修改计划",
  );
  await page.getByRole("button", { name: "关闭任务协作" }).click();

  await page.evaluate(() => {
    for (const key of Object.keys(window.sessionStorage)) {
      if (key.startsWith("chatbi.agentRun.")) window.sessionStorage.removeItem(key);
    }
  });
  await page.reload();

  await expect(controlButton).toBeEnabled();
  await controlButton.click();
  const restored = page.getByRole("dialog", { name: "任务协作" });
  await expect(restored.locator(".agent-status")).toHaveText("已暂停");
  await expect(restored).toContainText("请暂停并修改计划");
});

test("SSE 中断后携带游标续接并抑制边界重复事件", async ({ page }) => {
  const state = await installMockApi(page);
  await page.goto("/");

  await send(page, "请验证断线恢复");

  await expect(page.getByText("断线后已从持久事件恢复完成。", { exact: true })).toBeVisible();
  await expect.poll(() => state.reconnectCursors).toEqual(["reconnect-run-1:2"]);
  await expect(
    page.getByText("断线后已从持久事件恢复完成。", { exact: true }),
  ).toHaveCount(1);
  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();
  await expect(
    page.getByRole("dialog", { name: "任务协作" }).locator(".agent-status"),
  ).toHaveText("已完成");
});

test("4D 自主等级、反馈和分析分支形成可追踪闭环", async ({ page }) => {
  const state = await installMockApi(page);
  await page.goto("/");

  await page.getByRole("radio", { name: "辅助模式" }).click();
  await send(page, "请执行辅助模式验收");

  const controlButton = page.getByRole("button", { name: "任务协作" });
  await controlButton.click();
  let panel = page.getByRole("dialog", { name: "任务协作" });
  await expect(panel.locator(".agent-status")).toHaveText("已暂停");
  await expect(panel).toContainText("辅助模式");
  await panel.getByRole("button", { name: "确认计划并执行" }).click();
  await expect(panel.locator(".agent-status")).toHaveText("已完成");

  await panel.getByRole("radio", { name: "需改进" }).click();
  await panel.getByRole("textbox", { name: "反馈说明" }).fill("请改用不同假设并比较结果");
  await panel.getByRole("button", { name: "提交反馈" }).click();
  await expect(panel).toContainText("请改用不同假设并比较结果");
  await expect(panel.getByLabel("结果反馈")).toContainText("1 条");

  await page.getByRole("button", { name: "关闭任务协作" }).click();
  await page.getByRole("radio", { name: "自主模式" }).click();
  await controlButton.click();
  panel = page.getByRole("dialog", { name: "任务协作" });
  await panel.getByRole("textbox", { name: "新分支目标" }).fill(
    "改用稳健趋势方法并与原结果对比",
  );
  await panel.getByRole("button", { name: "创建分析分支" }).click();

  await expect(page.getByText("趋势图已生成。", { exact: true })).toBeVisible();
  await controlButton.click();
  panel = page.getByRole("dialog", { name: "任务协作" });
  await expect(panel.getByLabel("分支对比")).toContainText("2 个相关 Run");
  await expect(panel.getByLabel("分支对比")).toContainText("自主模式");
  await panel.getByRole("button", { name: /查看分支/ }).click();
  await expect(panel).toContainText("请执行辅助模式验收");
  await expect(panel.getByLabel("结果反馈")).toContainText(
    "请改用不同假设并比较结果",
  );
  expect(state.chatRequests).toEqual([
    {
      message: "请执行辅助模式验收",
      autonomy_mode: "assisted",
      parent_run_id: null,
    },
    {
      message: "改用稳健趋势方法并与原结果对比",
      autonomy_mode: "autonomous",
      parent_run_id: "collaboration-run-1",
    },
  ]);
});
