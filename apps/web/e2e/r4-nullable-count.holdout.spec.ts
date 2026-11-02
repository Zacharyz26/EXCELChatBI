import { expect, test, type Page, type Route } from "@playwright/test";

// Two additional independent UI contract cases. Original 17-case spec is unchanged.
// Inputs/oracles: COUNT(value_col) excludes null, COUNT(*) counts all rows.
// These intercepted payloads are frontend fixtures, not real Agent or producer proof.
const NOW = "2026-11-01T19:00:00Z";
const PROJECT = "r4-nullable-count";
const CONVERSATION = "nullable-count-conversation";
const REF = "a".repeat(32);
type Json = Record<string, unknown>;

async function reply(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function install(page: Page, payload: Json) {
  const mutations: string[] = [];
  const conversation = { id: CONVERSATION, project_id: PROJECT, title: "空值计数独立夹具", created_at: NOW, updated_at: NOW };
  const message = { id: "count-message", conversation_id: CONVERSATION, role: "assistant", content: "分组统计见下表。", tool_calls: null, created_at: NOW };
  const artifact = { id: "count-table", conversation_id: CONVERSATION, message_id: message.id, type: "table", payload, file_ref: null, source_tool: "aggregate_preview", params: { analysis_id: "f".repeat(32) }, dataset_ref: REF, created_at: NOW };
  await page.route(/^https?:\/\/[^/]+\/api\//, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api/, "");
    if (request.method() !== "GET") {
      mutations.push(`${request.method()} ${path}`);
      return reply(route, { detail: "READ_ONLY_HOLDOUT" }, 503);
    }
    if (path === "/auth/config") return reply(route, { mode: "disabled" });
    if (path === "/projects") return reply(route, [{ id: PROJECT, name: "计数合同", created_at: NOW }]);
    if (path === `/projects/${PROJECT}/conversations`) return reply(route, [conversation]);
    if (path === `/projects/${PROJECT}/datasets`) return reply(route, [{ ref: REF, project_id: PROJECT, filename: "nullable-count.csv", parent_ref: null, transform: null, created_at: NOW, profile: { dataset_ref: REF, row_count: 3, column_count: 2, columns: [{ name: "组别", dtype: "object", null_ratio: payload.value_col === null ? 2 / 3 : 0, distinct_count: 1, sample_values: ["A"] }, { name: "金额（元）", dtype: "float64", null_ratio: 1 / 3, distinct_count: 2, sample_values: ["10", "20"] }], sample_rows: [] } }]);
    if (path === `/conversations/${CONVERSATION}`) return reply(route, { conversation, messages: [message], artifacts: [artifact], execution_context: { schema_version: 1, availability: "none", reason_codes: [], dataset_ref: null, source_kind: null } });
    if (path === `/agent/runs/by-conversation/${CONVERSATION}/latest`) return reply(route, { run: null });
    if (path === "/kb/overview") return reply(route, { chunk_count: 0, sources: [], topics: [], documents: [] });
    return reply(route, { detail: `UNEXPECTED_READ ${path}` }, 501);
  });
  return mutations;
}

test("R4 nullable metric count is labeled as that field's non-null count, not three rows", async ({ page }) => {
  // Independent rows: (A,10), (A,null), (A,20): COUNT(metric)=2, COUNT(*)=3.
  const mutations = await install(page, {
    agg: "count", group_col: "组别", value_col: "金额（元）", group_total: 1, truncated: false,
    rows: [{ group: "A", value: 2, count: 2, valid_value_count: 2, row_count: 3 }],
  });
  await page.goto("/");
  const table = page.locator(".table-artifact");
  await expect(table.getByRole("columnheader", { name: /(?:非空数|有效值数).*金额（元）/ })).toHaveCount(1);
  const cells = table.locator("tbody tr").first().getByRole("cell");
  await expect(cells.nth(1)).toHaveText("2");
  await expect(cells.nth(2)).toHaveText("2");
  await expect(table).not.toContainText(/人数/);
  await expect(table.getByRole("row", { name: /总计/ })).toHaveCount(0);
  expect(mutations).toEqual([]);
});

test("R4 omitted value_col counts the group field, retaining a null group with zero valid values", async ({ page }) => {
  // Independent rows: (null,10), (null,null), (A,20).
  // Tool defaults value_col to group_col: null group's COUNT(group)=0, COUNT(*)=2.
  const mutations = await install(page, {
    agg: "count", group_col: "组别", value_col: null, group_total: 2, truncated: false,
    rows: [{ group: null, value: 0, count: 0, valid_value_count: 0, row_count: 2 }, { group: "A", value: 1, count: 1, valid_value_count: 1, row_count: 1 }],
  });
  await page.goto("/");
  const table = page.locator(".table-artifact");
  await expect(table.getByRole("columnheader", { name: /(?:非空数|有效值数).*组别/ })).toHaveCount(1);
  const cells = table.locator("tbody tr").first().getByRole("cell");
  await expect(cells.nth(1)).toHaveText("0");
  await expect(cells.nth(2)).toHaveText("0");
  await expect(table).not.toContainText(/人数/);
  await expect(table.getByRole("row", { name: /总计/ })).toHaveCount(0);
  expect(mutations).toEqual([]);
});
