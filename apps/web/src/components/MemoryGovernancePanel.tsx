import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  deleteProjectMemory,
  listProjectMemories,
  reviseProjectMemory,
} from "@/api/client";
import type {
  MemoryKind,
  MemoryScope,
  MemoryStatus,
  WorkspaceMemory,
} from "@/types";

const STATUS_LABELS: Record<MemoryStatus, string> = {
  active: "生效中",
  conflict: "待处理冲突",
  superseded: "历史版本",
  deleted: "已删除",
};
const SCOPE_LABELS: Record<MemoryScope, string> = {
  project: "项目",
  conversation: "对话",
  subject: "仅自己",
};
const KIND_LABELS: Record<MemoryKind, string> = {
  field_alias: "字段别名",
  user_preference: "用户偏好",
  confirmed_decision: "确认决策",
  entity_mapping: "实体映射",
  conversation_summary: "对话摘要",
};

export function MemoryGovernancePanel({
  projectId,
  projectName,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  const [memories, setMemories] = useState<WorkspaceMemory[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [statusFilter, setStatusFilter] = useState<MemoryStatus | "all">("all");
  const [scopeFilter, setScopeFilter] = useState<MemoryScope | "all">("all");
  const [editing, setEditing] = useState<WorkspaceMemory | null>(null);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [confidenceDraft, setConfidenceDraft] = useState("0.9");
  const [expiresDraft, setExpiresDraft] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await listProjectMemories(projectId);
      setMemories(response.items);
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        if (editing) setEditing(null);
        else onClose();
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [editing, onClose]);

  const visible = useMemo(
    () => memories.filter(
      (memory) => (statusFilter === "all" || memory.status === statusFilter)
        && (scopeFilter === "all" || memory.scope === scopeFilter),
    ),
    [memories, scopeFilter, statusFilter],
  );

  function beginEdit(memory: WorkspaceMemory) {
    setEditing(memory);
    setSummaryDraft(memory.content_summary);
    setConfidenceDraft(String(memory.confidence));
    setExpiresDraft(toLocalDateTime(memory.expires_at));
    setError("");
    setNotice("");
  }

  async function submitRevision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editing) return;
    const confidence = Number(confidenceDraft);
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      setError("置信度必须在 0 到 1 之间。");
      return;
    }
    setBusyId(editing.memory_id);
    setError("");
    try {
      await reviseProjectMemory(projectId, editing.memory_id, {
        expected_version: editing.version,
        content_summary: summaryDraft,
        confidence,
        expires_at: expiresDraft ? new Date(expiresDraft).toISOString() : null,
      });
      setEditing(null);
      setNotice("纠正已保存为新版本，旧版本继续保留在历史中。");
      await load();
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(memory: WorkspaceMemory) {
    if (!window.confirm(`删除这条${STATUS_LABELS[memory.status]}记忆？历史快照不会被改写。`)) {
      return;
    }
    setBusyId(memory.memory_id);
    setError("");
    setNotice("");
    try {
      await deleteProjectMemory(
        projectId,
        memory.memory_id,
        memory.version,
      );
      setNotice("记忆已软删除；已有运行和历史快照仍保持原样。");
      await load();
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="memory-governance-backdrop" role="presentation">
      <aside
        className="memory-governance-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-governance-title"
      >
        <header className="memory-governance-header">
          <div>
            <span>PROJECT MEMORY</span>
            <h2 id="memory-governance-title">项目记忆</h2>
            <p>{projectName} · 仅展示你有权查看的内容</p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭项目记忆">×</button>
        </header>

        <div className="memory-governance-safety">
          可纠正摘要、置信度和有效期。作用域、类型及原始来源不可修改；删除为软删除。
        </div>

        <div className="memory-governance-filters" aria-label="记忆筛选">
          <label>
            状态
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value as MemoryStatus | "all")}
            >
              <option value="all">全部</option>
              {Object.entries(STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <label>
            作用域
            <select
              value={scopeFilter}
              onChange={(event) => setScopeFilter(event.target.value as MemoryScope | "all")}
            >
              <option value="all">全部</option>
              {Object.entries(SCOPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <button type="button" onClick={() => void load()} disabled={loading}>刷新</button>
        </div>

        {error && <div className="memory-governance-message memory-governance-message--error">{error}</div>}
        {notice && <div className="memory-governance-message">{notice}</div>}

        <div className="memory-governance-list" aria-live="polite">
          {loading ? (
            <p className="memory-governance-empty">正在读取项目记忆…</p>
          ) : visible.length === 0 ? (
            <p className="memory-governance-empty">当前筛选条件下没有记忆。</p>
          ) : visible.map((memory) => (
            <article className="memory-card" key={memory.memory_id}>
              <div className="memory-card__meta">
                <span className={`memory-status memory-status--${memory.status}`}>
                  {STATUS_LABELS[memory.status]}
                </span>
                <span>{SCOPE_LABELS[memory.scope]}</span>
                <span>{KIND_LABELS[memory.kind]}</span>
                <span>v{memory.version}</span>
              </div>
              <p className="memory-card__summary">{memory.content_summary}</p>
              <dl className="memory-card__facts">
                <div><dt>置信度</dt><dd>{Math.round(memory.confidence * 100)}%</dd></div>
                <div><dt>来源类型</dt><dd>{memory.source_type}</dd></div>
                <div><dt>有效期</dt><dd>{formatDate(memory.expires_at) || "长期"}</dd></div>
                <div><dt>资源关联</dt><dd>{memory.links.length || "无"}</dd></div>
              </dl>
              <div className="memory-card__actions">
                {memory.status === "active" && (
                  <button
                    type="button"
                    onClick={() => beginEdit(memory)}
                    disabled={busyId !== null}
                  >
                    纠正
                  </button>
                )}
                {(memory.status === "active" || memory.status === "conflict") && (
                  <button
                    type="button"
                    className="memory-danger-button"
                    onClick={() => void remove(memory)}
                    disabled={busyId !== null}
                  >
                    {busyId === memory.memory_id ? "处理中…" : "删除"}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>

        {editing && (
          <form className="memory-editor" onSubmit={(event) => void submitRevision(event)}>
            <div className="memory-editor__heading">
              <div><span>IMMUTABLE REVISION</span><h3>纠正记忆</h3></div>
              <button type="button" onClick={() => setEditing(null)} aria-label="关闭纠正表单">×</button>
            </div>
            <label>
              摘要
              <textarea
                value={summaryDraft}
                onChange={(event) => setSummaryDraft(event.target.value)}
                maxLength={4_000}
                required
                autoFocus
              />
            </label>
            <div className="memory-editor__fields">
              <label>
                置信度（0–1）
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={confidenceDraft}
                  onChange={(event) => setConfidenceDraft(event.target.value)}
                  required
                />
              </label>
              <label>
                有效截止时间
                <input
                  type="datetime-local"
                  value={expiresDraft}
                  onChange={(event) => setExpiresDraft(event.target.value)}
                />
              </label>
            </div>
            <p>保存会生成新版本；若其他人已修改该版本，服务端会拒绝本次覆盖。</p>
            <div className="memory-editor__actions">
              <button type="button" onClick={() => setEditing(null)}>取消</button>
              <button type="submit" disabled={busyId !== null || !summaryDraft.trim()}>
                {busyId ? "保存中…" : "保存新版本"}
              </button>
            </div>
          </form>
        )}
      </aside>
    </div>
  );
}

function formatDate(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function toLocalDateTime(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const local = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function messageOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : "项目记忆操作失败，请重试。";
}
