import { useState } from "react";
import { analyze } from "@/api/client";
import { EChartsRenderer } from "@/components/EChartsRenderer";
import { ExcelUpload } from "@/components/ExcelUpload";
import { KnowledgeQA } from "@/components/KnowledgeQA";
import { ReportPanel } from "@/components/ReportPanel";
import { StatsPanel } from "@/components/StatsPanel";
import type { ChartResponse, DataProfile } from "@/types";

type WorkspaceView = "data" | "analysis" | "stats" | "reports" | "knowledge";
type IconName = WorkspaceView;

interface NavItem {
  id: WorkspaceView;
  label: string;
  description: string;
  icon: IconName;
}

const NAV_ITEMS: NavItem[] = [
  { id: "data", label: "数据接入", description: "上传 Excel 并确认数据画像", icon: "data" },
  { id: "analysis", label: "自动分析", description: "自动规划并生成可视化图表", icon: "analysis" },
  { id: "stats", label: "统计分析", description: "趋势、异常、回归与相关性", icon: "stats" },
  { id: "reports", label: "报告导出", description: "生成 Markdown 与 PDF 报告", icon: "reports" },
  { id: "knowledge", label: "知识库问答", description: "基于企业知识库检索问答", icon: "knowledge" },
];

/** 应用根组件：保留各业务组件状态，仅在工作台视图之间切换可见性。 */
export default function App() {
  const [activeView, setActiveView] = useState<WorkspaceView>("data");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [profile, setProfile] = useState<DataProfile | null>(null);
  const [chart, setChart] = useState<ChartResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentNav = NAV_ITEMS.find((item) => item.id === activeView) ?? NAV_ITEMS[0];

  function navigate(view: WorkspaceView) {
    setActiveView(view);
    setSidebarOpen(false);
  }

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
    <div className="app-shell">
      <aside className={`sidebar${sidebarOpen ? " sidebar--open" : ""}`}>
        <div className="brand">
          <div className="brand__mark" aria-hidden="true">BI</div>
          <div>
            <div className="brand__name">ChatBI</div>
            <div className="brand__caption">智能数据工作台</div>
          </div>
        </div>

        <nav className="sidebar__nav" aria-label="工作台功能导航">
          <div className="sidebar__label">工作空间</div>
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-item${activeView === item.id ? " nav-item--active" : ""}`}
              aria-current={activeView === item.id ? "page" : undefined}
              onClick={() => navigate(item.id)}
            >
              <NavIcon name={item.icon} />
              <span>{item.label}</span>
              <span className="nav-item__indicator" aria-hidden="true" />
            </button>
          ))}
        </nav>

        <div className="sidebar__status">
          <span className={`status-dot${profile ? " status-dot--ready" : ""}`} />
          <div>
            <strong>{profile ? "数据集已就绪" : "等待数据集"}</strong>
            <span>{profile ? `${profile.row_count} 行 · ${profile.column_count} 列` : "请先上传 Excel 文件"}</span>
          </div>
        </div>
      </aside>

      {sidebarOpen && (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="关闭导航"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <main className="workspace-main">
        <header className="workspace-header">
          <button
            type="button"
            className="menu-button"
            aria-label="打开导航"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen(true)}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="workspace-header__copy">
            <div className="workspace-header__eyebrow">ChatBI Workspace</div>
            <h1>{currentNav.label}</h1>
            <p>{currentNav.description}</p>
          </div>
          <div className={`dataset-badge${profile ? " dataset-badge--ready" : ""}`}>
            <span className="status-dot" />
            {profile ? "当前数据集已连接" : "尚未连接数据集"}
          </div>
        </header>

        <div className="workspace-content">
          <section className="workspace-view" hidden={activeView !== "data"} aria-label="数据接入">
            <div className="workspace-card">
              <div className="card-heading">
                <div>
                  <span className="card-heading__kicker">DATA SOURCE</span>
                  <h2>上传 Excel 数据</h2>
                  <p>支持 .xlsx 与 .xls 文件，上传后先确认字段画像，再进入分析流程。</p>
                </div>
                <span className="card-heading__step">01</span>
              </div>
              <ExcelUpload
                onUploaded={(res) => {
                  setProfile(res.profile);
                  setChart(null);
                  setError(null);
                }}
              />
            </div>

            {profile ? (
              <div className="workspace-card">
                <div className="card-heading card-heading--compact">
                  <div>
                    <span className="card-heading__kicker">DATA PROFILE</span>
                    <h2>数据画像</h2>
                    <p>{profile.row_count} 行数据 · {profile.column_count} 个字段</p>
                  </div>
                  <button type="button" className="text-button" onClick={() => navigate("analysis")}>
                    前往自动分析
                    <span aria-hidden="true">→</span>
                  </button>
                </div>
                <div className="table-scroll">
                  <table className="profile-table">
                    <thead>
                      <tr>
                        <th>列名</th><th>类型</th><th>空值率</th><th>Distinct</th>
                        <th>最小值</th><th>最大值</th><th>均值</th>
                      </tr>
                    </thead>
                    <tbody>
                      {profile.columns.map((column) => (
                        <tr key={column.name}>
                          <td><strong>{column.name}</strong></td>
                          <td><span className="type-tag">{column.dtype}</span></td>
                          <td>{(column.null_ratio * 100).toFixed(1)}%</td>
                          <td>{column.distinct_count}</td>
                          <td>{column.min ?? "—"}</td>
                          <td>{column.max ?? "—"}</td>
                          <td>{column.mean ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <WorkspaceEmpty
                title="还没有可用的数据集"
                description="选择一个 Excel 文件后，这里将展示字段类型、空值率和基础统计信息。"
              />
            )}
          </section>

          <section className="workspace-view" hidden={activeView !== "analysis"} aria-label="自动分析">
            {profile ? (
              <>
                <div className="workspace-card action-card">
                  <div>
                    <span className="card-heading__kicker">AUTO INSIGHT</span>
                    <h2>生成智能图表</h2>
                    <p>基于当前 {profile.row_count} 行数据自动选择维度、指标和可视化方式。</p>
                  </div>
                  <button type="button" className="primary-button" onClick={onAnalyze} disabled={loading}>
                    {loading ? "正在分析…" : "生成图表"}
                  </button>
                </div>
                {error && <div className="alert alert--error">出错：{error}</div>}
                {chart ? (
                  <div className="workspace-card chart-card">
                    <div className="card-heading card-heading--compact">
                      <div>
                        <span className="card-heading__kicker">VISUALIZATION</span>
                        <h2>分析图表</h2>
                        <p>图表类型：{chart.chart_type}</p>
                      </div>
                    </div>
                    <EChartsRenderer option={chart.option} chartId={chart.chart_id} />
                  </div>
                ) : (
                  !loading && (
                    <WorkspaceEmpty
                      title="尚未生成图表"
                      description="点击“生成图表”，系统会根据当前数据画像规划并执行可视化分析。"
                    />
                  )
                )}
              </>
            ) : (
              <DatasetRequired onNavigate={() => navigate("data")} />
            )}
          </section>

          <section className="workspace-view" hidden={activeView !== "stats"} aria-label="统计分析">
            {profile ? (
              <div className="workspace-card feature-card"><StatsPanel profile={profile} /></div>
            ) : (
              <DatasetRequired onNavigate={() => navigate("data")} />
            )}
          </section>

          <section className="workspace-view" hidden={activeView !== "reports"} aria-label="报告导出">
            {profile ? (
              <div className="workspace-card feature-card"><ReportPanel profile={profile} /></div>
            ) : (
              <DatasetRequired onNavigate={() => navigate("data")} />
            )}
          </section>

          <section className="workspace-view" hidden={activeView !== "knowledge"} aria-label="知识库问答">
            <div className="workspace-card feature-card"><KnowledgeQA /></div>
          </section>
        </div>
      </main>
    </div>
  );
}

function DatasetRequired({ onNavigate }: { onNavigate: () => void }) {
  return (
    <WorkspaceEmpty
      title="请先连接数据集"
      description="此功能需要基于 Excel 数据运行。上传并确认数据画像后即可继续。"
      action={<button type="button" className="primary-button" onClick={onNavigate}>前往上传数据</button>}
    />
  );
}

function WorkspaceEmpty({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="workspace-card empty-state">
      <div className="empty-state__icon" aria-hidden="true">＋</div>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </div>
  );
}

function NavIcon({ name }: { name: IconName }) {
  const paths: Record<IconName, React.ReactNode> = {
    data: <><path d="M4 6c0 1.1 3.6 2 8 2s8-.9 8-2-3.6-2-8-2-8 .9-8 2Z" /><path d="M4 6v6c0 1.1 3.6 2 8 2s8-.9 8-2V6" /><path d="M4 12v6c0 1.1 3.6 2 8 2s8-.9 8-2v-6" /></>,
    analysis: <><path d="M4 19V9" /><path d="M10 19V5" /><path d="M16 19v-7" /><path d="M22 19V3" /></>,
    stats: <><path d="M3 12h4l3-7 4 14 3-7h4" /><path d="M4 21h16" /></>,
    reports: <><path d="M6 3h9l4 4v14H6Z" /><path d="M14 3v5h5" /><path d="M9 13h7M9 17h7" /></>,
    knowledge: <><path d="M4 5.5A3.5 3.5 0 0 1 7.5 2H12v18H7.5A3.5 3.5 0 0 0 4 23.5Z" /><path d="M20 5.5A3.5 3.5 0 0 0 16.5 2H12v18h4.5a3.5 3.5 0 0 1 3.5 3.5Z" /></>,
  };

  return (
    <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths[name]}
    </svg>
  );
}
