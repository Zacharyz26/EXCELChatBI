// Excel 上传：选择文件 → 上传 → 回传数据画像（先确认画像再分析，设计文档 5.1）
import { useState, type ChangeEvent } from "react";
import { uploadExcel } from "@/api/client";
import type { UploadResponse } from "@/types";

interface Props {
  onUploaded: (res: UploadResponse) => void;
}

export function ExcelUpload({ onUploaded }: Props) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      onUploaded(await uploadExcel(file));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <section style={{ margin: "16px 0" }}>
      <input type="file" accept=".xlsx,.xls" onChange={onChange} disabled={uploading} />
      {uploading && <span> 上传解析中…</span>}
      {error && <p style={{ color: "crimson" }}>上传失败：{error}</p>}
    </section>
  );
}
