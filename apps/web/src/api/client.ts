// 后端 API 客户端
import type {
  ChartResponse,
  IngestResponse,
  KBOverview,
  KBQueryResponse,
  ReportRequest,
  ReportResponse,
  StatsKind,
  StatsResponse,
  UploadResponse,
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
    if (body?.detail) detail = body.detail;
  } catch {
    /* 忽略非 JSON 响应 */
  }
  throw new Error(detail);
}

/** 上传 Excel，返回数据集引用与数据画像。 */
export async function uploadExcel(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch(`${API_BASE}/upload/excel`, { method: "POST", body: form });
  if (!resp.ok) return asError(resp);
  return resp.json();
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
