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
