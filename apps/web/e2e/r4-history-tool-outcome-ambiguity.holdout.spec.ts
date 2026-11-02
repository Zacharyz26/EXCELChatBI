import { expect, test, type Page, type Route } from "@playwright/test";

// Independent UI contract fixtures; API intercepted, not real Agent/DB evidence.
// New independent history-outcome holdout. Root schedules all execution.
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


function msg(id: string, role: string, content: string, toolCalls: Json[] | null = null): Json {
  return { id, conversation_id: "conversation-a", role, content, tool_calls: toolCalls, created_at: NOW };
}
function request(id: string, title: string, tool: string): Json {
  return msg(id, "assistant", title, [{ id: "provider-local-call", name: tool, arguments: JSON.stringify({ dataset_ref: CURRENT }) }]);
}
function result(id: string, status: string, text: string, fields: string): Json {
  return msg(id, "tool", JSON.stringify({ tool_call_id: "provider-local-call", status,
    ...(status === "error" ? { message: text } : { summary: text }), fields }));
}
function card(page: Page, title: string) {
  return page.locator(".message-row--assistant").filter({ hasText: title }).locator(".tool-step");
}


test("R4 duplicate call ids inside one assistant response are ambiguous", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].messages = [
    msg("user-duplicate", "user", "执行两个独立步骤"),
    msg("assistant-duplicate", "assistant", "同轮冲突调用", [
      { id: "provider-local-call", name: "get_data_profile", arguments: "{}" },
      { id: "provider-local-call", name: "generate_report", arguments: "{}" },
    ]),
    result("outcome-duplicate", "ok", "不能唯一归属的成功", "不能唯一归属字段"),
  ];
  await page.goto("/");
  const cards = card(page, "同轮冲突调用");
  await expect(cards).toHaveCount(2);
  for (let i = 0; i < 2; i += 1) {
    await expect(cards.nth(i)).not.toHaveClass(/tool-step--ok(?:\s|$)/);
    await expect(cards.nth(i)).toContainText(/未知|暂无|未收到|待确认|未确认/);
    await expect(cards.nth(i)).not.toContainText("不能唯一归属的成功");
  }
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 duplicate outcomes for the same owning call cannot silently choose last", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].messages = [
    msg("user-two-results", "user", "执行画像"),
    request("assistant-two-results", "同轮结果冲突", "get_data_profile"),
    result("outcome-one", "error", "冲突中的失败结果", "第一结果字段"),
    result("outcome-two", "ok", "冲突中的成功结果", "第二结果字段"),
  ];
  await page.goto("/");
  const c = card(page, "同轮结果冲突");
  await expect(c).toHaveCount(1);
  await expect(c).not.toHaveClass(/tool-step--ok(?:\s|$)/);
  await expect(c).not.toHaveClass(/tool-step--error(?:\s|$)/);
  await expect(c).toContainText(/未知|暂无|未收到|待确认|未确认/);
  await expect(c).not.toContainText("冲突中的成功结果");
  await expect(c).not.toContainText("冲突中的失败结果");
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 an unknown historical outcome status is not successful execution", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].messages = [
    msg("user-invalid-status", "user", "执行画像"),
    request("assistant-invalid-status", "没有合法结果状态", "get_data_profile"),
    result("outcome-invalid-status", "completed", "未知状态不能证明成功", "未验证字段"),
  ];
  await page.goto("/");
  const c = card(page, "没有合法结果状态");
  await expect(c).not.toHaveClass(/tool-step--ok(?:\s|$)/);
  await expect(c).not.toContainText("未知状态不能证明成功");
  await expect(c).not.toContainText("未验证字段");
  await expect(c).toContainText(/未知|暂无|未收到|待确认|未确认/);
  expect(mutations(s.requests)).toEqual([]);
});
