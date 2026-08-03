import { expect, test } from "@playwright/test";

const E2E_TOKEN = "chatbi-local-e2e-token-00000001";
const recoveryOnly = process.env.CHATBI_COMPOSE_RECOVERY_ONLY === "1";
const runId = process.env.CHATBI_COMPOSE_RECOVERY_RUN_ID ?? "";

test.skip(!recoveryOnly, "仅在 Compose 服务重启后的恢复阶段执行");

test("全新浏览器从服务端恢复 TaskRun 与报告，且 MCP 不暴露", async ({ page }) => {
  expect(runId).not.toBe("");

  await page.goto("/");
  await page.getByLabel("访问令牌").fill(E2E_TOKEN);
  await page.getByRole("button", { name: "进入工作区" }).click();
  await expect(page.getByRole("button", { name: "我的分析项目" })).toBeVisible();

  const controlButton = page.getByRole("button", { name: "任务协作" });
  await expect(controlButton).toBeEnabled({ timeout: 30_000 });
  await controlButton.click();
  const panel = page.getByRole("dialog", { name: "任务协作" });
  await expect(panel.locator(".agent-status")).toHaveText("已完成");
  await expect(panel).toContainText(`Run ${runId.slice(0, 10)}`);
  const reportAudit = panel.locator(".agent-tool-audit", {
    hasText: "generate_report",
  });
  await expect(reportAudit).toContainText("report-tools");
  await expect(reportAudit).toContainText("healthy");
  await expect(reportAudit).toContainText("Evidence");
  await expect(reportAudit).toContainText("Artifact");

  await page.getByRole("button", { name: "关闭任务协作" }).click();
  const report = page.locator(".report-artifact");
  await expect(report.getByText("分析报告", { exact: true })).toBeVisible();
  await expect(report.getByText("已生成", { exact: true })).toBeVisible();

  const pdfResponsePromise = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return response.request().method() === "GET"
      && pathname.startsWith("/api/")
      && pathname.endsWith(".pdf");
  });
  const downloadPromise = page.waitForEvent("download");
  await report.getByRole("button", { name: "下载 PDF" }).click();
  const [pdfResponse, download] = await Promise.all([
    pdfResponsePromise,
    downloadPromise,
  ]);
  expect(pdfResponse.ok()).toBeTruthy();
  expect(await download.failure()).toBeNull();

  const mcpResponse = await page.request.get("/mcp/");
  expect(mcpResponse.status()).toBe(404);
});
