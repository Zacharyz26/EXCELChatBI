import { useEffect, useState, type FormEvent } from "react";
import { ChatPanel } from "@/components/ChatPanel";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceArtifact, WorkspaceDataset } from "@/types";

interface ChatWorkspaceProps {
  onOpenClassic: () => void;
}

/** 对话式产品主入口；经典五页仍通过显式入口保留。 */
export function ChatWorkspace({ onOpenClassic }: ChatWorkspaceProps) {
  const initialize = useWorkspaceStore((state) => state.initialize);
  const initialized = useWorkspaceStore((state) => state.initialized);
  const loading = useWorkspaceStore((state) => state.loading);
  const uploading = useWorkspaceStore((state) => state.uploading);
  const streaming = useWorkspaceStore((state) => state.streaming);
  const projects = useWorkspaceStore((state) => state.projects);
  const conversations = useWorkspaceStore((state) => state.conversations);
  const datasets = useWorkspaceStore((state) => state.datasets);
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const activeProjectId = useWorkspaceStore((state) => state.activeProjectId);
  const activeConversationId = useWorkspaceStore((state) => state.activeConversationId);
  const selectProject = useWorkspaceStore((state) => state.selectProject);
  const selectConversation = useWorkspaceStore((state) => state.selectConversation);
  const addProject = useWorkspaceStore((state) => state.addProject);
  const addConversation = useWorkspaceStore((state) => state.addConversation);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectName, setProjectName] = useState("");

  useEffect(() => {
    void initialize();
  }, [initialize]);

  const activeProject = projects.find((project) => project.id === activeProjectId);
  const activeConversation = conversations.find((item) => item.id === activeConversationId);
  const busy = loading || uploading || streaming;

  async function submitProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectName.trim()) return;
    await addProject(projectName);
    setProjectName("");
    setCreatingProject(false);
  }

  return (
    <div className="conversation-shell">
      <aside className={`conversation-sidebar${sidebarOpen ? " conversation-sidebar--open" : ""}`}>
        <div className="conversation-brand">
          <div className="conversation-brand__mark">BI</div>
          <div><strong>ChatBI</strong><span>AI DATA WORKSPACE</span></div>
        </div>

        <button
          type="button"
          className="new-conversation-button"
          onClick={() => void addConversation()}
          disabled={!activeProjectId || busy}
        >
          <PlusIcon />
          新对话
        </button>

        <div className="conversation-sidebar__scroll">
          <div className="sidebar-section-heading">
            <span>项目</span>
            <button
              type="button"
              onClick={() => setCreatingProject((value) => !value)}
              disabled={busy}
              aria-label="新建项目"
            >＋</button>
          </div>

          {creatingProject && (
            <form className="project-create-form" onSubmit={submitProject}>
              <input
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
                placeholder="项目名称"
                maxLength={100}
                autoFocus
              />
              <button type="submit" disabled={!projectName.trim()}>创建</button>
            </form>
          )}

          <div className="project-list">
            {projects.map((project) => (
              <div key={project.id}>
                <button
                  type="button"
                  className={`project-item${project.id === activeProjectId ? " project-item--active" : ""}`}
                  onClick={() => {
                    void selectProject(project.id);
                    setSidebarOpen(false);
                  }}
                  disabled={busy && project.id !== activeProjectId}
                >
                  <FolderIcon />
                  <span>{project.name}</span>
                  <ChevronIcon open={project.id === activeProjectId} />
                </button>

                {project.id === activeProjectId && (
                  <div className="project-children">
                    {datasets.length > 0 && (
                      <div className="sidebar-datasets">
                        {[...datasets].reverse().map((dataset) => (
                          <div className="dataset-item" key={dataset.ref} title={dataset.filename}>
                            <DatasetIcon /><span>{dataset.filename}</span>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="conversation-list">
                      {conversations.map((conversation) => (
                        <button
                          type="button"
                          key={conversation.id}
                          className={`conversation-item${conversation.id === activeConversationId ? " conversation-item--active" : ""}`}
                          onClick={() => {
                            void selectConversation(conversation.id);
                            setSidebarOpen(false);
                          }}
                          disabled={busy && conversation.id !== activeConversationId}
                          title={conversation.title}
                        >
                          <MessageIcon />
                          <span>{conversation.title}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        <button type="button" className="classic-mode-button" onClick={onOpenClassic} disabled={busy}>
          <GridIcon />
          <span><strong>经典模式</strong><small>使用原分析功能页</small></span>
          <span aria-hidden="true">→</span>
        </button>
      </aside>

      {sidebarOpen && (
        <button
          type="button"
          className="conversation-backdrop"
          onClick={() => setSidebarOpen(false)}
          aria-label="关闭侧栏"
        />
      )}

      <main className="conversation-main">
        <header className="conversation-header">
          <button
            type="button"
            className="conversation-menu-button"
            onClick={() => setSidebarOpen(true)}
            aria-label="打开侧栏"
          >
            <MenuIcon />
          </button>
          <div className="conversation-header__title">
            <span>{activeProject?.name ?? (initialized ? "未选择项目" : "正在初始化")}</span>
            <h1>{activeConversation?.title ?? "新对话"}</h1>
          </div>
          <div className="conversation-header__status">
            <span className={`connection-dot${streaming ? " connection-dot--busy" : ""}`} />
            {streaming ? "正在生成" : datasets.length > 0 ? `${datasets.length} 个数据集` : "未上传数据"}
          </div>
        </header>

        <div className="conversation-body">
          <ChatPanel />
          <DatasetContext datasets={datasets} artifacts={artifacts} />
        </div>
      </main>
    </div>
  );
}

function DatasetContext({
  datasets,
  artifacts,
}: {
  datasets: WorkspaceDataset[];
  artifacts: WorkspaceArtifact[];
}) {
  const profileArtifact = [...artifacts]
    .reverse()
    .find((artifact) => artifact.type === "profile" && artifact.dataset_ref);
  const dataset = datasets.find((item) => item.ref === profileArtifact?.dataset_ref);
  if (!dataset) {
    return (
      <aside className="dataset-context" aria-label="数据上下文">
        <div className="context-heading"><span>CONTEXT</span><h2>数据上下文</h2></div>
        <div className="context-empty">
          <DatasetIcon />
          <strong>尚未连接数据</strong>
          <p>从输入框左侧上传 Excel 后，字段画像会显示在这里。</p>
        </div>
      </aside>
    );
  }

  const columns = Array.isArray(dataset.profile.columns) ? dataset.profile.columns : [];
  return (
    <aside className="dataset-context" aria-label="数据上下文">
      <div className="context-heading"><span>CONTEXT</span><h2>当前数据集</h2></div>
      <div className="context-dataset-name">
        <div><DatasetIcon /></div>
        <span><strong>{dataset.filename}</strong><small>{dataset.profile.row_count} 行 · {dataset.profile.column_count} 列</small></span>
      </div>
      <div className="context-section-title">字段</div>
      <div className="context-fields">
        {columns.map((column) => (
          <div key={column.name}>
            <span>{column.name}</span>
            <small>{column.dtype}</small>
          </div>
        ))}
      </div>
      {datasets.length > 1 && <p className="context-more">另有 {datasets.length - 1} 个历史数据集</p>}
    </aside>
  );
}

function PlusIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>;
}

function FolderIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7.5h7l2-2h9v13H3Z" /></svg>;
}

function ChevronIcon({ open }: { open: boolean }) {
  return <svg className={open ? "chevron--open" : ""} viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>;
}

function DatasetIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></svg>;
}

function MessageIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v12H8l-4 3Z" /></svg>;
}

function GridIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>;
}

function MenuIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>;
}
