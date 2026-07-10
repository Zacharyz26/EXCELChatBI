// 知识库问答（F1）：概览引导 + 示例问题 + 提问 → 显示答案与引用来源（红线6）
import { useEffect, useState } from "react";
import { ingestSamples, kbOverview, kbQuery } from "@/api/client";
import type { KBOverview, KBQueryResponse } from "@/types";

export function KnowledgeQA() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<KBQueryResponse | null>(null);
  const [overview, setOverview] = useState<KBOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [info, setInfo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 挂载时拉概览（若已摄入，直接展示能问什么）
  useEffect(() => {
    kbOverview().then(setOverview).catch(() => setOverview(null));
  }, []);

  // 示例问题：由真实小节标题派生，措辞贴合文档（红线6：不诱导编造）
  const examples = (overview?.topics ?? []).map((t) => `${t}怎么定义？`);

  async function refreshOverview() {
    try {
      setOverview(await kbOverview());
    } catch {
      /* 概览失败不影响主流程 */
    }
  }

  async function onIngest() {
    setError(null);
    try {
      const r = await ingestSamples();
      setInfo(`已摄入 ${r.ingested_docs} 篇文档，共 ${r.total_chunks} 个片段`);
      await refreshOverview();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function onAsk(q?: string) {
    const query = (q ?? question).trim();
    if (!query) return;
    setQuestion(query);
    setLoading(true);
    setError(null);
    try {
      setResult(await kbQuery(query));
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

      {overview && overview.topics.length > 0 && (
        <div style={{ margin: "8px 0", fontSize: 13, color: "#555" }}>
          <div>
            知识库包含：<strong>{overview.topics.join("、")}</strong>
            {overview.sources.length > 0 && `（来自 ${overview.sources.join("、")}）`}
          </div>
          <div style={{ marginTop: 6 }}>
            示例问题（点击提问）：
            {examples.map((q) => (
              <button
                key={q}
                onClick={() => onAsk(q)}
                disabled={loading}
                style={{
                  marginLeft: 6, marginBottom: 4, padding: "2px 8px", fontSize: 12,
                  border: "1px solid #cbd5e0", borderRadius: 12, background: "#f7fafc",
                  cursor: "pointer",
                }}
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <input
          style={{ flex: 1 }}
          value={question}
          placeholder="用中文提问，如：活跃用户怎么定义？"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onAsk()}
        />
        <button onClick={() => onAsk()} disabled={loading}>
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
