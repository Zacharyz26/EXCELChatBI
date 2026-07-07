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

// ── 统计分析（/analyze/stats）──

export type StatsKind = "trend" | "anomaly" | "regression";

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

export type StatsResult = TrendResult | AnomalyResult | RegressionResult;

export interface StatsResponse {
  kind: StatsKind;
  result: StatsResult;
  interpretation: string | null;
}
