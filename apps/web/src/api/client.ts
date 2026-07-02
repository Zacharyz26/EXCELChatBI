// 后端 API 客户端
import type { ChartResponse, UploadResponse } from "@/types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

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

export { API_BASE };
