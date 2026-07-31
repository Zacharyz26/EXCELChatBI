import path from "node:path";
import { expect, test } from "@playwright/test";

const E2E_TOKEN = "chatbi-fullstack-e2e-token-00000001";

test("真实 Web/API 完成记忆治理、Excel 上传与血缘查看", async ({ page }) => {
  const networkFailures: string[] = [];
  page.on("requestfailed", (request) => {
    networkFailures.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "连接 ChatBI" })).toBeVisible();
  await page.getByLabel("访问令牌").fill(E2E_TOKEN);
  await page.getByRole("button", { name: "进入工作区" }).click();
  await expect(page.getByRole("button", { name: "我的分析项目" })).toBeVisible();
  await expect(page.getByRole("main").getByRole("heading", { name: "新对话" })).toBeVisible();

  const readiness = await page.request.get("/api/health/ready");
  expect(readiness.ok()).toBeTruthy();
  await expect(readiness.json()).resolves.toMatchObject({ status: "ready" });

  await page.getByRole("button", { name: "项目记忆" }).click();
  await expect(page.getByRole("heading", { name: "项目记忆" })).toBeVisible();
  await expect(page.getByText("外部编号的展示名称是对象 ID")).toBeVisible();
  await page.getByRole("button", { name: "纠正" }).click();
  await page.getByLabel("摘要").fill("外部编号统一展示为对象标识");
  await page.getByRole("button", { name: "保存新版本" }).click();
  await expect(page.getByText("纠正已保存为新版本，旧版本继续保留在历史中。")).toBeVisible();
  const revisedCard = page.locator(".memory-card").filter({
    hasText: "外部编号统一展示为对象标识",
  });
  await expect(revisedCard.getByText("v2", { exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await revisedCard.getByRole("button", { name: "删除" }).click();
  await expect(page.getByText("记忆已软删除；已有运行和历史快照仍保持原样。")).toBeVisible();
  await expect(revisedCard.getByText("已删除", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭项目记忆" }).click();

  const fixture = path.resolve("../../.data/e2e/sales.xlsx");
  await page.locator('input[type="file"]').setInputFiles(fixture);

  await expect(page.getByText("上传了文件：sales.xlsx", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText("已完成“sales.xlsx”的数据画像，共 3 行、3 列。")).toBeVisible();
  await expect(page.getByText("1 个数据集", { exact: true })).toBeVisible();
  await expect(page.getByText("sales.xlsx", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "数据血缘" }).click();
  await expect(page.getByRole("heading", { name: "数据血缘" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "数据集", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "分析工件" })).toBeVisible();
  await expect(page.getByText("完整", { exact: true })).toBeVisible();
  const datasetNode = page.locator(".lineage-node").filter({ hasText: "sales.xlsx" });
  await expect(datasetNode).toBeVisible();
  await datasetNode.click();
  await expect(page.getByText("此视图不返回工具参数、文件路径、数据样本或结果正文。")).toBeVisible();
  await page.getByRole("button", { name: "关闭数据血缘" }).click();

  expect(networkFailures).toEqual([]);
});
