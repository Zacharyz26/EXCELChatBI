// 统计分析面板（/analyze/stats）：趋势/异常/回归 + 中文解读
// 红线说明：前端可渲染后端返回的完整明细（逐行分量、异常原值）；红线1 只约束"不进 LLM"。
import { useMemo, useRef, useState } from "react";
import { analyzeStats, translateError } from "@/api/client";
import { EChartsRenderer } from "@/components/EChartsRenderer";
import type {
  AnomalyResult,
  CorrelationResult,
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
const KIND_LABELS: Record<StatsKind, string> = {
  trend: "趋势分析",
  anomaly: "异常检测",
  regression: "回归分析",
  correlation: "相关性分析",
};

export function StatsPanel({ profile }: Props) {
  const cols = useMemo(() => profile.columns.map((c) => c.name), [profile]);
  // 数值/时间列的启发式默认值，方便一键跑通
  const numericCols = useMemo(
    () => profile.columns.filter((c) => ["int", "float"].includes(c.dtype)).map((c) => c.name),
    [profile],
  );

  const [kind, setKind] = useState<StatsKind>("trend");
  // 要求数值的字段只从数值列取默认值（选择器也只列数值列，从源头避免"不是数值型"）
  const [valueCol, setValueCol] = useState(numericCols[0] ?? "");
  const [timeCol, setTimeCol] = useState(cols[0] ?? "");
  const [period, setPeriod] = useState("");
  const [method, setMethod] = useState<string>("iqr");
  const [trendMethod, setTrendMethod] = useState<string>("auto");
  const [target, setTarget] = useState(numericCols[0] ?? "");
  const [features, setFeatures] = useState<string[]>([]);
  const [corrMethod, setCorrMethod] = useState<string>("pearson");
  const [corrCols, setCorrCols] = useState<string[]>(numericCols.slice(0, 3));
  const [interpret, setInterpret] = useState(true);

  const [resp, setResp] = useState<StatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 类型切换或重复运行时使旧请求失效，避免迟到响应覆盖当前类型的状态。
  const requestIdRef = useRef(0);

  function onKindChange(nextKind: StatsKind) {
    requestIdRef.current += 1;
    setKind(nextKind);
    setResp(null);
    setError(null);
    setLoading(false);
  }

  function onTargetChange(nextTarget: string) {
    // 因变量不能同时留在自变量中；切换参数时旧结果与旧请求一并失效。
    requestIdRef.current += 1;
    setTarget(nextTarget);
    setFeatures((current) => current.filter((feature) => feature !== nextTarget));
    setResp(null);
    setError(null);
    setLoading(false);
  }

  function toggleFeature(name: string) {
    setFeatures((f) => (f.includes(name) ? f.filter((x) => x !== name) : [...f, name]));
  }

  function toggleCorrCol(name: string) {
    setCorrCols((c) => (c.includes(name) ? c.filter((x) => x !== name) : [...c, name]));
  }

  // 前端预校验：返回中文提示（不完整时禁用按钮），null 表示可运行
  function validationError(): string | null {
    if (kind === "trend") {
      if (!valueCol) return "趋势分析需选择数值列（该数据集无数值列）";
      if (!timeCol) return "趋势分析需选择时间列";
      return null; // 季节周期选填：不填走移动平均，填了启用 STL
    }
    if (kind === "anomaly") {
      if (!valueCol) return "异常检测需选择数值列（该数据集无数值列）";
      if (method === "stl") {
        if (!timeCol) return "STL 异常检测需选择时间列";
        if (!period) return "STL 异常检测需填写季节周期";
      }
      return null;
    }
    if (kind === "correlation") {
      if (corrCols.length < 2) return "相关性分析需至少选择 2 列（仅数值列）";
      return null;
    }
    // regression
    if (!target) return "回归分析需选择因变量（该数据集无数值列）";
    if (features.includes(target)) return "因变量不能同时作为自变量";
    if (features.length < 1) return "回归分析需至少选择 1 个自变量";
    return null;
  }

  function buildParams(): Record<string, unknown> {
    if (kind === "trend") {
      const p: Record<string, unknown> = { value_col: valueCol, time_col: timeCol };
      if (period) p.period = Number(period);
      if (trendMethod === "prophet") p.method = "prophet";
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
    if (kind === "correlation") {
      return { columns: corrCols, method: corrMethod };
    }
    return { target, features, kind: "ols" };
  }

  async function onRun() {
    const runKind = kind;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    setResp(null);
    try {
      const nextResp = await analyzeStats(profile.dataset_ref, runKind, buildParams(), interpret);
      if (requestId !== requestIdRef.current) return;
      if (nextResp.kind !== runKind) {
        setError(`返回结果类型与当前${KIND_LABELS[runKind]}不一致，请重新运行`);
        return;
      }
      setResp(nextResp);
    } catch (e) {
      if (requestId === requestIdRef.current) {
        setResp(null);
        setError(translateError((e as Error).message));
      }
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }

  const invalid = validationError();
  // 双重约束结果类型：即使状态未来由其他路径写入，也不渲染非当前类型结果。
  const currentResp = resp?.kind === kind ? resp : null;

  return (
    <section style={{ margin: "24px 0", borderTop: "1px solid #eee", paddingTop: 16 }}>
      <h2>统计分析</h2>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
        <label>
          类型{" "}
          <select value={kind} onChange={(e) => onKindChange(e.target.value as StatsKind)}>
            <option value="trend">趋势分析</option>
            <option value="anomaly">异常检测</option>
            <option value="regression">回归分析</option>
            <option value="correlation">相关性分析</option>
          </select>
        </label>

        {(kind === "trend" || kind === "anomaly") && (
          <label>
            数值列{" "}
            <select value={valueCol} onChange={(e) => setValueCol(e.target.value)}>
              {numericCols.map((c) => (
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
            季节周期
            <span style={{ color: "#888", fontSize: 12 }}>
              {kind === "trend" ? "（选填；填写后启用 STL 季节分解）" : "（必填）"}
            </span>{" "}
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

        {kind === "trend" && (
          <label>
            预测方法{" "}
            <select value={trendMethod} onChange={(e) => setTrendMethod(e.target.value)}>
              <option value="auto">自动（STL/MA）</option>
              <option value="prophet">Prophet</option>
            </select>
          </label>
        )}

        {kind === "regression" && (
          <label>
            因变量{" "}
            <select value={target} onChange={(e) => onTargetChange(e.target.value)}>
              {numericCols.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
        )}

        {kind === "correlation" && (
          <label>
            方法{" "}
            <select value={corrMethod} onChange={(e) => setCorrMethod(e.target.value)}>
              <option value="pearson">pearson</option>
              <option value="spearman">spearman</option>
            </select>
          </label>
        )}

        <label>
          <input type="checkbox" checked={interpret} onChange={(e) => setInterpret(e.target.checked)} />{" "}
          附带中文解读
        </label>

        <button onClick={onRun} disabled={loading || invalid !== null}>
          {loading ? "分析中…" : "运行分析"}
        </button>
        {invalid && <span style={{ color: "#b26a00", fontSize: 13 }}>{invalid}</span>}
      </div>

      {kind === "regression" && (
        <div style={{ marginTop: 8, fontSize: 13 }}>
          自变量（至少选 1 个，仅数值列）：
          {numericCols
            .filter((c) => c !== target)
            .map((c) => (
              <label key={c} style={{ marginLeft: 8 }}>
                <input type="checkbox" checked={features.includes(c)} onChange={() => toggleFeature(c)} /> {c}
              </label>
            ))}
        </div>
      )}

      {kind === "correlation" && (
        <div style={{ marginTop: 8, fontSize: 13 }}>
          参与列（至少选 2 列，仅数值列）：
          {numericCols.map((c) => (
            <label key={c} style={{ marginLeft: 8 }}>
              <input type="checkbox" checked={corrCols.includes(c)} onChange={() => toggleCorrCol(c)} /> {c}
            </label>
          ))}
        </div>
      )}

      {error && <p style={{ color: "crimson" }}>出错：{error}</p>}

      {!loading && !error && !currentResp && (
        <p
          style={{ marginTop: 16, padding: 16, color: "#666", background: "#f7f7f7" }}
          role="status"
        >
          尚未运行{KIND_LABELS[kind]}，请配置参数后点击“运行分析”。
        </p>
      )}

      {currentResp && (
        <div style={{ marginTop: 16 }}>
          {currentResp.kind === "trend" && <TrendView result={currentResp.result as TrendResult} />}
          {currentResp.kind === "anomaly" && <AnomalyView result={currentResp.result as AnomalyResult} />}
          {currentResp.kind === "regression" && <RegressionView result={currentResp.result as RegressionResult} />}
          {currentResp.kind === "correlation" && <CorrelationView result={currentResp.result as CorrelationResult} />}
          {currentResp.interpretation && (
            <div style={{ marginTop: 12, padding: 12, background: "#f4f8ff", borderLeft: "3px solid #4a7" }}>
              <strong>中文解读：</strong>
              <p style={{ margin: "6px 0 0", whiteSpace: "pre-wrap" }}>{currentResp.interpretation}</p>
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

// ── 相关性：ECharts 热力图（复用 EChartsRenderer）+ 最强相关对表 ──
function CorrelationView({ result }: { result: CorrelationResult }) {
  const cols = result.columns;
  const data: [number, number, number | null][] = [];
  result.matrix.forEach((row, i) => row.forEach((v, j) => data.push([j, i, v])));

  const option = {
    title: { text: `相关性热力图（${result.method}）` },
    tooltip: { position: "top" },
    grid: { height: "70%", top: "12%" },
    xAxis: { type: "category", data: cols, splitArea: { show: true } },
    yAxis: { type: "category", data: cols, splitArea: { show: true } },
    visualMap: {
      min: -1, max: 1, calculable: true, orient: "horizontal", left: "center", bottom: "0%",
      inRange: { color: ["#2b6cb0", "#ffffff", "#c53030"] },
    },
    series: [{
      type: "heatmap",
      data,
      label: {
        show: true,
        formatter: (p: { value: [number, number, number | null] }) =>
          p.value[2] == null ? "" : p.value[2].toFixed(2),
      },
    }],
  };

  return (
    <div>
      <p style={{ fontSize: 14 }}>
        方法 {result.method} · 样本 {result.n_obs} · 列数 {cols.length}
      </p>
      <EChartsRenderer option={option} chartId={`corr-${result.method}`} />
      {result.top_pairs.length > 0 && (
        <table style={{ borderCollapse: "collapse", fontSize: 13, border: "1px solid #ccc", marginTop: 8 }}>
          <thead>
            <tr><th style={cell}>列 A</th><th style={cell}>列 B</th><th style={cell}>相关系数</th><th style={cell}>p 值</th><th style={cell}>显著</th></tr>
          </thead>
          <tbody>
            {result.top_pairs.map((p, i) => (
              <tr key={i}>
                <td style={cell}>{p.a}</td>
                <td style={cell}>{p.b}</td>
                <td style={cell}>{fmt(p.corr)}</td>
                <td style={cell}>{fmt(p.p_value)}</td>
                <td style={cell}>{p.significant ? "✓" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

const cell: React.CSSProperties = { border: "1px solid #ccc", padding: "2px 8px" };

function fmt(v: number | null): string {
  if (v == null) return "—";
  return Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.001) ? v.toExponential(3) : String(Number(v.toFixed(4)));
}
