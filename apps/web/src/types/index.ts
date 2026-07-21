// 前后端共享的前端类型定义

export interface ColumnProfile {
  name: string;
  dtype: string;
  null_ratio: number;
  distinct_count: number;
  min: number | null;
  max: number | null;
  mean: number | null;
  std: number | null;
  median: number | null;
  sample_values: string[];
}

export interface DataProfile {
  dataset_ref: string;
  row_count: number;
  column_count: number;
  columns: ColumnProfile[];
  sample_rows: Record<string, unknown>[];
}

export interface UploadResponse {
  dataset_ref: string;
  profile: DataProfile;
  messages?: WorkspaceMessage[] | null;
  artifact?: WorkspaceArtifact | null;
}

// ── 对话工作区（阶段 1）──

export interface WorkspaceProject {
  id: string;
  name: string;
  created_at: string;
}

export interface WorkspaceConversation {
  id: string;
  project_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceDataset {
  ref: string;
  project_id: string;
  filename: string;
  profile: DataProfile;
  parent_ref: string | null;
  transform: Record<string, unknown> | null;
  created_at: string;
}

export interface WorkspaceMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | string;
  content: string;
  tool_calls: Record<string, unknown>[] | null;
  created_at: string;
}

export interface WorkspaceArtifact {
  id: string;
  conversation_id: string;
  message_id: string;
  type: string;
  payload: Record<string, unknown> | null;
  file_ref: string | null;
  source_tool: string | null;
  params: Record<string, unknown> | null;
  dataset_ref: string | null;
  created_at: string;
}

export interface ConversationDetail {
  conversation: WorkspaceConversation;
  messages: WorkspaceMessage[];
  artifacts: WorkspaceArtifact[];
}

export interface ChatStreamEvent {
  event: string;
  data: Record<string, unknown>;
}

// ── 对话式 Agent 实时轮次（阶段 3，SSE 事件 14.5.3 → 消息卡片）──

/** 一次工具调用步骤（计划卡/执行卡合一渲染，随 tool_start/tool_end 更新）。 */
export interface ToolStep {
  id: string;
  tool: string;
  label: string;
  status: "pending" | "running" | "ok" | "error";
  /** 人话参数摘要（后端 _humanize_args 生成，默认展示） */
  fields?: string;
  /** 原始入参 JSON（仅供“调整参数”表单预填） */
  argsPreview?: string;
  summary?: string;
  message?: string;
}

/** 正在流式进行的一轮 Agent 回复中的一个卡片。 */
export type LiveTurnItem =
  | { kind: "text"; id: string; content: string }
  | { kind: "understanding"; id: string; text: string }
  | { kind: "tools"; id: string; steps: ToolStep[] }
  | { kind: "artifact"; id: string; artifact: WorkspaceArtifact };

export interface IngestResponse {
  ingested_docs: number;
  chunks: number;
  total_chunks: number;
  created: string[];
  updated: string[];
  skipped: string[];
  deleted: string[];
}

export interface KBDocument {
  document_id: string;
  source: string;
  content_hash: string;
  version: number;
  updated_at: string;
  chunk_count: number;
}

export interface KBOverview {
  chunk_count: number;
  sources: string[];
  topics: string[];
  documents: KBDocument[];
}
