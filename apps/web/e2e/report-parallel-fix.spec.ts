import fs from "node:fs/promises";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const E2E_TOKEN = "chatbi-local-e2e-token-00000001";
const AUTH_HEADERS = { Authorization: `Bearer ${E2E_TOKEN}` };

interface RunDetail {
  run: { status: string };
  plan: {
    definition: {
      steps: Array<{ capability: string; dependencies: string[] }>;
    };
  } | null;
}

async function waitForRunDetail(
  page: Page,
  runId: string,
  expectedStatus: string,
  timeoutMs = 120_000,
): Promise<RunDetail> {
  const deadline = Date.now() + timeoutMs;
  let lastStatus = "not_visible";
  while (Date.now() < deadline) {
    const response = await page.request.get(
      `/api/agent/runs/${encodeURIComponent(runId)}`,
      { headers: AUTH_HEADERS },
    );
    if (response.status() === 404) {
      lastStatus = "not_visible";
    } else {
      if (!response.ok()) {
        const body = await response.text();
        throw new Error(
          `获取 Run ${runId} 失败：HTTP ${response.status()} ${body.slice(0, 500)}`,
        );
      }
      const detail = await response.json() as RunDetail;
      lastStatus = detail.run.status;
      if (lastStatus === expectedStatus) return detail;
    }
    await page.waitForTimeout(250);
  }
  throw new Error(
    `Run ${runId} 在 ${timeoutMs}ms 内未达到 ${expectedStatus}，最后状态：${lastStatus}`,
  );
}

async function sendAndCaptureRun(page: Page, message: string): Promise<string> {
  await page.getByLabel("消息内容").fill(message);
  const responsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/chat/stream")
      && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "发送消息" }).click();
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  const runId = await response.headerValue("x-chatbi-run-id");
  expect(runId).toBeTruthy();
  return runId!;
}

test("独立画像趋势并行后一次生成并下载 PDF", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  await page.goto("/");
  await page.getByLabel("访问令牌").fill(E2E_TOKEN);
  await page.getByRole("button", { name: "进入工作区" }).click();
  await expect(page.getByRole("button", { name: "我的分析项目" })).toBeVisible();

  const isolatedProject = `Compose 报告专项 ${Date.now()}`;
  await page.getByLabel("新建项目").click();
  await page.getByPlaceholder("项目名称").fill(isolatedProject);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByRole("button", {
    name: isolatedProject,
    exact: true,
  })).toBeVisible();

  const uploadButton = page.getByRole("button", { name: "上传 Excel", exact: true });
  const fixture = path.resolve("../../.data/e2e/sales.xlsx");
  const chooserPromise = page.waitForEvent("filechooser");
  await uploadButton.click();
  const chooser = await chooserPromise;
  const uploadResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/upload/excel")
      && response.request().method() === "POST",
  );
  await chooser.setFiles(fixture);
  expect((await uploadResponsePromise).ok()).toBeTruthy();
  await expect(page.getByText("已完成“sales.xlsx”的数据画像，共 6 行、3 列。")).toBeVisible();

  const parallelRunId = await sendAndCaptureRun(
    page,
    "COMPOSE_6A_PARALLEL：请深入分析这份数据的画像和销售额时间趋势。",
  );
  await expect(page.getByText(
    "Compose 6A 受控并行画像与趋势分析已完成。",
    { exact: true },
  )).toBeVisible({ timeout: 120_000 });

  const parallelDetail = await waitForRunDetail(page, parallelRunId, "completed");
  const trendStep = parallelDetail.plan?.definition.steps.find(
    (step) => step.capability === "stats.trend",
  );
  expect(trendStep?.dependencies).toEqual([]);
  const parallelEventsResponse = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(parallelRunId)}/events`,
    { headers: AUTH_HEADERS },
  );
  expect(parallelEventsResponse.ok()).toBeTruthy();
  const parallelEvents = await parallelEventsResponse.json() as {
    events: Array<{
      sequence: number;
      event_type: string;
      payload: Record<string, unknown>;
    }>;
  };
  const started = parallelEvents.events.filter(
    (event) => event.event_type === "step.started" && event.payload.parallel === true,
  );
  const completed = parallelEvents.events.filter(
    (event) => event.event_type === "step.completed"
      && ["get_data_profile", "trend_analysis"].includes(String(event.payload.tool)),
  );
  expect(started).toHaveLength(2);
  expect(completed).toHaveLength(2);
  expect(Math.max(...started.map((event) => event.sequence))).toBeLessThan(
    Math.min(...completed.map((event) => event.sequence)),
  );

  const reportRunId = await sendAndCaptureRun(
    page,
    "COMPOSE_REPORT：请把本次对话已完成的数据画像组装成一份报告，附要点解读，并导出 PDF。",
  );
  await expect(page.getByText(
    "报告和 PDF 已基于本对话的已验证数据画像生成。",
    { exact: true },
  )).toBeVisible({ timeout: 120_000 });
  const reportArtifact = page.locator(".report-artifact");
  await expect(reportArtifact.getByText("已生成", { exact: true })).toBeVisible();

  const pdfResponsePromise = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return response.request().method() === "GET"
      && pathname.startsWith("/api/")
      && pathname.endsWith(".pdf");
  });
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 PDF" }).click();
  const [download, pdfResponse] = await Promise.all([
    downloadPromise,
    pdfResponsePromise,
  ]);
  expect(pdfResponse.ok()).toBeTruthy();
  expect(await download.failure()).toBeNull();
  const downloadPath = await download.path();
  expect(downloadPath).toBeTruthy();
  const pdfBytes = await fs.readFile(downloadPath!);
  expect(pdfBytes.subarray(0, 5).toString("ascii")).toBe("%PDF-");
  await testInfo.attach("report.pdf", { body: pdfBytes, contentType: "application/pdf" });

  await waitForRunDetail(page, reportRunId, "completed");
  const reportEventsResponse = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(reportRunId)}/events`,
    { headers: AUTH_HEADERS },
  );
  expect(reportEventsResponse.ok()).toBeTruthy();
  const reportEvents = await reportEventsResponse.json() as {
    events: Array<{ event_type: string; payload: Record<string, unknown> }>;
  };
  const reportCompletions = reportEvents.events.filter(
    (event) => event.event_type === "step.completed"
      && event.payload.tool === "generate_report",
  );
  expect(reportCompletions).toHaveLength(1);
  expect(reportCompletions[0]?.payload.status).toBe("completed");
  expect(reportEvents.events.some(
    (event) => event.payload.observation
      && JSON.stringify(event.payload.observation).includes("duplicate_invocation_circuit_break"),
  )).toBeFalsy();
});
