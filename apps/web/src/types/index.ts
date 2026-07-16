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

export interface ChartResponse {
  chart_id: string;
  chart_type: string;
  option: Record<string, unknown>; // ECharts option（数值来自真实数据）
}

export interface ChatRequest {
  session_id: string;
  message: string;
  image_refs?: string[];
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

export interface KBCitation {
  source: string;
  snippet: string;
  section?: string | null;
}

export interface KBQueryResponse {
  answer: string;
  citations: KBCitation[];
  is_empty: boolean;
}

export interface IngestResponse {
  ingested_docs: number;
  chunks: number;
  total_chunks: number;
}

export interface KBOverview {
  chunk_count: number;
  sources: string[];
  topics: string[];
}

// ── 统计分析（/analyze/stats）──

export type StatsKind = "trend" | "anomaly" | "regression" | "correlation";

export interface TrendResult {
  method: string;
  direction: string;
  slope: number | null;
  seasonality_strength: number | null;
  ma_window: number;
  n: number;
  time: string[] | null;
  points: {
    trend: (number | null)[];
    seasonal: (number | null)[];
    resid: (number | null)[];
  };
  forecast: (number | null)[];
}

export interface AnomalyPoint {
  index: number;
  value: number | null;
  score: number | null;
  time?: string;
}

export interface AnomalyResult {
  method: string;
  n_total: number;
  n_anomalies: number;
  anomalies: AnomalyPoint[];
}

export interface RegressionCoef {
  name: string;
  coef: number | null;
  std_err: number | null;
  p_value: number | null;
  significant: boolean;
}

export interface RegressionResult {
  kind: string;
  r_squared: number | null;
  adj_r_squared: number | null;
  n_obs: number;
  model_pvalue: number | null;
  coefficients: RegressionCoef[];
}

export interface CorrelationPair {
  a: string;
  b: string;
  corr: number | null;
  p_value: number | null;
  significant: boolean;
}

export interface CorrelationResult {
  method: string;
  columns: string[];
  n_obs: number;
  matrix: (number | null)[][];
  top_pairs: CorrelationPair[];
}

export type StatsResult = TrendResult | AnomalyResult | RegressionResult | CorrelationResult;

export interface StatsResponse {
  kind: StatsKind;
  result: StatsResult;
  interpretation: string | null;
}

// ── 报告导出（/analyze/report）──

export interface ReportChartSpec {
  chart_type: string;
  encoding: Record<string, unknown>;
  caption?: string;
}

export interface ReportStatSpec {
  kind: StatsKind;
  params: Record<string, unknown>;
  caption?: string;
}

export interface ReportRequest {
  dataset_ref: string;
  title: string;
  charts: ReportChartSpec[];
  stats: ReportStatSpec[];
  interpret: boolean;
}

export interface ReportResponse {
  report_id: string;
  md_url: string;
  pdf_url: string;
}
