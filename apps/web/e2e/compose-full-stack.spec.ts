import fs from "node:fs/promises";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const E2E_TOKEN = "chatbi-local-e2e-token-00000001";
const AUTH_HEADERS = { Authorization: `Bearer ${E2E_TOKEN}` };
const FEEDBACK_COMMENT = (
  "COMPOSE_4D_FEEDBACK：请保留原始数据，只重新核对字段和规模。"
);
const BRANCH_GOAL = (
  "COMPOSE_4D_BRANCH：请深入分析这份数据的字段与规模，并根据父分支反馈调整计划。"
);
const READ_ONLY_GOAL = (
  "COMPOSE_4D_READ_ONLY：请基于已有数据画像重新生成一份报告并导出 PDF。"
);
const RUN_NOT_VISIBLE = "run_not_visible";

interface RunDetailPayload {
  run: {
    run_id: string;
    conversation_id: string;
    parent_run_id: string | null;
    autonomy_mode: "assisted" | "read_only" | "autonomous";
    status: string;
  };
  plan: { definition: { steps: Array<{ capability: string }> } } | null;
  tool_audits: Array<{
    tool_name: string;
    status: string;
    evidence_id: string | null;
    artifact_id: string | null;
  }>;
  feedback: Array<{ rating: string; comment: string | null }>;
}

interface RunStatusResponse {
  status(): number;
  ok(): boolean;
  json(): Promise<unknown>;
}

async function getRunDetail(page: Page, runId: string): Promise<RunDetailPayload> {
  const response = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(runId)}`,
    { headers: AUTH_HEADERS },
  );
  expect(response.ok()).toBeTruthy();
  return response.json() as Promise<RunDetailPayload>;
}

async function getRunStatusForPolling(page: Page, runId: string): Promise<string> {
  const response = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(runId)}`,
    { headers: AUTH_HEADERS },
  );
  return runStatusFromResponse(response);
}

async function runStatusFromResponse(response: RunStatusResponse): Promise<string> {
  // Streaming response headers expose the run ID before the worker has necessarily
  // committed the first run row. Treat only that short 404 window as retryable.
  if (response.status() === 404) {
    return RUN_NOT_VISIBLE;
  }
  expect(response.ok()).toBeTruthy();
  const detail = await response.json() as RunDetailPayload;
  return detail.run.status;
}

test.skip(
  process.env.CHATBI_COMPOSE_RECOVERY_ONLY === "1",
  "恢复阶段复用初始阶段创建的持久化工作区",
);

test("Run 状态轮询仅重试持久化可见性窗口", async () => {
  const responses: RunStatusResponse[] = [
    {
      status: () => 404,
      ok: () => false,
      json: async () => ({ detail: "TaskRun 不存在" }),
    },
    {
      status: () => 200,
      ok: () => true,
      json: async () => ({ run: { status: "blocked" } }),
    },
  ];

  await expect.poll(async () => runStatusFromResponse(responses.shift()!)).toBe("blocked");
  expect(responses).toHaveLength(0);
  await expect(runStatusFromResponse({
    status: () => 500,
    ok: () => false,
    json: async () => ({ detail: "unexpected" }),
  })).rejects.toThrow();
});

test("Compose 完成上传、计划、MCP、Evidence、报告与 PDF 下载", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("访问令牌").fill(E2E_TOKEN);
  await page.getByRole("button", { name: "进入工作区" }).click();
  await expect(page.getByRole("button", { name: "我的分析项目" })).toBeVisible();

  const uploadButton = page.getByRole("button", { name: "上传 Excel", exact: true });
  await expect(uploadButton).toBeEnabled();
  const fixture = path.resolve("../../.data/e2e/sales.xlsx");
  const fileChooserPromise = page.waitForEvent("filechooser");
  await uploadButton.click();
  const fileChooser = await fileChooserPromise;
  const uploadResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/upload/excel")
      && response.request().method() === "POST",
  );
  await fileChooser.setFiles(fixture);
  const uploadResponse = await uploadResponsePromise;
  expect(uploadResponse.ok()).toBeTruthy();
  await expect(page.getByText("已完成“sales.xlsx”的数据画像，共 3 行、3 列。")).toBeVisible({
    timeout: 30_000,
  });

  await page.getByRole("radio", { name: "自主模式" }).click();
  await page.getByLabel("消息内容").fill(
    "请把本次对话已完成的数据画像组装成一份报告，附要点解读，并导出 PDF。",
  );
  const streamResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/chat/stream")
      && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "发送消息" }).click();
  const streamResponse = await streamResponsePromise;
  expect(streamResponse.ok()).toBeTruthy();
  const runId = await streamResponse.headerValue("x-chatbi-run-id");
  expect(runId).toBeTruthy();
  await expect(page.getByText("报告和 PDF 已基于本对话的已验证数据画像生成。")).toBeVisible({
    timeout: 120_000,
  });
  const reportArtifact = page.locator(".report-artifact");
  await expect(
    reportArtifact.getByText("分析报告", { exact: true }),
  ).toBeVisible();
  await expect(
    reportArtifact.getByText("已生成", { exact: true }),
  ).toBeVisible();

  const pdfResponsePromise = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return response.request().method() === "GET"
      && pathname.startsWith("/api/")
      && pathname.endsWith(".pdf");
  });
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 PDF" }).click();
  const pdf = await download;
  const pdfResponse = await pdfResponsePromise;
  expect(pdfResponse.ok()).toBeTruthy();
  expect(pdf.suggestedFilename()).toMatch(/\.pdf$/);
  expect(await pdf.failure()).toBeNull();

  const pdfPath = new URL(pdfResponse.url()).pathname;
  const reportPdfUrl = pdfPath.slice("/api".length);
  expect(reportPdfUrl).toBeTruthy();
  const eventsResponse = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(runId!)}/events`,
    { headers: AUTH_HEADERS },
  );
  expect(eventsResponse.ok()).toBeTruthy();
  const eventsPayload = await eventsResponse.json() as {
    events: Array<{ event_type: string; payload: Record<string, unknown> }>;
  };
  const completed = eventsPayload.events.find(
    (event) => event.event_type === "step.completed"
      && event.payload.tool === "generate_report",
  );
  expect(completed).toBeDefined();
  expect(completed?.payload.evidence_ids).toHaveLength(1);
  expect(completed?.payload.artifact_ids).toHaveLength(1);

  const detail = await getRunDetail(page, runId!);
  expect(detail.run.status).toBe("completed");

  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled();
  await controlButton.click();
  const auditSection = page.getByRole("region", { name: "工具执行审计" });
  const reportAudit = auditSection.locator(".agent-tool-audit", {
    hasText: "generate_report",
  });
  await expect(reportAudit).toContainText("report-tools");
  await expect(reportAudit).toContainText("analysis:execute");
  await expect(reportAudit).toContainText("healthy");
  await expect(reportAudit).toContainText("Evidence");
  await expect(reportAudit).toContainText("Artifact");
  await page.getByRole("button", { name: "关闭任务协作" }).click();

  // 4D：在真实 API/SQLite 上追加反馈，再以辅助模式创建 LLM Planner 分支。
  await controlButton.click();
  let panel = page.getByRole("dialog", { name: "任务协作" });
  await panel.getByRole("radio", { name: "需改进" }).click();
  await panel.getByRole("textbox", { name: "反馈说明" }).fill(FEEDBACK_COMMENT);
  const feedbackResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith(`/api/agent/runs/${runId}/feedback`)
      && response.request().method() === "POST",
  );
  await panel.getByRole("button", { name: "提交反馈" }).click();
  const feedbackResponse = await feedbackResponsePromise;
  expect(feedbackResponse.ok()).toBeTruthy();
  await expect(panel.getByLabel("结果反馈")).toContainText(FEEDBACK_COMMENT);
  await page.getByRole("button", { name: "关闭任务协作" }).click();

  await page.getByRole("radio", { name: "辅助模式" }).click();
  await controlButton.click();
  panel = page.getByRole("dialog", { name: "任务协作" });
  await panel.getByRole("textbox", { name: "新分支目标" }).fill(BRANCH_GOAL);
  const branchResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/chat/stream")
      && response.request().method() === "POST",
  );
  await panel.getByRole("button", { name: "创建分析分支" }).click();
  const branchResponse = await branchResponsePromise;
  expect(branchResponse.ok()).toBeTruthy();
  const assistedRunId = await branchResponse.headerValue("x-chatbi-run-id");
  expect(assistedRunId).toBeTruthy();
  await expect.poll(
    async () => getRunStatusForPolling(page, assistedRunId!),
    { timeout: 60_000 },
  ).toBe("paused");

  await expect(controlButton).toBeEnabled();
  await controlButton.click();
  panel = page.getByRole("dialog", { name: "任务协作" });
  await expect(panel.locator(".agent-status")).toHaveText("已暂停");
  await expect(panel).toContainText("辅助模式");
  await expect(panel).toContainText("data.profile");

  const assistedEventsBeforeResume = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(assistedRunId!)}/events`,
    { headers: AUTH_HEADERS },
  );
  expect(assistedEventsBeforeResume.ok()).toBeTruthy();
  const assistedInitialPayload = await assistedEventsBeforeResume.json() as {
    events: Array<{ event_type: string; payload: Record<string, unknown> }>;
  };
  const assistedPlan = assistedInitialPayload.events.find(
    (event) => event.event_type === "plan.created",
  );
  expect((assistedPlan?.payload.planner as { route?: string })?.route).toBe("llm");
  expect(
    assistedInitialPayload.events.some(
      (event) => event.event_type === "autonomy.plan_review_requested",
    ),
  ).toBeTruthy();

  const resumeResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith(
      `/api/agent/runs/${assistedRunId}/resume/stream`,
    ) && response.request().method() === "POST",
  );
  await panel.getByRole("button", { name: "确认计划并执行" }).click();
  const resumeResponse = await resumeResponsePromise;
  expect(resumeResponse.ok()).toBeTruthy();
  await expect.poll(
    async () => getRunStatusForPolling(page, assistedRunId!),
    { timeout: 120_000 },
  ).toBe("completed");
  await expect(page.getByText(
    "Compose 4D 分支画像已完成，并已按父分支反馈重新核对。",
    { exact: true },
  )).toBeVisible();
  const assistedDetail = await getRunDetail(page, assistedRunId!);
  expect(assistedDetail.run.parent_run_id).toBe(runId);
  expect(assistedDetail.run.autonomy_mode).toBe("assisted");
  expect(assistedDetail.tool_audits).toEqual(expect.arrayContaining([
    expect.objectContaining({ tool_name: "get_data_profile", status: "succeeded" }),
  ]));
  await expect(panel.getByLabel("分支对比")).toContainText("2 个相关 Run");
  await page.getByRole("button", { name: "关闭任务协作" }).click();

  // 标准只读模式必须让 generate_report 在 Host 策略层失败，且不新增报告 Artifact。
  const conversationBefore = await page.request.get(
    `/api/conversations/${encodeURIComponent(detail.run.conversation_id)}`,
    { headers: AUTH_HEADERS },
  );
  expect(conversationBefore.ok()).toBeTruthy();
  const reportCountBefore = (
    (await conversationBefore.json() as { artifacts: Array<{ type: string }> }).artifacts
  ).filter((artifact) => artifact.type === "report").length;

  await page.getByRole("radio", { name: "标准只读" }).click();
  await page.getByLabel("消息内容").fill(READ_ONLY_GOAL);
  const readOnlyResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/chat/stream")
      && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "发送消息" }).click();
  const readOnlyResponse = await readOnlyResponsePromise;
  expect(readOnlyResponse.ok()).toBeTruthy();
  const readOnlyRunId = await readOnlyResponse.headerValue("x-chatbi-run-id");
  expect(readOnlyRunId).toBeTruthy();
  await expect.poll(
    async () => getRunStatusForPolling(page, readOnlyRunId!),
    { timeout: 120_000 },
  ).toMatch(/^(blocked|failed)$/);

  const readOnlyDetail = await getRunDetail(page, readOnlyRunId!);
  expect(readOnlyDetail.run.autonomy_mode).toBe("read_only");
  const reportAttempts = readOnlyDetail.tool_audits.filter(
    (audit) => audit.tool_name === "generate_report",
  );
  expect(reportAttempts.length).toBeGreaterThan(0);
  expect(reportAttempts.every((audit) => audit.status === "failed")).toBeTruthy();
  expect(reportAttempts.every((audit) => (
    audit.evidence_id === null && audit.artifact_id === null
  ))).toBeTruthy();

  const readOnlyEventsResponse = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(readOnlyRunId!)}/events`,
    { headers: AUTH_HEADERS },
  );
  expect(readOnlyEventsResponse.ok()).toBeTruthy();
  const readOnlyEvents = await readOnlyEventsResponse.json() as {
    events: Array<{ event_type: string; payload: Record<string, unknown> }>;
  };
  expect(
    readOnlyEvents.events.some((event) => (
      JSON.stringify(event.payload).includes("autonomy_write_denied")
    )),
  ).toBeTruthy();
  const deniedReportStep = readOnlyEvents.events.find(
    (event) => event.event_type === "step.completed"
      && event.payload.tool === "generate_report"
      && event.payload.status === "failed",
  );
  expect(deniedReportStep).toBeDefined();
  expect(deniedReportStep?.payload.evidence_ids).toEqual([]);
  expect(deniedReportStep?.payload.artifact_ids).toEqual([]);
  expect(readOnlyEvents.events.some(
    (event) => event.event_type === "step.completed"
      && event.payload.tool === "generate_report"
      && event.payload.status === "completed",
  )).toBeFalsy();

  const conversationAfter = await page.request.get(
    `/api/conversations/${encodeURIComponent(detail.run.conversation_id)}`,
    { headers: AUTH_HEADERS },
  );
  expect(conversationAfter.ok()).toBeTruthy();
  const reportCountAfter = (
    (await conversationAfter.json() as { artifacts: Array<{ type: string }> }).artifacts
  ).filter((artifact) => artifact.type === "report").length;
  expect(reportCountAfter).toBe(reportCountBefore);

  const pdfUrl = await page.getByRole("button", { name: "下载 PDF" }).evaluate(
    (button) => button.closest(".report-artifact")?.textContent ? "present" : "",
  );
  expect(pdfUrl).toBe("present");
  const conversationResponse = await page.request.get("/api/projects", {
    headers: AUTH_HEADERS,
  });
  expect(conversationResponse.ok()).toBeTruthy();

  await fs.writeFile(
    path.resolve("../../.data/e2e/compose-result.json"),
    JSON.stringify({
      run_id: runId,
      pdf_url: reportPdfUrl,
      completed_event_count: 1,
      assisted_run_id: assistedRunId,
      read_only_run_id: readOnlyRunId,
    }),
    "utf-8",
  );
});
