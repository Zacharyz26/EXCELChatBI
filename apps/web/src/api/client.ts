// 后端 API 客户端
import type {
  ChatStreamEvent,
  ConversationDetail,
  IngestResponse,
  KBOverview,
  UploadResponse,
  WorkspaceConversation,
  WorkspaceDataset,
  WorkspaceProject,
} from "@/types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

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

/** 重命名历史对话。 */
export async function updateConversation(
  conversationId: string,
  title: string,
): Promise<WorkspaceConversation> {
  const resp = await fetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 删除历史对话（级联删除其消息与工件）。 */
export async function deleteConversation(conversationId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) return asError(resp);
}

/** 读取一个对话的消息和工件快照。 */
export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  const resp = await fetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 重命名数据集显示名。 */
export async function updateDataset(
  datasetRef: string,
  filename: string,
): Promise<WorkspaceDataset> {
  const resp = await fetch(`${API_BASE}/datasets/${encodeURIComponent(datasetRef)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 删除数据集。被对话引用且未 force 时后端返回 409（误删保护），此处转为 warning。 */
export async function deleteDataset(
  datasetRef: string,
  force = false,
): Promise<{ deleted: boolean; warning?: string }> {
  const url = `${API_BASE}/datasets/${encodeURIComponent(datasetRef)}${force ? "?force=true" : ""}`;
  const resp = await fetch(url, { method: "DELETE" });
  if (resp.status === 409) {
    const body = await resp.json().catch(() => null);
    return {
      deleted: false,
      warning: typeof body?.detail === "string" ? body.detail : "数据集正在被使用。",
    };
  }
  if (!resp.ok) return asError(resp);
  return { deleted: true };
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

export { API_BASE };
