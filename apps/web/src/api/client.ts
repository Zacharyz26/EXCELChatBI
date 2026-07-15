// 后端 API 客户端
import type {
  ChartResponse,
  ChatStreamEvent,
  ConversationDetail,
  IngestResponse,
  KBOverview,
  KBQueryResponse,
  ReportRequest,
  ReportResponse,
  StatsKind,
  StatsResponse,
  UploadResponse,
  WorkspaceConversation,
  WorkspaceDataset,
  WorkspaceProject,
} from "@/types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

/** 把后端技术性校验报错兜底翻译成用户能懂的中文（前端预校验漏网时的最后一道友好层）。 */
export function translateError(msg: string): string {
  const m = msg || "";
  if (/features/.test(m) && /(non-empty|too short|minItems|minimum)/i.test(m)) {
    return "回归分析需至少选择 1 个自变量";
  }
  if (/columns/.test(m) && /(non-empty|too short|minItems|minimum)/i.test(m)) {
    return "相关性分析需至少选择 2 列";
  }
  if (/不是数值型|not numeric/i.test(m)) return "所选列不是数值型，请改选数值列";
  if (/method=stl|需要提供\s*period|need.*period/i.test(m)) return "STL 方法需填写季节周期";
  // 已是中文的后端提示（样本量不足、列不存在等）直接透传
  if (/[一-龥]/.test(m)) return m;
  // 其余英文技术错误兜底
  return "参数不完整或不合法，请检查填写";
}

async function asError(resp: Response): Promise<never> {
  let detail = `${resp.status} ${resp.statusText}`;
  try {
    const body = await resp.json();
    if (typeof body?.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body?.detail)) {
      detail = body.detail
        .map((item: { msg?: unknown }) => String(item?.msg ?? "参数不合法"))
        .join("；");
    }
  } catch {
    /* 忽略非 JSON 响应 */
  }
  throw new Error(detail);
}

/** 上传 Excel，返回数据集引用与数据画像。 */
export async function uploadExcel(
  file: File,
  workspace?: { projectId: string; conversationId: string },
): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  if (workspace) {
    form.append("project_id", workspace.projectId);
    form.append("conversation_id", workspace.conversationId);
  }
  const resp = await fetch(`${API_BASE}/upload/excel`, { method: "POST", body: form });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 读取全部项目。 */
export async function listProjects(): Promise<WorkspaceProject[]> {
  const resp = await fetch(`${API_BASE}/projects`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 创建一个项目。 */
export async function createProject(name: string): Promise<WorkspaceProject> {
  const resp = await fetch(`${API_BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 读取项目内的历史对话。 */
export async function listConversations(
  projectId: string,
): Promise<WorkspaceConversation[]> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 在项目内创建新对话。 */
export async function createConversation(
  projectId: string,
  title = "新对话",
): Promise<WorkspaceConversation> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 读取一个对话的消息和工件快照。 */
export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  const resp = await fetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 读取项目内登记的数据集。 */
export async function listDatasets(projectId: string): Promise<WorkspaceDataset[]> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/datasets`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/**
 * 通过 fetch 消费 POST SSE。原生 EventSource 不支持 POST，因此在这里解析事件帧；
 * 支持代理常见的 CRLF、分块边界和多行 data。
 */
export async function streamChat(
  conversationId: string,
  message: string,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ conversation_id: conversationId, message }),
  });
  if (!resp.ok) return asError(resp);
  if (!resp.body) throw new Error("浏览器未提供可读取的流式响应");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      emitSseBlock(buffer.slice(0, boundary), onEvent);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }

  if (buffer.trim()) emitSseBlock(buffer, onEvent);
}

function emitSseBlock(block: string, onEvent: (event: ChatStreamEvent) => void): void {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return;

  const raw = dataLines.join("\n");
  let data: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(raw);
    data = parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : { value: parsed };
  } catch {
    data = { value: raw };
  }
  onEvent({ event: eventName, data });
}

/** 基于已上传数据集，请求自动出图（ECharts 配置）。 */
export async function analyze(datasetRef: string): Promise<ChartResponse> {
  const resp = await fetch(`${API_BASE}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset_ref: datasetRef }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 统计分析：趋势/异常/回归，可选附带 LLM 中文解读（喂模型的只有摘要，红线1）。 */
export async function analyzeStats(
  datasetRef: string,
  kind: StatsKind,
  params: Record<string, unknown>,
  interpret: boolean,
): Promise<StatsResponse> {
  const resp = await fetch(`${API_BASE}/analyze/stats`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset_ref: datasetRef, kind, params, interpret }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 生成报告：后端重跑分析组装 Markdown/PDF，返回下载链接。 */
export async function generateReport(body: ReportRequest): Promise<ReportResponse> {
  const resp = await fetch(`${API_BASE}/analyze/report`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 把后端相对下载路径拼成经代理可访问的完整 URL。 */
export function fileUrl(path: string): string {
  return `${API_BASE}${path}`;
}

/** 摄入样例知识库（服务端目录 docs/kb_samples）。 */
export async function ingestSamples(): Promise<IngestResponse> {
  const resp = await fetch(`${API_BASE}/kb/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: "docs/kb_samples" }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 知识库概览：片段数、来源文件、主题（供展示与派生示例问题）。 */
export async function kbOverview(): Promise<KBOverview> {
  const resp = await fetch(`${API_BASE}/kb/overview`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 知识库中文提问，返回答案与引用。 */
export async function kbQuery(question: string): Promise<KBQueryResponse> {
  const resp = await fetch(`${API_BASE}/kb/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

export { API_BASE };
