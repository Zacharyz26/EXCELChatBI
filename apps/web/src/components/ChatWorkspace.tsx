import { useEffect, useState, type FormEvent } from "react";
import { ingestSamples, kbOverview } from "@/api/client";
import { ChatPanel } from "@/components/ChatPanel";
import { useWorkspaceStore } from "@/stores/workspace";
import type { KBOverview, WorkspaceArtifact, WorkspaceDataset } from "@/types";

/** 对话式产品主入口（阶段4：经典五页已下线，全部能力经对话链路提供）。 */
export function ChatWorkspace() {
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
  const renameConversation = useWorkspaceStore((state) => state.renameConversation);
  const removeConversation = useWorkspaceStore((state) => state.removeConversation);
  const selectDataset = useWorkspaceStore((state) => state.selectDataset);
  const renameDataset = useWorkspaceStore((state) => state.renameDataset);
  const removeDataset = useWorkspaceStore((state) => state.removeDataset);
  const activeDatasetRef = useWorkspaceStore((state) => state.activeDatasetRef);
  const sendMessage = useWorkspaceStore((state) => state.sendMessage);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [renamingDatasetRef, setRenamingDatasetRef] = useState<string | null>(null);
  const [datasetNameDraft, setDatasetNameDraft] = useState("");

  // 本对话正在使用的数据集：以对话工件的 dataset_ref 为事实来源
  const usedDatasetRefs = new Set(
    artifacts.map((item) => item.dataset_ref).filter((ref): ref is string => !!ref),
  );

  async function confirmRemoveDataset(ref: string, filename: string) {
    if (!window.confirm(`删除数据集「${filename}」？数据文件将被移除。`)) return;
    const warning = await removeDataset(ref);
    if (warning && window.confirm(`${warning}\n\n仍要删除吗？`)) {
      await removeDataset(ref, true);
    }
  }

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
                          renamingDatasetRef === dataset.ref ? (
                            <form
                              key={dataset.ref}
                              className="conversation-rename-form"
                              onSubmit={(event) => {
                                event.preventDefault();
                                void renameDataset(dataset.ref, datasetNameDraft);
                                setRenamingDatasetRef(null);
                              }}
                            >
                              <input
                                value={datasetNameDraft}
                                onChange={(event) => setDatasetNameDraft(event.target.value)}
                                onKeyDown={(event) => {
                                  if (event.key === "Escape") setRenamingDatasetRef(null);
                                }}
                                maxLength={255}
                                autoFocus
                                aria-label="数据集新名称"
                              />
                              <button type="submit" disabled={!datasetNameDraft.trim()}>保存</button>
                            </form>
                          ) : (
                            <div
                              className={`dataset-item${dataset.ref === activeDatasetRef ? " dataset-item--active" : ""}`}
                              key={dataset.ref}
                            >
                              <button
                                type="button"
                                className="dataset-item__main"
                                title={`${dataset.filename}（在右侧面板查看字段）`}
                                onClick={() => selectDataset(dataset.ref)}
                              >
                                <DatasetIcon /><span>{dataset.filename}</span>
                              </button>
                              {usedDatasetRefs.has(dataset.ref) && (
                                <span className="dataset-item__badge" title="本对话正在使用">使用中</span>
                              )}
                              <button
                                type="button"
                                className="dataset-item__action"
                                aria-label={`重命名数据集 ${dataset.filename}`}
                                title="重命名"
                                disabled={busy}
                                onClick={() => {
                                  setRenamingDatasetRef(dataset.ref);
                                  setDatasetNameDraft(dataset.filename);
                                }}
                              >
                                ✎
                              </button>
                              <button
                                type="button"
                                className="dataset-item__action dataset-item__action--danger"
                                aria-label={`删除数据集 ${dataset.filename}`}
                                title="删除数据集"
                                disabled={busy}
                                onClick={() => void confirmRemoveDataset(dataset.ref, dataset.filename)}
                              >
                                ×
                              </button>
                            </div>
                          )
                        ))}
                      </div>
                    )}
                    <div className="conversation-list">
                      {conversations.map((conversation) => (
                        renamingId === conversation.id ? (
                          <form
                            key={conversation.id}
                            className="conversation-rename-form"
                            onSubmit={(event) => {
                              event.preventDefault();
                              void renameConversation(conversation.id, renameDraft);
                              setRenamingId(null);
                            }}
                          >
                            <input
                              value={renameDraft}
                              onChange={(event) => setRenameDraft(event.target.value)}
                              onKeyDown={(event) => {
                                if (event.key === "Escape") setRenamingId(null);
                              }}
                              maxLength={200}
                              autoFocus
                              aria-label="对话新名称"
                            />
                            <button type="submit" disabled={!renameDraft.trim()}>保存</button>
                          </form>
                        ) : (
                          <div
                            key={conversation.id}
                            className={`conversation-item${conversation.id === activeConversationId ? " conversation-item--active" : ""}`}
                          >
                            <button
                              type="button"
                              className="conversation-item__main"
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
                            <button
                              type="button"
                              className="conversation-item__action"
                              aria-label={`重命名对话 ${conversation.title}`}
                              title="重命名"
                              disabled={busy}
                              onClick={() => {
                                setRenamingId(conversation.id);
                                setRenameDraft(conversation.title);
                              }}
                            >
                              ✎
                            </button>
                            <button
                              type="button"
                              className="conversation-item__action conversation-item__action--danger"
                              aria-label={`删除对话 ${conversation.title}`}
                              title="删除"
                              disabled={busy}
                              onClick={() => {
                                if (window.confirm(`删除对话「${conversation.title}」及其消息与工件？`)) {
                                  void removeConversation(conversation.id);
                                }
                              }}
                            >
                              ×
                            </button>
                          </div>
                        )
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

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
          <DatasetContext
            datasets={datasets}
            artifacts={artifacts}
            activeDatasetRef={activeDatasetRef}
            usedDatasetRefs={usedDatasetRefs}
            onView={selectDataset}
            onUse={(dataset) => {
              void sendMessage(
                `接下来的分析请使用数据集「${dataset.filename}」（dataset_ref: ${dataset.ref}）。`,
              );
            }}
            busy={busy}
          />
        </div>
      </main>
    </div>
  );
}

function DatasetContext({
  datasets,
  artifacts,
  activeDatasetRef,
  usedDatasetRefs,
  onView,
  onUse,
  busy,
}: {
  datasets: WorkspaceDataset[];
  artifacts: WorkspaceArtifact[];
  activeDatasetRef: string | null;
  usedDatasetRefs: Set<string>;
  onView: (datasetRef: string) => void;
  onUse: (dataset: WorkspaceDataset) => void;
  busy: boolean;
}) {
  const profileArtifact = [...artifacts]
    .reverse()
    .find((artifact) => artifact.type === "profile" && artifact.dataset_ref);
  // 优先级：侧边栏点选 → 本对话最近画像 → 项目最新数据集
  const dataset = datasets.find((item) => item.ref === activeDatasetRef)
    ?? datasets.find((item) => item.ref === profileArtifact?.dataset_ref)
    ?? datasets[datasets.length - 1];
  const columns = dataset && Array.isArray(dataset.profile.columns)
    ? dataset.profile.columns
    : [];
  const usedDatasets = datasets.filter((item) => usedDatasetRefs.has(item.ref));

  return (
    <aside className="dataset-context" aria-label="数据上下文">
      {dataset ? (
        <>
          <div className="context-heading"><span>CONTEXT</span><h2>当前数据集</h2></div>
          <div className="context-dataset-name">
            <div><DatasetIcon /></div>
            <span><strong>{dataset.filename}</strong><small>{dataset.profile.row_count} 行 · {dataset.profile.column_count} 列</small></span>
          </div>
          {dataset.parent_ref && (
            <p className="context-lineage">
              衍生自「{datasets.find((item) => item.ref === dataset.parent_ref)?.filename ?? "已删除的数据集"}」
              {dataset.transform && ` · 变换：${JSON.stringify(dataset.transform).slice(0, 60)}`}
            </p>
          )}
          {!usedDatasetRefs.has(dataset.ref) && (
            <button
              type="button"
              className="context-use-button"
              disabled={busy}
              onClick={() => onUse(dataset)}
            >
              在当前对话中使用此数据集
            </button>
          )}
          <div className="context-section-title">字段</div>
          <div className="context-fields">
            {columns.map((column) => (
              <div key={column.name}>
                <span>{column.name}</span>
                <small>{column.dtype}</small>
              </div>
            ))}
          </div>
          <div className="context-section-title">本对话使用的数据集</div>
          {usedDatasets.length > 0 ? (
            <div className="context-used-datasets">
              {usedDatasets.map((item) => (
                <button
                  type="button"
                  key={item.ref}
                  className={`context-used-dataset${item.ref === dataset.ref ? " context-used-dataset--current" : ""}`}
                  title="在此面板查看"
                  onClick={() => onView(item.ref)}
                >
                  <DatasetIcon />
                  <span>{item.filename}</span>
                  {item.parent_ref && <small>衍生</small>}
                </button>
              ))}
            </div>
          ) : (
            <p className="context-more">本对话还没有基于数据集的分析。</p>
          )}
        </>
      ) : (
        <>
          <div className="context-heading"><span>CONTEXT</span><h2>数据上下文</h2></div>
          <div className="context-empty">
            <DatasetIcon />
            <strong>尚未连接数据</strong>
            <p>从输入框左侧上传 Excel 后，字段画像会显示在这里。</p>
          </div>
        </>
      )}
      <KnowledgeSection />
    </aside>
  );
}

/** 知识库入口（原知识库页的摄入/概览能力迁入，问答走对话 kb_search）。 */
function KnowledgeSection() {
  const [overview, setOverview] = useState<KBOverview | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    kbOverview().then(setOverview).catch(() => setOverview(null));
  }, []);

  async function onIngest() {
    setBusy(true);
    setNotice(null);
    try {
      const result = await ingestSamples();
      setNotice(`已摄入 ${result.ingested_docs} 篇文档，共 ${result.total_chunks} 个片段`);
      setOverview(await kbOverview());
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "摄入失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="context-knowledge">
      <div className="context-section-title">知识库</div>
      {overview && overview.chunk_count > 0 ? (
        <p className="context-knowledge__summary">
          {overview.chunk_count} 个片段
          {overview.topics.length > 0 && ` · 主题：${overview.topics.join("、")}`}
          <br />在对话中直接询问指标口径即可检索。
        </p>
      ) : (
        <p className="context-knowledge__summary">知识库为空，可先摄入样例文档。</p>
      )}
      <button type="button" onClick={() => void onIngest()} disabled={busy}>
        {busy ? "正在摄入…" : "摄入样例知识库"}
      </button>
      {notice && <p className="context-knowledge__notice">{notice}</p>}
    </div>
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

function MenuIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>;
}
