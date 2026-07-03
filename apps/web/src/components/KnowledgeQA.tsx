// 知识库问答（F1）：提问 → 显示答案与引用来源（红线6）
import { useState } from "react";
import { ingestSamples, kbQuery } from "@/api/client";
import type { KBQueryResponse } from "@/types";

export function KnowledgeQA() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<KBQueryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [info, setInfo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onIngest() {
    setError(null);
    try {
      const r = await ingestSamples();
      setInfo(`已摄入 ${r.ingested_docs} 篇文档，共 ${r.total_chunks} 个片段`);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function onAsk() {
    if (!question.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setResult(await kbQuery(question));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section style={{ margin: "24px 0", borderTop: "1px solid #eee", paddingTop: 16 }}>
      <h2>知识库问答</h2>
      <p>
        <button onClick={onIngest}>摄入样例知识库</button>
        {info && <span style={{ marginLeft: 8, color: "#2a7" }}>{info}</span>}
      </p>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          style={{ flex: 1 }}
          value={question}
          placeholder="用中文提问，如：活跃用户怎么定义？"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onAsk()}
        />
        <button onClick={onAsk} disabled={loading}>
          {loading ? "检索中…" : "提问"}
        </button>
      </div>

      {error && <p style={{ color: "crimson" }}>出错：{error}</p>}
      {result && (
        <div style={{ marginTop: 12 }}>
          <p><strong>答案：</strong>{result.answer}</p>
          {result.citations.length > 0 && (
            <div>
              <strong>引用来源：</strong>
              <ol>
                {result.citations.map((c, i) => (
                  <li key={i}>
                    <code>{c.source}</code>
                    {c.section ? `（${c.section}）` : ""}：{c.snippet}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
