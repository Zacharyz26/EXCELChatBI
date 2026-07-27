import path from "node:path";
import { expect, test } from "@playwright/test";

const E2E_TOKEN = "chatbi-fullstack-e2e-token-00000001";

test("真实 Web/API 完成项目初始化与 Excel 上传", async ({ page }) => {
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

  const fixture = path.resolve("../../.data/e2e/sales.xlsx");
  await page.locator('input[type="file"]').setInputFiles(fixture);

  await expect(page.getByText("上传了文件：sales.xlsx", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText("已完成“sales.xlsx”的数据画像，共 3 行、3 列。")).toBeVisible();
  await expect(page.getByText("1 个数据集", { exact: true })).toBeVisible();
  await expect(page.getByText("sales.xlsx", { exact: true }).first()).toBeVisible();
  expect(networkFailures).toEqual([]);
});
