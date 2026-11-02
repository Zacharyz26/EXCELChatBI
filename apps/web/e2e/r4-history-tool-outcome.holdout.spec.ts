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

for (const firstStatus of ["error", "ok"] as const) {
  test(`R4 historical local tool ids cannot cross turns: first ${firstStatus}`, async ({ page }) => {
    const s = await install(page);
    const first = "第一轮独立画像";
    const second = "第二轮独立报告";
    const secondStatus = firstStatus === "error" ? "ok" : "error";
    s.states["conversation-a"].messages = [
      msg("user-one", "user", "先检查数据"),
      request("assistant-one", first, "get_data_profile"),
      result("tool-one", firstStatus, "第一轮画像独有结果", "第一轮字段"),
      msg("answer-one", "assistant", "第一轮结束。"),
      msg("user-two", "user", "再生成报告"),
      request("assistant-two", second, "generate_report"),
      result("tool-two", secondStatus, "第二轮报告独有结果", "第二轮字段"),
      msg("answer-two", "assistant", "第二轮结束。"),
    ];
    await page.goto("/");
    for (const [title, status, expected, forbidden, fields] of [
      [first, firstStatus, "第一轮画像独有结果", "第二轮报告独有结果", "第一轮字段"],
      [second, secondStatus, "第二轮报告独有结果", "第一轮画像独有结果", "第二轮字段"],
    ]) {
      const c = card(page, title);
      await expect(c).toHaveCount(1);
      await expect(c).toHaveClass(new RegExp(`tool-step--${status}(?:\\s|$)`));
      await expect(c).toContainText(expected);
      await expect(c).toContainText(fields);
      await expect(c).not.toContainText(forbidden);
    }
    await page.reload();
    await expect(card(page, first)).toContainText("第一轮画像独有结果");
    await expect(card(page, second)).toContainText("第二轮报告独有结果");
    expect(mutations(s.requests)).toEqual([]);
  });
}

test("R4 orphan result before the assistant call cannot become its result", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].messages = [
    result("orphan-result", "ok", "无所属轮次的历史成功", "无所属字段"),
    msg("user-new", "user", "发起新的画像"),
    request("assistant-new", "尚无结果的新画像", "get_data_profile"),
  ];
  await page.goto("/");
  const c = card(page, "尚无结果的新画像");
  await expect(c).toHaveCount(1);
  await expect(c).not.toHaveClass(/tool-step--ok(?:\s|$)/);
  await expect(c).not.toContainText("无所属轮次的历史成功");
  await expect(c).not.toContainText("无所属字段");
  await expect(c).toContainText(/未知|暂无|未收到|待确认|未确认/);
  expect(mutations(s.requests)).toEqual([]);
});

test("R4 result in a later user turn cannot complete an unanswered earlier tool", async ({ page }) => {
  const s = await install(page);
  s.states["conversation-a"].messages = [
    msg("user-old", "user", "旧画像"),
    request("assistant-old", "旧轮缺少执行结果", "get_data_profile"),
    msg("user-later", "user", "新报告"),
    request("assistant-later", "新轮报告有结果", "generate_report"),
    result("tool-later", "ok", "仅属于新报告的成功", "报告字段"),
  ];
  await page.goto("/");
  const c = card(page, "旧轮缺少执行结果");
  await expect(c).not.toHaveClass(/tool-step--ok(?:\s|$)/);
  await expect(c).not.toContainText("仅属于新报告的成功");
  await expect(c).toContainText(/未知|暂无|未收到|待确认|未确认/);
  await expect(card(page, "新轮报告有结果")).toContainText("仅属于新报告的成功");
  expect(mutations(s.requests)).toEqual([]);
});
