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

function apiToken(): string {
  if (typeof window === "undefined") return "";
  return window.sessionStorage.getItem("chatbi.apiToken") ?? "";
}

/** Bearer token 只保存在当前浏览器会话；生产环境应由登录流程调用。 */
export function setApiToken(token: string): void {
  if (typeof window === "undefined") return;
  if (token.trim()) {
    window.sessionStorage.setItem("chatbi.apiToken", token.trim());
  } else {
    window.sessionStorage.removeItem("chatbi.apiToken");
  }
}

export function hasApiToken(): boolean {
  return apiToken().length > 0;
}

export async function getAuthMode(): Promise<"disabled" | "bearer"> {
  const resp = await fetch(`${API_BASE}/auth/config`);
  if (!resp.ok) return asError(resp);
  const body: unknown = await resp.json();
  if (
    !body
    || typeof body !== "object"
    || !["disabled", "bearer"].includes(String((body as { mode?: unknown }).mode))
  ) {
    throw new Error("服务端返回了未知认证模式");
  }
  return (body as { mode: "disabled" | "bearer" }).mode;
}

function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = apiToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
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
  const resp = await apiFetch(`${API_BASE}/upload/excel`, { method: "POST", body: form });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 读取全部项目。 */
export async function listProjects(): Promise<WorkspaceProject[]> {
  const resp = await apiFetch(`${API_BASE}/projects`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 创建一个项目。 */
export async function createProject(name: string): Promise<WorkspaceProject> {
  const resp = await apiFetch(`${API_BASE}/projects`, {
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
  const resp = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 在项目内创建新对话。 */
export async function createConversation(
  projectId: string,
  title = "新对话",
): Promise<WorkspaceConversation> {
  const resp = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations`, {
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
  const resp = await apiFetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 删除历史对话（级联删除其消息与工件）。 */
export async function deleteConversation(conversationId: string): Promise<void> {
  const resp = await apiFetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) return asError(resp);
}

/** 读取一个对话的消息和工件快照。 */
export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  const resp = await apiFetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 重命名数据集显示名。 */
export async function updateDataset(
  datasetRef: string,
  filename: string,
): Promise<WorkspaceDataset> {
  const resp = await apiFetch(`${API_BASE}/datasets/${encodeURIComponent(datasetRef)}`, {
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
  const resp = await apiFetch(url, { method: "DELETE" });
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
  const resp = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/datasets`);
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
  const resp = await apiFetch(`${API_BASE}/chat/stream`, {
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

/** 通过带认证的 fetch 下载工件，避免普通链接丢失 Authorization header。 */
export async function downloadFile(path: string): Promise<void> {
  const resp = await apiFetch(`${API_BASE}${path}`);
  if (!resp.ok) return asError(resp);
  const blobUrl = URL.createObjectURL(await resp.blob());
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = path.split("/").pop() || "download";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(blobUrl);
}

/** 摄入样例知识库（服务端目录 docs/kb_samples）。 */
export async function ingestSamples(): Promise<IngestResponse> {
  const resp = await apiFetch(`${API_BASE}/kb/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: "docs/kb_samples" }),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 知识库概览：片段数、来源文件、主题（供展示与派生示例问题）。 */
export async function kbOverview(): Promise<KBOverview> {
  const resp = await apiFetch(`${API_BASE}/kb/overview`);
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 从默认文档目录完整构建新索引并原子切换。 */
export async function rebuildKnowledgeBase(): Promise<IngestResponse> {
  const resp = await apiFetch(`${API_BASE}/kb/rebuild`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!resp.ok) return asError(resp);
  return resp.json();
}

/** 删除一个来源文档及其全部片段。 */
export async function deleteKnowledgeDocument(documentId: string): Promise<void> {
  const resp = await apiFetch(
    `${API_BASE}/kb/documents/${encodeURIComponent(documentId)}`,
    { method: "DELETE" },
  );
  if (!resp.ok) return asError(resp);
}

export { API_BASE };
