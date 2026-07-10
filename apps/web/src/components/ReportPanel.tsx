// 报告面板：勾选要包含的图表/统计 → 生成报告 → 下载 PDF / Markdown
// 后端重跑分析拿真实结果、解读来自 stats_interpreter（已门控出口），前端只提交选择。
import { useMemo, useState } from "react";
import { fileUrl, generateReport, translateError } from "@/api/client";
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

  // 要求数值的字段（y/趋势数值/回归因变量·自变量）只从数值列取；维度列（x/时间）用全部列
  const [inclChart, setInclChart] = useState(true);
  const [chartX, setChartX] = useState(cols[0] ?? "");
  const [chartY, setChartY] = useState(numericCols[0] ?? "");

  const [inclTrend, setInclTrend] = useState(true);
  const [trendVal, setTrendVal] = useState(numericCols[0] ?? "");
  const [trendTime, setTrendTime] = useState(cols[0] ?? "");
  const [trendPeriod, setTrendPeriod] = useState("");

  const [inclReg, setInclReg] = useState(false);
  const [regTarget, setRegTarget] = useState(numericCols[0] ?? "");
  const [regFeatures, setRegFeatures] = useState<string[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ReportResponse | null>(null);

  function toggleFeature(name: string) {
    setRegFeatures((f) => (f.includes(name) ? f.filter((x) => x !== name) : [...f, name]));
  }

  // 逐项校验：明确指出是哪个分析缺什么参数（返回中文提示数组）
  function reportErrors(): string[] {
    const errs: string[] = [];
    if (!inclChart && !inclTrend && !inclReg) errs.push("请至少勾选一项要包含的分析");
    if (inclChart && (!chartX || !chartY)) errs.push("柱状图需选择 x 轴列与 y（数值）列");
    if (inclTrend && (!trendVal || !trendTime)) errs.push("趋势分析需选择数值列与时间列");
    if (inclReg && !regTarget) errs.push("回归分析需选择因变量");
    if (inclReg && regFeatures.length < 1) errs.push("回归分析需至少选择 1 个自变量");
    return errs;
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
      setError(translateError((e as Error).message));
    } finally {
      setLoading(false);
    }
  }

  const errs = reportErrors();

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
              y（数值）{" "}
              <select value={chartY} onChange={(e) => setChartY(e.target.value)}>
                {numericCols.map((c) => <option key={c} value={c}>{c}</option>)}
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
                {numericCols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              时间{" "}
              <select value={trendTime} onChange={(e) => setTrendTime(e.target.value)}>
                {cols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              周期<span style={{ color: "#888", fontSize: 12 }}>（选填）</span>{" "}
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
                {numericCols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>{" "}
              自变量（至少选 1 个）：
              {numericCols.filter((c) => c !== regTarget).map((c) => (
                <label key={c} style={{ marginLeft: 6 }}>
                  <input type="checkbox" checked={regFeatures.includes(c)} onChange={() => toggleFeature(c)} /> {c}
                </label>
              ))}
            </span>
          )}
        </div>
      </div>

      {errs.length > 0 && (
        <ul style={{ margin: "10px 0 0", color: "#b26a00", fontSize: 13 }}>
          {errs.map((m) => <li key={m}>{m}</li>)}
        </ul>
      )}

      <p>
        <button onClick={onGenerate} disabled={loading || errs.length > 0}>
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
