// 后端 API 客户端 + SSE 流式封装骨架
import type { ChatRequest, UploadResponse } from "@/types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

/** 发起对话，返回 SSE 流（逐片段回调）。 */
export async function streamChat(
  _req: ChatRequest,
  _onChunk: (text: string) => void,
): Promise<void> {
  // TODO: fetch + ReadableStream 解析 SSE，逐片段回调 onChunk
  throw new Error("TODO: 实现 SSE 流式对话");
}

/** 上传 Excel，返回数据集引用。 */
export async function uploadExcel(_file: File): Promise<UploadResponse> {
  // TODO: POST multipart/form-data 到 `${API_BASE}/upload/excel`
  throw new Error("TODO: 实现 Excel 上传");
}

export { API_BASE };
