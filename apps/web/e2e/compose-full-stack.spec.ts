import fs from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";

const E2E_TOKEN = "chatbi-local-e2e-token-00000001";

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
    { headers: { Authorization: `Bearer ${E2E_TOKEN}` } },
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

  const detailResponse = await page.request.get(
    `/api/agent/runs/${encodeURIComponent(runId!)}`,
    { headers: { Authorization: `Bearer ${E2E_TOKEN}` } },
  );
  expect(detailResponse.ok()).toBeTruthy();
  const detail = await detailResponse.json() as {
    run: { status: string };
  };
  expect(detail.run.status).toBe("completed");

  const pdfUrl = await page.getByRole("button", { name: "下载 PDF" }).evaluate(
    (button) => button.closest(".report-artifact")?.textContent ? "present" : "",
  );
  expect(pdfUrl).toBe("present");
  const conversationResponse = await page.request.get("/api/projects", {
    headers: { Authorization: `Bearer ${E2E_TOKEN}` },
  });
  expect(conversationResponse.ok()).toBeTruthy();

  await fs.writeFile(
    path.resolve("../../.data/e2e/compose-result.json"),
    JSON.stringify({
      run_id: runId,
      pdf_url: reportPdfUrl,
      completed_event_count: 1,
    }),
    "utf-8",
  );
});
