// 统计分析面板（/analyze/stats）：趋势/异常/回归 + 中文解读
// 红线说明：前端可渲染后端返回的完整明细（逐行分量、异常原值）；红线1 只约束"不进 LLM"。
import { useMemo, useState } from "react";
import { analyzeStats } from "@/api/client";
import { EChartsRenderer } from "@/components/EChartsRenderer";
import type {
  AnomalyResult,
  DataProfile,
  RegressionResult,
  StatsKind,
  StatsResponse,
  TrendResult,
} from "@/types";

interface Props {
  profile: DataProfile;
}

const ANOMALY_METHODS = ["iqr", "3sigma", "isolation_forest", "stl"] as const;

export function StatsPanel({ profile }: Props) {
  const cols = useMemo(() => profile.columns.map((c) => c.name), [profile]);
  // 数值/时间列的启发式默认值，方便一键跑通
  const numericCols = useMemo(
    () => profile.columns.filter((c) => ["int", "float"].includes(c.dtype)).map((c) => c.name),
    [profile],
  );

  const [kind, setKind] = useState<StatsKind>("trend");
  const [valueCol, setValueCol] = useState(numericCols[0] ?? cols[0] ?? "");
  const [timeCol, setTimeCol] = useState(cols[0] ?? "");
  const [period, setPeriod] = useState("");
  const [method, setMethod] = useState<string>("iqr");
  const [target, setTarget] = useState(numericCols[0] ?? cols[0] ?? "");
  const [features, setFeatures] = useState<string[]>([]);
  const [interpret, setInterpret] = useState(true);

  const [resp, setResp] = useState<StatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggleFeature(name: string) {
    setFeatures((f) => (f.includes(name) ? f.filter((x) => x !== name) : [...f, name]));
  }

  function buildParams(): Record<string, unknown> {
    if (kind === "trend") {
      const p: Record<string, unknown> = { value_col: valueCol, time_col: timeCol };
      if (period) p.period = Number(period);
      p.forecast_horizon = 3;
      return p;
    }
    if (kind === "anomaly") {
      const p: Record<string, unknown> = { value_col: valueCol, method };
      if (method === "stl") {
        p.time_col = timeCol;
        if (period) p.period = Number(period);
      }
      return p;
    }
    return { target, features, kind: "ols" };
  }

  async function onRun() {
    setLoading(true);
    setError(null);
    try {
      setResp(await analyzeStats(profile.dataset_ref, kind, buildParams(), interpret));
    } catch (e) {
      setResp(null);
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section style={{ margin: "24px 0", borderTop: "1px solid #eee", paddingTop: 16 }}>
      <h2>统计分析</h2>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
        <label>
          类型{" "}
          <select value={kind} onChange={(e) => setKind(e.target.value as StatsKind)}>
            <option value="trend">趋势分析</option>
            <option value="anomaly">异常检测</option>
            <option value="regression">回归分析</option>
          </select>
        </label>

        {(kind === "trend" || kind === "anomaly") && (
          <label>
            数值列{" "}
            <select value={valueCol} onChange={(e) => setValueCol(e.target.value)}>
              {cols.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
        )}

        {(kind === "trend" || (kind === "anomaly" && method === "stl")) && (
          <label>
            时间列{" "}
            <select value={timeCol} onChange={(e) => setTimeCol(e.target.value)}>
              {cols.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
        )}

        {(kind === "trend" || (kind === "anomaly" && method === "stl")) && (
          <label>
            季节周期{" "}
            <input
              style={{ width: 64 }}
              value={period}
              placeholder="如 12"
              onChange={(e) => setPeriod(e.target.value.replace(/[^0-9]/g, ""))}
            />
          </label>
        )}

        {kind === "anomaly" && (
          <label>
            方法{" "}
            <select value={method} onChange={(e) => setMethod(e.target.value)}>
              {ANOMALY_METHODS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>
        )}

        {kind === "regression" && (
          <label>
            因变量{" "}
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              {cols.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
        )}

        <label>
          <input type="checkbox" checked={interpret} onChange={(e) => setInterpret(e.target.checked)} />{" "}
          附带中文解读
        </label>

        <button onClick={onRun} disabled={loading}>
          {loading ? "分析中…" : "运行分析"}
        </button>
      </div>

      {kind === "regression" && (
        <div style={{ marginTop: 8, fontSize: 13 }}>
          自变量（可多选）：
          {cols
            .filter((c) => c !== target)
            .map((c) => (
              <label key={c} style={{ marginLeft: 8 }}>
                <input type="checkbox" checked={features.includes(c)} onChange={() => toggleFeature(c)} /> {c}
              </label>
            ))}
        </div>
      )}

      {error && <p style={{ color: "crimson" }}>出错：{error}</p>}

      {resp && (
        <div style={{ marginTop: 16 }}>
          {resp.kind === "trend" && <TrendView result={resp.result as TrendResult} />}
          {resp.kind === "anomaly" && <AnomalyView result={resp.result as AnomalyResult} />}
          {resp.kind === "regression" && <RegressionView result={resp.result as RegressionResult} />}
          {resp.interpretation && (
            <div style={{ marginTop: 12, padding: 12, background: "#f4f8ff", borderLeft: "3px solid #4a7" }}>
              <strong>中文解读：</strong>
              <p style={{ margin: "6px 0 0", whiteSpace: "pre-wrap" }}>{resp.interpretation}</p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ── 趋势：折线（趋势分量 + 预测）复用 EChartsRenderer ──
function TrendView({ result }: { result: TrendResult }) {
  const n = result.n;
  const xLabels = result.time ?? Array.from({ length: n }, (_, i) => String(i + 1));
  const fcLabels = result.forecast.map((_, i) => `T+${i + 1}`);
  const trendSeries = [...result.points.trend, ...result.forecast.map(() => null)];
  const forecastSeries = [...result.points.trend.map(() => null), ...result.forecast];
  // 让预测线与趋势线相接：预测段首点接上趋势末点
  if (result.points.trend.length > 0) forecastSeries[n - 1] = result.points.trend[n - 1];

  const option = {
    title: { text: `趋势分析（${result.method}）` },
    tooltip: { trigger: "axis" },
    legend: { data: ["趋势", "预测"] },
    xAxis: { type: "category", data: [...xLabels, ...fcLabels] },
    yAxis: { type: "value" },
    series: [
      { name: "趋势", type: "line", smooth: true, data: trendSeries },
      { name: "预测", type: "line", data: forecastSeries, lineStyle: { type: "dashed" } },
    ],
  };

  return (
    <div>
      <p style={{ fontSize: 14 }}>
        方向：<strong>{result.direction}</strong> · 斜率 {fmt(result.slope)} · 季节强度{" "}
        {result.seasonality_strength == null ? "—" : fmt(result.seasonality_strength)} · 样本 {n}
      </p>
      <EChartsRenderer option={option} chartId={`trend-${result.method}`} />
    </div>
  );
}

// ── 异常：概览 + 异常点表（前端渲染完整明细）──
function AnomalyView({ result }: { result: AnomalyResult }) {
  const rate = result.n_total ? ((result.n_anomalies / result.n_total) * 100).toFixed(1) : "0";
  return (
    <div>
      <p style={{ fontSize: 14 }}>
        方法 {result.method} · 样本 {result.n_total} · 异常 <strong>{result.n_anomalies}</strong>（{rate}%）
      </p>
      {result.anomalies.length > 0 && (
        <table style={{ borderCollapse: "collapse", fontSize: 13, border: "1px solid #ccc" }}>
          <thead>
            <tr><th style={cell}>序号</th><th style={cell}>值</th><th style={cell}>异常分</th><th style={cell}>时间</th></tr>
          </thead>
          <tbody>
            {result.anomalies.map((a) => (
              <tr key={a.index}>
                <td style={cell}>{a.index}</td>
                <td style={cell}>{fmt(a.value)}</td>
                <td style={cell}>{fmt(a.score)}</td>
                <td style={cell}>{a.time ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── 回归：拟合优度 + 系数表 ──
function RegressionView({ result }: { result: RegressionResult }) {
  return (
    <div>
      <p style={{ fontSize: 14 }}>
        {result.kind.toUpperCase()} · R² {fmt(result.r_squared)} · 调整 R² {fmt(result.adj_r_squared)} ·
        样本 {result.n_obs} · 模型 p 值 {fmt(result.model_pvalue)}
      </p>
      <table style={{ borderCollapse: "collapse", fontSize: 13, border: "1px solid #ccc" }}>
        <thead>
          <tr><th style={cell}>变量</th><th style={cell}>系数</th><th style={cell}>标准误</th><th style={cell}>p 值</th><th style={cell}>显著</th></tr>
        </thead>
        <tbody>
          {result.coefficients.map((c) => (
            <tr key={c.name}>
              <td style={cell}>{c.name}</td>
              <td style={cell}>{fmt(c.coef)}</td>
              <td style={cell}>{fmt(c.std_err)}</td>
              <td style={cell}>{fmt(c.p_value)}</td>
              <td style={cell}>{c.significant ? "✓" : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const cell: React.CSSProperties = { border: "1px solid #ccc", padding: "2px 8px" };

function fmt(v: number | null): string {
  if (v == null) return "—";
  return Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.001) ? v.toExponential(3) : String(Number(v.toFixed(4)));
}
