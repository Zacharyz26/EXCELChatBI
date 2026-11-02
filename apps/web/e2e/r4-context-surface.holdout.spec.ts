import { expect, test, type Page, type Route } from "@playwright/test";

// FROZEN FRONTEND CONTRACT FIXTURES, NOT real Agent/DB/browser E2E evidence.
// Do not copy to the repo or execute before root schedules the R4 baseline.
// Every /api call is intercepted; unexpected mutations fail the assertion.
const NOW = "2026-11-01T19:00:00Z";
const ORIGINAL = "a".repeat(32);
const CURRENT = "b".repeat(32);
const OTHER = "c".repeat(32);
const PROJECT = "r4-project";
type Json = Record<string, unknown>;
type RunStatus = "paused" | "failed" | "blocked" | "completed";

function dataset(ref: string, filename: string, rows: number, parent: string | null = null): Json {
  return {
    ref, project_id: PROJECT, filename, parent_ref: parent,
    transform: parent ? { drop_duplicates: true } : null, created_at: NOW,
    profile: {
      dataset_ref: ref, row_count: rows, column_count: 2,
      columns: [
        { name: "月份", dtype: "object", null_ratio: 0, distinct_count: 6, sample_values: ["2026-01"] },
        { name: "播出时长（分钟）", dtype: "float64", null_ratio: 0, distinct_count: rows, sample_values: ["15"] },
      ], sample_rows: [],
    },
  };
}

function context(ref: string | null, options: {
  availability?: string; kind?: string; status?: RunStatus; runId?: string;
} = {}): Json {
  return {
    schema_version: 1,
    availability: options.availability ?? (ref ? "available" : "none"),
    reason_codes: options.availability === "unavailable" ? ["dataset_unavailable"] : [],
    dataset_ref: ref, source_kind: options.kind ?? (ref ? "transform" : null),
    source_run_id: ref === CURRENT ? "successful-transform-run" : null,
    source_invocation_id: ref === CURRENT ? "actual-transform-invocation-fixture" : null,
    source_artifact_id: null, source_occurred_at: NOW,
    parent_ref: ref === CURRENT ? ORIGINAL : null,
    lineage: ref === CURRENT ? [{ input_ref: ORIGINAL, output_ref: CURRENT, invocation_id: "actual-transform-invocation-fixture", arguments: { drop_duplicates: true } }] : [],
    run_id: options.runId ?? null, run_status: options.status ?? null,
    run_state_version: options.status ? 4 : null,
    scope_version_hash: ref ? "d".repeat(64) : null,
    scope_version_run_id: ref ? "successful-transform-run" : null,
    profile_artifact_id: null, analysis_ids: [],
    field_context: { column: null, source_run_id: null }, context_hash: "e".repeat(64),
  };
}

function runDetail(id: string, conversationId: string, status: RunStatus): Json {
  const run = {
    run_id: id, project_id: PROJECT, conversation_id: conversationId,
    user_message_id: `${id}-user`, parent_run_id: null, goal: "继续本会话分析",
    status, state_version: 4, plan_version: 1, budget: { max_tool_calls: 4 },
    usage: { tool_calls: 0 }, terminal_reason: status === "failed" ? "tool_failed" : null,
    created_at: NOW, updated_at: NOW, finished_at: ["failed", "completed", "blocked"].includes(status) ? NOW : null,
  };
  return { run, contract: { goal: run.goal }, plan: null, steps: [], tool_audits: [],
    related_runs: [], feedback: [], state: { last_sequence: 0 }, execution_control: null };
}

async function respond(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function install(page: Page) {
  const conversations = [
    { id: "conversation-a", project_id: PROJECT, title: "广播清洗会话", created_at: NOW, updated_at: NOW },
    { id: "conversation-b", project_id: PROJECT, title: "独立设备会话", created_at: NOW, updated_at: NOW },
  ];
  // Project latest intentionally disagrees with conversation A execution source.
  const datasets = [dataset(ORIGINAL, "broadcast-original.csv", 75),
    dataset(CURRENT, "broadcast-cleaned.csv", 36, ORIGINAL),
    dataset(OTHER, "other-conversation.csv", 2)];
  const states: Record<string, { execution_context: Json | undefined; messages: Json[]; artifacts: Json[]; run: Json | null }> = {
    "conversation-a": { execution_context: context(CURRENT), messages: [], artifacts: [], run: null },
    "conversation-b": { execution_context: context(OTHER, { kind: "upload" }), messages: [], artifacts: [], run: null },
  };
  const requests: Array<{ method: string; path: string; body: string | null }> = [];
  await page.route(/^https?:\/\/[^/]+\/api\//, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^\/api/, "");
    requests.push({ method: req.method(), path, body: req.postData() });
    if (req.method() !== "GET") return respond(route, { detail: "READ_ONLY_HOLDOUT: mutation forbidden" }, 503);
    if (path === "/auth/config") return respond(route, { mode: "disabled" });
    if (path === "/projects") return respond(route, [{ id: PROJECT, name: "R4 合成项目", created_at: NOW }]);
    if (path === `/projects/${PROJECT}/conversations`) return respond(route, conversations);
    if (path === `/projects/${PROJECT}/datasets`) return respond(route, datasets);
    if (path === "/kb/overview") return respond(route, { chunk_count: 0, sources: [], topics: [], documents: [] });
    const conversation = path.match(/^\/conversations\/([^/]+)$/);
    if (conversation) {
      const c = conversations.find((item) => item.id === conversation[1]);
      const s = states[conversation[1]];
      if (!c || !s) return respond(route, { detail: "not found" }, 404);
      return respond(route, { conversation: c, messages: s.messages, artifacts: s.artifacts, execution_context: s.execution_context });
    }
    const latest = path.match(/^\/agent\/runs\/by-conversation\/([^/]+)\/latest$/);
    if (latest) return respond(route, { run: states[latest[1]]?.run?.run ?? null });
    const detail = path.match(/^\/agent\/runs\/([^/]+)$/);
    if (detail) {
      const d = Object.values(states).find((item) => (item.run?.run as Json | undefined)?.run_id === detail[1])?.run;
      return respond(route, d ?? { detail: "unknown run" }, d ? 200 : 404);
    }
    const events = path.match(/^\/agent\/runs\/([^/]+)\/events$/);
    if (events) return respond(route, { run_id: events[1], events: [], last_sequence: 0 });
    return respond(route, { detail: `UNEXPECTED_READ ${path}` }, 501);
  });
  return { states, datasets, requests };
}

function execution(page: Page) { return page.getByRole("region", { name: "执行数据上下文", exact: true }); }
function mutations(requests: Array<{ method: string }>) { return requests.filter((r) => r.method !== "GET"); }

async function addTable(state: Awaited<ReturnType<typeof install>>, total: unknown, truncated = true) {
  const conversationId = "conversation-a";
  const messageId = "independent-month-table-message";
  state.states[conversationId].messages.push({ id: messageId, conversation_id: conversationId,
    role: "assistant", content: "月度统计见下表。", tool_calls: null, created_at: NOW });
  const payload: Json = { group_col: "月份", value_col: "播出时长（分钟）", agg: "sum",
    group_total: 6, truncated, rows: [{ group: "2026-01", value: 75, count: 6 }, { group: "2026-02", value: 135, count: 6 }] };
  if (total !== undefined) payload.total_value = total;
  state.states[conversationId].artifacts.push({ id: "independent-month-table", conversation_id: conversationId,
    message_id: messageId, type: "table", payload, file_ref: null, source_tool: "aggregate_preview",
    params: { analysis_id: "f".repeat(32) }, dataset_ref: CURRENT, created_at: NOW });
}

test("R4 server source differs from project latest and remains authoritative", async ({ page }) => {
  const s = await install(page);
  await page.goto("/");
  await expect(execution(page)).toContainText("broadcast-cleaned.csv");
  await expect(execution(page)).not.toContainText("other-conversation.csv");
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 viewing another dataset never changes execution source or sends an operation", async ({ page }) => {
  const s = await install(page);
  await page.goto("/");
  await page.locator(".dataset-item__main").filter({ hasText: "broadcast-original.csv" }).click();
  await expect(page.locator(".context-dataset-name")).toContainText("broadcast-original.csv");
  await expect(execution(page)).toContainText("broadcast-cleaned.csv");
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 conversation switch clears browser-only viewing state and restores its own source", async ({ page }) => {
  const s = await install(page);
  await page.goto("/");
  await page.locator(".dataset-item__main").filter({ hasText: "broadcast-original.csv" }).click();
  await page.locator(".conversation-item__main").filter({ hasText: "独立设备会话" }).click();
  await expect(execution(page)).toContainText("other-conversation.csv");
  await expect(page.locator(".context-dataset-name")).not.toContainText("broadcast-original.csv");
  await page.locator(".conversation-item__main").filter({ hasText: "广播清洗会话" }).click();
  await expect(execution(page)).toContainText("broadcast-cleaned.csv");
  await expect(page.locator(".context-dataset-name")).not.toContainText("broadcast-original.csv");
  await page.reload();
  await expect(execution(page)).toContainText("broadcast-cleaned.csv");
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 an already verified explicit parent selection survives refresh despite a newer project dataset", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].execution_context = context(ORIGINAL, { kind: "selection" });
  await page.goto("/");
  await expect(execution(page)).toContainText("broadcast-original.csv");
  await page.reload();
  await expect(execution(page)).toContainText("broadcast-original.csv");
  await expect(execution(page)).not.toContainText("broadcast-cleaned.csv");
  expect(mutations(s.requests)).toEqual([]);
});

for (const availability of ["unavailable", "none", "ambiguous"]) {
  test(`R4 ${availability} source cannot fall back to any project dataset`, async ({ page }) => {
    const s = await install(page);
    s.states["conversation-a"].execution_context = context(null, { availability });
    await page.goto("/");
    await expect(execution(page)).toBeVisible();
    for (const file of ["broadcast-original.csv", "broadcast-cleaned.csv", "other-conversation.csv"]) {
      await expect(execution(page)).not.toContainText(file);
    }
    // A blank region would conceal the unavailable state; require a visible non-success explanation.
    await expect(execution(page)).toContainText(/未连接|不可用|无法|不明确|尚未|存在歧义/);
    await page.reload();
    await expect(execution(page)).not.toContainText("other-conversation.csv");
    expect(mutations(s.requests)).toEqual([]);
  });
}

test("R4 missing context projection in an old response is unknown, never project latest", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].execution_context = undefined;
  await page.goto("/");
  await expect(execution(page)).toBeVisible();
  await expect(execution(page)).toContainText(/未连接|不可用|无法|不明确|尚未|未知/);
  await expect(execution(page)).not.toContainText("other-conversation.csv");
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 a source invalidated after display cannot survive refresh through stale UI state", async ({ page }) => {
  const s = await install(page);
  await page.goto("/");
  await expect(execution(page)).toContainText("broadcast-cleaned.csv");
  s.states["conversation-a"].execution_context = context(null, { availability: "unavailable" });
  await page.reload();
  await expect(execution(page)).not.toContainText("broadcast-cleaned.csv");
  await expect(execution(page)).not.toContainText("broadcast-original.csv");
  await expect(execution(page)).not.toContainText("other-conversation.csv");
  await expect(execution(page)).toContainText(/不可用|无法/);
  expect(mutations(s.requests)).toEqual([]);
});

for (const [status, label] of [["paused", "任务已暂停"], ["failed", "任务失败"], ["blocked", "任务已阻塞"]] as const) {
  test(`R4 ${status} run stays ${status} even with a valid successful-child source`, async ({ page }) => {
    const s = await install(page);
    const id = `${status}-run`;
    s.states["conversation-a"].run = runDetail(id, "conversation-a", status);
    s.states["conversation-a"].execution_context = context(CURRENT, { status, runId: id });
    await page.goto("/");
    await expect(execution(page)).toContainText("broadcast-cleaned.csv");
    await expect(page.locator(".conversation-header__status")).toContainText(label);
    await expect(page.locator(".conversation-header__status")).not.toContainText("任务已完成");
    await page.reload();
    await expect(page.locator(".conversation-header__status")).toContainText(label);
    expect(mutations(s.requests)).toEqual([]);
  });
}

test("R4 truncated preview displays authoritative total1350, never visible-row sum210", async ({ page }) => {
  const s = await install(page);
  await addTable(s, 1350);
  await page.goto("/");
  const card = page.locator(".table-artifact");
  const total = card.getByRole("row", { name: /总计/ });
  await expect(total).toHaveCount(1);
  await expect(total).toContainText(/1,?350/);
  await expect(total).not.toContainText("210");
  await expect(card).toContainText("共 6 组，仅展示前 2 组");
  await expect(card).toContainText("播出时长（分钟）");
  await page.reload();
  await expect(page.locator(".table-artifact").getByRole("row", { name: /总计/ })).toContainText(/1,?350/);
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 explicit zero is shown as a total rather than treated as absent", async ({ page }) => {
  const s = await install(page);
  // UI transport fixture deliberately distinguishes payload total from preview summation.
  await addTable(s, 0);
  await page.goto("/");
  const total = page.locator(".table-artifact").getByRole("row", { name: /总计/ });
  await expect(total).toHaveCount(1);
  await expect(total.getByRole("cell").filter({ hasText: /^0(?:\.0+)?$/ })).toHaveCount(1);
});

for (const missing of [undefined, null]) {
  test(`R4 ${missing === null ? "null" : "missing"} total remains unknown and is not recomputed from preview`, async ({ page }) => {
    const s = await install(page);
    await addTable(s, missing);
    await page.goto("/");
    const card = page.locator(".table-artifact");
    await expect(card).toBeVisible();
    const total = card.getByRole("row", { name: /总计/ });
    // Either omit the total or explicitly render unknown; never fabricate 210 or zero.
    if (await total.count()) {
      await expect(total).toContainText(/未知|未提供|不可用|—/);
      await expect(total).not.toContainText("210");
      await expect(total.getByRole("cell").filter({ hasText: /^0(?:\.0+)?$/ })).toHaveCount(0);
    }
    expect(mutations(s.requests)).toEqual([]);
  });
}

test("R4 total_value exact-number transport retains integer precision independently of preview", async ({ page }) => {
  const s = await install(page);
  await addTable(s, { exact_number: "9007199254740993", number_type: "integer" });
  await page.goto("/");
  const total = page.locator(".table-artifact").getByRole("row", { name: /总计/ });
  await expect(total).toContainText("9007199254740993");
  await expect(total).not.toContainText("9007199254740992");
  await page.reload();
  await expect(page.locator(".table-artifact").getByRole("row", { name: /总计/ })).toContainText("9007199254740993");
  expect(mutations(s.requests)).toEqual([]);
});
