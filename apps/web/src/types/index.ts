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

export type MemoryScope = "conversation" | "project" | "subject";
export type MemoryKind =
  | "field_alias"
  | "user_preference"
  | "confirmed_decision"
  | "entity_mapping"
  | "conversation_summary";
export type MemoryStatus = "active" | "conflict" | "superseded" | "deleted";
export type MemorySourceType =
  | "message"
  | "user_confirmation"
  | "artifact"
  | "evidence"
  | "invocation";

export interface WorkspaceMemoryLink {
  target_type: string;
  target_ref: string;
}

export interface WorkspaceMemory {
  memory_id: string;
  project_id: string;
  scope: MemoryScope;
  conversation_id: string | null;
  kind: MemoryKind;
  content_summary: string;
  source_type: MemorySourceType;
  confidence: number;
  valid_from: string;
  expires_at: string | null;
  version: number;
  status: MemoryStatus;
  supersedes_id: string | null;
  conflicts_with_id: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  links: WorkspaceMemoryLink[];
}

export interface MemoryListResponse {
  items: WorkspaceMemory[];
  total: number;
  offset: number;
  limit: number;
}

export interface MemoryRevisionInput {
  expected_version: number;
  content_summary: string;
  confidence: number;
  expires_at: string | null;
}

export interface MemoryMutationResponse {
  memory: WorkspaceMemory;
  outcome: "created" | "conflict" | "replayed" | "reused" | "revised";
}

export type LineageNodeType =
  | "dataset"
  | "analysis"
  | "artifact"
  | "evidence"
  | "claim";
export type LineageNodeStatus =
  | "active"
  | "deleted"
  | "succeeded"
  | "failed"
  | "unknown"
  | "running";
export type LineageRelation =
  | "derived_from"
  | "used_by"
  | "produced"
  | "profiled_as"
  | "included_in"
  | "substantiates"
  | "supports";

export interface LineageNode {
  node_id: string;
  node_type: LineageNodeType;
  resource_ref: string;
  label: string;
  status: LineageNodeStatus;
  conversation_id: string | null;
  run_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface LineageEdge {
  source: string;
  target: string;
  relation: LineageRelation;
}

export interface LineageIssue {
  code: string;
  count: number;
}

export interface LineageGraphResponse {
  project_id: string;
  nodes: LineageNode[];
  edges: LineageEdge[];
  graph_hash: string;
  integrity_status: "ok" | "degraded";
  issues: LineageIssue[];
  total_nodes: number;
  total_edges: number;
  truncated: boolean;
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

// ── v2.5 阶段 4：TaskRun 协作控制面 ──

export type AgentRunStatus =
  | "planning"
  | "waiting_user"
  | "running"
  | "verifying"
  | "paused"
  | "completed"
  | "blocked"
  | "failed"
  | "cancelled";

export type AgentStepStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "skipped"
  | "blocked";

export interface AgentRun {
  run_id: string;
  project_id: string;
  conversation_id: string;
  user_message_id: string;
  parent_run_id: string | null;
  goal: string;
  status: AgentRunStatus;
  state_version: number;
  plan_version: number;
  budget: Record<string, unknown>;
  usage: Record<string, unknown>;
  terminal_reason: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface AgentPlanStepDefinition {
  step_id: string;
  purpose: string;
  capability: string;
  dependencies: string[];
  expected_evidence?: string[];
  completion_conditions?: string[];
  fallback?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface AgentPlanDefinition {
  schema_version: number;
  summary: string;
  steps: AgentPlanStepDefinition[];
  assumptions: unknown[];
  clarifications: unknown[];
  [key: string]: unknown;
}

export interface AgentPlan {
  plan_id: string;
  version: number;
  reason: string | null;
  definition: AgentPlanDefinition;
  created_at: string;
}

export interface AgentTaskStep {
  step_id: string;
  persisted_step_id: string;
  position: number;
  status: AgentStepStatus;
  definition: AgentPlanStepDefinition;
  started_at: string | null;
  completed_at: string | null;
}

export interface AgentRunDetail {
  run: AgentRun;
  contract: Record<string, unknown> | null;
  plan: AgentPlan | null;
  steps: AgentTaskStep[];
  state: Record<string, unknown> | null;
}

export type AgentApprovalStatus =
  | "pending"
  | "approved"
  | "denied"
  | "consumed"
  | "revoked";

export interface AgentApproval {
  approval_id: string;
  run_id: string;
  plan_id: string;
  plan_version: number;
  step_id: string;
  tool_name: string;
  tool_schema_hash: string;
  parameter_summary_hash: string;
  risk_level: "high" | "critical";
  status: AgentApprovalStatus;
  version: number;
  expires_at: string;
  decision_reason: string | null;
  requested_at: string;
  updated_at: string;
  decided_at: string | null;
  consumed_at: string | null;
}

export interface AgentTaskEvent {
  schema_version: "2.0";
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface AgentRunEventsResponse {
  run_id: string;
  events: AgentTaskEvent[];
  last_sequence: number;
}

export interface AgentClarification {
  question_id: string;
  question: string;
  reason: string;
  about: string;
  resume_token: string;
  answer_schema: Record<string, unknown>;
  sequence: number;
}

export interface AgentControlResponse {
  run: AgentRun;
  event: AgentTaskEvent;
  replayed: boolean;
}

export interface AgentPlanRevisionResponse extends AgentControlResponse {
  plan: AgentPlan;
  steps: AgentTaskStep[];
}

export interface AgentApprovalDecisionResponse extends AgentControlResponse {
  approval: AgentApproval;
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
  dependencies?: string[];
}

/** 正在流式进行的一轮 Agent 回复中的一个卡片。 */
export type LiveTurnItem =
  | { kind: "text"; id: string; content: string }
  | { kind: "understanding"; id: string; text: string }
  | {
    kind: "tools";
    id: string;
    steps: ToolStep[];
    source?: "task_plan" | "tool_calls";
    planVersion?: number;
  }
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
