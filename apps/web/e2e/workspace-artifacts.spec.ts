import { expect, test, type Page, type Route } from "@playwright/test";

const NOW = "2026-07-17T08:00:00Z";

interface MockWorkspaceState {
  project: Record<string, unknown>;
  conversation: Record<string, unknown>;
  datasets: Array<Record<string, unknown>>;
  messages: Array<Record<string, unknown>>;
  artifacts: Array<Record<string, unknown>>;
  kbDocuments: Array<Record<string, unknown>>;
  turn: number;
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

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function persistChartTurn(state: MockWorkspaceState, prompt: string): {
  frames: Array<[string, Record<string, unknown>]>;
} {
  const suffix = String(++state.turn);
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

  return {
    frames: [
      ["meta", {
        conversation_id: "conversation-1",
        message_id: `chart-final-${suffix}`,
        user_message_id: userId,
        title: "月度销售趋势",
      }],
      ["understanding", { text: "我将按月份生成销售趋势图。" }],
      ["plan", {
        message_id: toolMessageId,
        steps: [{ id: callId, tool: "gen_chart", label: "生成图表" }],
      }],
      ["tool_start", {
        id: callId,
        tool: "gen_chart",
        fields: "数据集: sales-re · 图型: line · X轴: 月份 · Y轴: 销售额",
        args_preview: "{}",
      }],
      ["artifact", chart],
      ["tool_end", {
        id: callId,
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
    turn: 0,
  };

  await page.route(/^https?:\/\/[^/]+\/api\//, async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api/, "");

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
    if (method === "POST" && path === "/chat/stream") {
      const body = request.postDataJSON() as { message?: string };
      const prompt = body.message ?? "";
      const turn = prompt.includes("报告")
        ? persistReportTurn(state, prompt)
        : prompt.includes("定义") || prompt.includes("口径")
          ? persistKnowledgeTurn(state, prompt)
          : persistChartTurn(state, prompt);
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream; charset=utf-8",
        headers: { "Cache-Control": "no-cache" },
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
  const pdfLink = report.getByRole("link", { name: "下载 PDF" });
  await expect(pdfLink).toHaveAttribute("href", "/api/analyze/report/report-1.pdf");
  await expect(pdfLink).toHaveAttribute("target", "_blank");
  const pdfResponse = await page.evaluate(async (href) => {
    const response = await fetch(href);
    return {
      contentType: response.headers.get("content-type"),
      disposition: response.headers.get("content-disposition"),
    };
  }, await pdfLink.getAttribute("href"));
  expect(pdfResponse.contentType).toContain("application/pdf");
  expect(pdfResponse.disposition).toContain('filename="report-1.pdf"');

  await page.reload();
  await expect(page.locator(".report-artifact")).toBeVisible();
  await expect(page.getByRole("link", { name: "下载 PDF" })).toBeVisible();
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
