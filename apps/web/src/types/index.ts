// 前后端共享的前端类型定义骨架

export interface ChatRequest {
  session_id: string;
  message: string;
  image_refs?: string[];
}

// SSE 流式片段：token | step | chart | error
export interface ChatChunk {
  type: "token" | "step" | "chart" | "error";
  data: string;
}

export interface UploadResponse {
  dataset_ref: string;
}
