import { useState } from "react";
import { ExcelUpload } from "@/components/ExcelUpload";
import { EChartsRenderer } from "@/components/EChartsRenderer";
import { StatsPanel } from "@/components/StatsPanel";
import { ReportPanel } from "@/components/ReportPanel";
import { KnowledgeQA } from "@/components/KnowledgeQA";
import { analyze } from "@/api/client";
import type { ChartResponse, DataProfile } from "@/types";

/** 应用根组件：上传 Excel → 展示画像 → 生成图表 → 渲染（第一个垂直切片）。 */
export default function App() {
  const [profile, setProfile] = useState<DataProfile | null>(null);
  const [chart, setChart] = useState<ChartResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onAnalyze() {
    if (!profile) return;
    setLoading(true);
    setError(null);
    try {
      setChart(await analyze(profile.dataset_ref));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: 24, fontFamily: "system-ui" }}>
      <h1>ChatBI 智能体 · Excel 自动分析</h1>

      <ExcelUpload
        onUploaded={(res) => {
          setProfile(res.profile);
          setChart(null);
          setError(null);
        }}
      />

      {profile && (
        <section>
          <h2>数据画像（{profile.row_count} 行 · {profile.column_count} 列）</h2>
          <table style={{ borderCollapse: "collapse", fontSize: 13, border: "1px solid #ccc" }}>
            <thead>
              <tr>
                <th>列名</th><th>类型</th><th>空值率</th><th>distinct</th><th>min</th><th>max</th><th>均值</th>
              </tr>
            </thead>
            <tbody>
              {profile.columns.map((c) => (
                <tr key={c.name}>
                  <td>{c.name}</td><td>{c.dtype}</td><td>{(c.null_ratio * 100).toFixed(1)}%</td>
                  <td>{c.distinct_count}</td><td>{c.min ?? "-"}</td><td>{c.max ?? "-"}</td><td>{c.mean ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            <button onClick={onAnalyze} disabled={loading}>
              {loading ? "正在分析…" : "生成图表"}
            </button>
          </p>
        </section>
      )}

      {error && <p style={{ color: "crimson" }}>出错：{error}</p>}
      {chart && (
        <section>
          <h2>图表（类型：{chart.chart_type}）</h2>
          <EChartsRenderer option={chart.option} chartId={chart.chart_id} />
        </section>
      )}

      {profile && <StatsPanel profile={profile} />}
      {profile && <ReportPanel profile={profile} />}

      <KnowledgeQA />
    </div>
  );
}
