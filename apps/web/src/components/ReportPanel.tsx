// 报告面板：勾选要包含的图表/统计 → 生成报告 → 下载 PDF / Markdown
// 后端重跑分析拿真实结果、解读来自 stats_interpreter（已门控出口），前端只提交选择。
import { useMemo, useState } from "react";
import { fileUrl, generateReport } from "@/api/client";
import type { DataProfile, ReportChartSpec, ReportResponse, ReportStatSpec } from "@/types";

interface Props {
  profile: DataProfile;
}

export function ReportPanel({ profile }: Props) {
  const cols = useMemo(() => profile.columns.map((c) => c.name), [profile]);
  const numericCols = useMemo(
    () => profile.columns.filter((c) => ["int", "float"].includes(c.dtype)).map((c) => c.name),
    [profile],
  );

  const [title, setTitle] = useState("分析报告");
  const [interpret, setInterpret] = useState(true);

  const [inclChart, setInclChart] = useState(true);
  const [chartX, setChartX] = useState(cols[0] ?? "");
  const [chartY, setChartY] = useState(numericCols[0] ?? cols[0] ?? "");

  const [inclTrend, setInclTrend] = useState(true);
  const [trendVal, setTrendVal] = useState(numericCols[0] ?? cols[0] ?? "");
  const [trendTime, setTrendTime] = useState(cols[0] ?? "");
  const [trendPeriod, setTrendPeriod] = useState("");

  const [inclReg, setInclReg] = useState(false);
  const [regTarget, setRegTarget] = useState(numericCols[0] ?? cols[0] ?? "");
  const [regFeatures, setRegFeatures] = useState<string[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ReportResponse | null>(null);

  function toggleFeature(name: string) {
    setRegFeatures((f) => (f.includes(name) ? f.filter((x) => x !== name) : [...f, name]));
  }

  function buildRequest() {
    const charts: ReportChartSpec[] = [];
    if (inclChart) {
      charts.push({
        chart_type: "bar",
        encoding: { x: chartX, y: chartY, agg: "sum" },
        caption: `${chartY} by ${chartX}`,
      });
    }
    const stats: ReportStatSpec[] = [];
    if (inclTrend) {
      const params: Record<string, unknown> = {
        value_col: trendVal,
        time_col: trendTime,
        forecast_horizon: 3,
      };
      if (trendPeriod) params.period = Number(trendPeriod);
      stats.push({ kind: "trend", params, caption: "趋势分析" });
    }
    if (inclReg) {
      stats.push({
        kind: "regression",
        params: { target: regTarget, features: regFeatures, kind: "ols" },
        caption: "回归分析",
      });
    }
    return { dataset_ref: profile.dataset_ref, title, charts, stats, interpret };
  }

  async function onGenerate() {
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      setReport(await generateReport(buildRequest()));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section style={{ margin: "24px 0", borderTop: "1px solid #eee", paddingTop: 16 }}>
      <h2>报告导出</h2>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
        <label>
          标题{" "}
          <input value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          <input type="checkbox" checked={interpret} onChange={(e) => setInterpret(e.target.checked)} />{" "}
          附带中文解读
        </label>
      </div>

      <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8, fontSize: 14 }}>
        <div>
          <label>
            <input type="checkbox" checked={inclChart} onChange={(e) => setInclChart(e.target.checked)} />{" "}
            柱状图
          </label>
          {inclChart && (
            <span style={{ marginLeft: 8 }}>
              x{" "}
              <select value={chartX} onChange={(e) => setChartX(e.target.value)}>
                {cols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              y{" "}
              <select value={chartY} onChange={(e) => setChartY(e.target.value)}>
                {cols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </span>
          )}
        </div>

        <div>
          <label>
            <input type="checkbox" checked={inclTrend} onChange={(e) => setInclTrend(e.target.checked)} />{" "}
            趋势分析
          </label>
          {inclTrend && (
            <span style={{ marginLeft: 8 }}>
              数值{" "}
              <select value={trendVal} onChange={(e) => setTrendVal(e.target.value)}>
                {cols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              时间{" "}
              <select value={trendTime} onChange={(e) => setTrendTime(e.target.value)}>
                {cols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              周期{" "}
              <input
                style={{ width: 56 }}
                value={trendPeriod}
                placeholder="如12"
                onChange={(e) => setTrendPeriod(e.target.value.replace(/[^0-9]/g, ""))}
              />
            </span>
          )}
        </div>

        <div>
          <label>
            <input type="checkbox" checked={inclReg} onChange={(e) => setInclReg(e.target.checked)} />{" "}
            回归分析
          </label>
          {inclReg && (
            <span style={{ marginLeft: 8 }}>
              因变量{" "}
              <select value={regTarget} onChange={(e) => setRegTarget(e.target.value)}>
                {cols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              自变量：
              {cols.filter((c) => c !== regTarget).map((c) => (
                <label key={c} style={{ marginLeft: 6 }}>
                  <input type="checkbox" checked={regFeatures.includes(c)} onChange={() => toggleFeature(c)} /> {c}
                </label>
              ))}
            </span>
          )}
        </div>
      </div>

      <p>
        <button onClick={onGenerate} disabled={loading}>
          {loading ? "生成中…（渲染图表 + 排版 PDF，稍候）" : "生成报告"}
        </button>
      </p>

      {error && <p style={{ color: "crimson" }}>出错：{error}</p>}
      {report && (
        <div style={{ padding: 12, background: "#f4f8ff", borderLeft: "3px solid #4a7" }}>
          报告已生成：
          <a href={fileUrl(report.pdf_url)} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>
            下载 PDF
          </a>
          <a href={fileUrl(report.md_url)} target="_blank" rel="noreferrer" style={{ marginLeft: 12 }}>
            下载 Markdown
          </a>
        </div>
      )}
    </section>
  );
}
