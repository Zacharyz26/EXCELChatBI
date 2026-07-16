import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { fileUrl } from "@/api/client";
import { EChartsRenderer } from "@/components/EChartsRenderer";
import { useWorkspaceStore } from "@/stores/workspace";
import type {
  LiveTurnItem,
  ToolStep,
  WorkspaceArtifact,
  WorkspaceMessage,
} from "@/types";

/** 阶段 3 对话主区：Agent 透明度卡片流（理解/执行/工件）+ 快捷指令条。 */
export function ChatPanel() {
  const messages = useWorkspaceStore((state) => state.messages);
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const liveTurn = useWorkspaceStore((state) => state.liveTurn);
  const activeConversationId = useWorkspaceStore((state) => state.activeConversationId);
  const loading = useWorkspaceStore((state) => state.loading);
  const uploading = useWorkspaceStore((state) => state.uploading);
  const streaming = useWorkspaceStore((state) => state.streaming);
  const error = useWorkspaceStore((state) => state.error);
  const sendMessage = useWorkspaceStore((state) => state.sendMessage);
  const uploadFile = useWorkspaceStore((state) => state.uploadFile);
  const clearError = useWorkspaceStore((state) => state.clearError);
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
  }, [messages, liveTurn, streaming]);

  async function submitMessage() {
    const content = draft.trim();
    if (!content || !activeConversationId || streaming || uploading) return;
    setDraft("");
    await sendMessage(content);
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitMessage();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submitMessage();
    }
  }

  function onFileSelected(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    input.value = "";
    if (file) void uploadFile(file);
  }

  function injectTemplate(template: string) {
    setDraft(template);
    textareaRef.current?.focus();
  }

  function adjustParams(tool: string, label: string, argsJson: string) {
    // 参数确认走对话链路（14.4）：结构化追问消息，真相源始终是对话
    void sendMessage(
      `请把「${label}」（${tool}）的参数调整为：\n${argsJson}\n并重新执行该分析。`,
    );
  }

  const busy = streaming || uploading;

  return (
    <section className="chat-panel" aria-label="对话消息">
      <div className="chat-messages" aria-live="polite">
        {loading && messages.length === 0 ? (
          <div className="chat-loading">
            <span /><span /><span />
            <p>正在载入对话…</p>
          </div>
        ) : uploading && messages.length === 0 ? (
          <div className="message-list"><UploadProgress /></div>
        ) : messages.length === 0 && !streaming ? (
          <ChatWelcome onUpload={() => fileInputRef.current?.click()} />
        ) : (
          <div className="message-list">
            {messages.map((message) => (
              <MessageItem
                key={message.id}
                message={message}
                artifacts={artifacts.filter((item) => item.message_id === message.id)}
                onAdjust={adjustParams}
                busy={busy}
              />
            ))}
            {streaming && <LiveTurnBlock items={liveTurn} />}
            {uploading && <UploadProgress />}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-composer-zone">
        {error && (
          <div className="chat-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={clearError} aria-label="关闭错误提示">×</button>
          </div>
        )}
        <div className="quick-commands" role="toolbar" aria-label="快捷指令">
          {QUICK_COMMANDS.map((command) => (
            <button
              key={command.label}
              type="button"
              className="quick-command"
              onClick={() => injectTemplate(command.template)}
              disabled={!activeConversationId || busy}
              title={command.hint}
            >
              {command.label}
            </button>
          ))}
        </div>
        <form className="chat-composer" onSubmit={onSubmit}>
          <input
            ref={fileInputRef}
            className="visually-hidden"
            type="file"
            accept=".xlsx,.xls"
            onChange={onFileSelected}
            tabIndex={-1}
          />
          <button
            type="button"
            className="composer-upload"
            onClick={() => fileInputRef.current?.click()}
            disabled={!activeConversationId || busy}
            aria-label="上传 Excel"
            title="上传 Excel"
          >
            <PaperclipIcon />
          </button>
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="描述你的分析需求…"
            rows={1}
            maxLength={20_000}
            disabled={!activeConversationId || loading}
            aria-label="消息内容"
          />
          <button
            type="submit"
            className="composer-send"
            disabled={!draft.trim() || !activeConversationId || busy}
            aria-label={streaming ? "正在生成回复" : "发送消息"}
          >
            {streaming ? <LoadingRing /> : <SendIcon />}
          </button>
        </form>
        <p className="composer-hint">
          {uploading
            ? "正在解析并生成数据画像…"
            : "Enter 发送 · Shift + Enter 换行 · Agent 会自动规划并调用分析工具"}
        </p>
      </div>
    </section>
  );
}

// ── 快捷指令条（14.4：注入模板仍走对话链路，不退化成菜单表单）──

const QUICK_COMMANDS = [
  {
    label: "自动分析",
    hint: "让 Agent 自主规划分析",
    template: "请分析这份数据：先给出数据画像与质量概况，再完成你认为最有价值的 2-3 个分析并出图，最后用中文总结主要发现。",
  },
  {
    label: "趋势",
    hint: "趋势分析",
    template: "请分析【数值列】随【时间列】的变化趋势，并生成折线图。",
  },
  {
    label: "异常",
    hint: "异常检测",
    template: "请检测【数值列】中的异常值，列出异常点，并说明是否建议排除后重新分析。",
  },
  {
    label: "相关性",
    hint: "相关性分析",
    template: "请分析各数值列之间的相关性，指出最强相关的字段对并解读。",
  },
  {
    label: "图表",
    hint: "生成图表",
    template: "请用合适的图表展示各【维度列】的【数值列】情况。",
  },
  {
    label: "报告",
    hint: "汇总本对话分析成报告",
    template: "请把本次对话已完成的分析组装成一份报告，附要点解读，并导出 PDF。",
  },
  {
    label: "质检",
    hint: "数据质量检查",
    template: "请检查数据质量：空值、整行重复、常量列，并给出清洗建议。",
  },
];

const TOOL_LABELS: Record<string, string> = {
  get_data_profile: "数据画像与质量概况",
  trend_analysis: "趋势分析",
  anomaly_detect: "异常检测",
  regression: "回归分析",
  correlation: "相关性分析",
  gen_chart: "生成图表",
  chart_screenshot: "图表截图",
  transform_dataset: "数据集变换",
  aggregate_preview: "分组聚合取数",
  kb_search: "知识库检索",
  generate_report: "生成报告",
};

// ── 消息与卡片 ──

function MessageItem({
  message,
  artifacts,
  onAdjust,
  busy,
}: {
  message: WorkspaceMessage;
  artifacts: WorkspaceArtifact[];
  onAdjust: (tool: string, label: string, argsJson: string) => void;
  busy: boolean;
}) {
  if (message.role === "tool") return null; // 工具结果原文不进消息流（工件卡承载）
  const isUser = message.role === "user";
  const toolCalls = message.tool_calls ?? [];
  return (
    <article className={`message-row message-row--${isUser ? "user" : "assistant"}`}>
      {!isUser && <div className="message-avatar" aria-hidden="true">BI</div>}
      <div className="message-content">
        <div className="message-meta">
          <strong>{isUser ? "你" : "ChatBI"}</strong>
          <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
        </div>
        <div className="message-bubble">
          {message.content && <p>{message.content}</p>}
          {toolCalls.length > 0 && (
            <div className="tool-steps-card">
              {toolCalls.map((call, index) => {
                const tool = stringOf(call.name);
                const args = prettyArguments(call.arguments);
                return (
                  <ToolStepRow
                    key={stringOf(call.id) || index}
                    step={{
                      id: stringOf(call.id),
                      tool,
                      label: TOOL_LABELS[tool] ?? tool,
                      status: "ok",
                      argsPreview: args,
                    }}
                    onAdjust={onAdjust}
                    busy={busy}
                  />
                );
              })}
            </div>
          )}
          {artifacts.map((artifact) => (
            <ArtifactCard key={artifact.id} artifact={artifact} />
          ))}
        </div>
      </div>
    </article>
  );
}

/** 正在进行的 Agent 轮次：按事件顺序渲染理解卡/执行卡/工件卡/流式正文。 */
function LiveTurnBlock({ items }: { items: LiveTurnItem[] }) {
  return (
    <article className="message-row message-row--assistant">
      <div className="message-avatar" aria-hidden="true">BI</div>
      <div className="message-content">
        <div className="message-meta"><strong>ChatBI</strong></div>
        <div className="message-bubble">
          {items.length === 0 && (
            <p className="live-thinking">正在理解需求<TypingCursor /></p>
          )}
          {items.map((item, index) => {
            const isLast = index === items.length - 1;
            if (item.kind === "text") {
              return <p key={item.id}>{item.content}{isLast && <TypingCursor />}</p>;
            }
            if (item.kind === "understanding") {
              return (
                <div key={item.id} className="understanding-card">
                  <span className="understanding-card__tag">理解</span>
                  <p>{item.text}</p>
                </div>
              );
            }
            if (item.kind === "tools") {
              return (
                <div key={item.id} className="tool-steps-card">
                  {item.steps.map((step) => (
                    <ToolStepRow key={step.id} step={step} busy />
                  ))}
                </div>
              );
            }
            return <ArtifactCard key={item.id} artifact={item.artifact} />;
          })}
        </div>
      </div>
    </article>
  );
}

/** 执行卡中的一步：状态图标 + 人话标签 + 参数/摘要 + 调整参数（14.4）。 */
function ToolStepRow({
  step,
  onAdjust,
  busy,
}: {
  step: ToolStep;
  onAdjust?: (tool: string, label: string, argsJson: string) => void;
  busy: boolean;
}) {
  const [adjusting, setAdjusting] = useState(false);
  const [paramsDraft, setParamsDraft] = useState("");

  function openAdjust() {
    setParamsDraft(step.argsPreview ?? "{}");
    setAdjusting(true);
  }

  function submitAdjust(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!onAdjust || !paramsDraft.trim()) return;
    setAdjusting(false);
    onAdjust(step.tool, step.label, paramsDraft.trim());
  }

  return (
    <div className={`tool-step tool-step--${step.status}`}>
      <div className="tool-step__row">
        <span className="tool-step__status" aria-hidden="true">
          {step.status === "ok" ? "✓" : step.status === "error" ? "✗" : <LoadingRing />}
        </span>
        <span className="tool-step__label">{step.label}</span>
        <span className="tool-step__summary">
          {step.status === "error" ? step.message : step.summary}
        </span>
        {onAdjust && step.argsPreview && !busy && (
          <button
            type="button"
            className="tool-step__adjust"
            onClick={() => (adjusting ? setAdjusting(false) : openAdjust())}
          >
            调整参数
          </button>
        )}
      </div>
      {step.argsPreview && !adjusting && (
        <code className="tool-step__args">{step.argsPreview}</code>
      )}
      {adjusting && (
        <form className="tool-step__form" onSubmit={submitAdjust}>
          <textarea
            value={paramsDraft}
            onChange={(event) => setParamsDraft(event.target.value)}
            rows={4}
            aria-label="调整后的参数（JSON）"
          />
          <div>
            <button type="submit" disabled={!paramsDraft.trim()}>以新参数重新执行</button>
            <button type="button" onClick={() => setAdjusting(false)}>取消</button>
          </div>
        </form>
      )}
    </div>
  );
}

function ArtifactCard({ artifact }: { artifact: WorkspaceArtifact }) {
  if (artifact.type === "profile") return <ProfileArtifact artifact={artifact} />;
  if (artifact.type === "chart") return <ChartArtifact artifact={artifact} />;
  if (artifact.type === "table") return <TableArtifact artifact={artifact} />;
  if (artifact.type === "stats") return <StatsArtifact artifact={artifact} />;
  if (artifact.type === "report") return <ReportArtifact artifact={artifact} />;
  return null;
}

function ProfileArtifact({ artifact }: { artifact: WorkspaceArtifact }) {
  const raw = artifact.payload ?? {};
  // 兼容 {profile, quality} 包装（get_data_profile）与裸画像（上传）
  const payload = isRecord(raw.profile) ? raw.profile : raw;
  const quality = isRecord(raw.quality) ? raw.quality : null;
  const rowCount = numberValue(payload.row_count);
  const columnCount = numberValue(payload.column_count);
  const columns = Array.isArray(payload.columns) ? payload.columns : [];
  const names = columns
    .map((column) => (
      column && typeof column === "object" && "name" in column
        ? String(column.name)
        : ""
    ))
    .filter(Boolean);

  return (
    <section className="profile-artifact">
      <div className="profile-artifact__heading">
        <div className="artifact-icon" aria-hidden="true"><TableIcon /></div>
        <div>
          <span>数据画像</span>
          <strong>Excel 字段概况</strong>
        </div>
        <span className="artifact-status">已完成</span>
      </div>
      <div className="profile-artifact__stats">
        <div><strong>{rowCount ?? "—"}</strong><span>数据行</span></div>
        <div><strong>{columnCount ?? "—"}</strong><span>字段数</span></div>
      </div>
      {quality && (
        <p className="profile-artifact__quality">
          整行重复 {String(quality.duplicate_rows ?? "—")} 行
          {Array.isArray(quality.high_null_columns) && quality.high_null_columns.length > 0
            && ` · 高空值列 ${quality.high_null_columns.length} 个`}
          {Array.isArray(quality.constant_columns) && quality.constant_columns.length > 0
            && ` · 常量列 ${quality.constant_columns.length} 个`}
        </p>
      )}
      {names.length > 0 && (
        <div className="profile-artifact__fields">
          {names.slice(0, 8).map((name) => <span key={name}>{name}</span>)}
          {names.length > 8 && <span>+{names.length - 8}</span>}
        </div>
      )}
    </section>
  );
}

function ChartArtifact({ artifact }: { artifact: WorkspaceArtifact }) {
  const payload = artifact.payload ?? {};
  const option = isRecord(payload.option) ? payload.option : null;
  if (!option) return null;
  return (
    <section className="chart-artifact">
      <EChartsRenderer option={option} chartId={stringOf(payload.chart_id) || artifact.id} />
    </section>
  );
}

function TableArtifact({ artifact }: { artifact: WorkspaceArtifact }) {
  const payload = artifact.payload ?? {};
  const rows = Array.isArray(payload.rows) ? payload.rows.filter(isRecord) : [];
  if (rows.length === 0) return null;
  const groupCol = stringOf(payload.group_col) || "分组";
  const valueLabel = stringOf(payload.value_col)
    ? `${payload.agg}(${stringOf(payload.value_col)})`
    : "计数";
  const total = numberValue(payload.group_total);
  return (
    <section className="table-artifact">
      <table>
        <thead>
          <tr><th>{groupCol}</th><th>{valueLabel}</th><th>样本数</th></tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td>{String(row.group ?? "")}</td>
              <td>{formatNumber(row.value)}</td>
              <td>{formatNumber(row.count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {payload.truncated === true && total !== null && (
        <p className="table-artifact__note">共 {total} 组，仅展示前 {rows.length} 组</p>
      )}
    </section>
  );
}

const STATS_KIND_LABELS: Record<string, string> = {
  trend_analysis: "趋势分析",
  anomaly_detect: "异常检测",
  regression: "回归分析",
  correlation: "相关性分析",
};

function StatsArtifact({ artifact }: { artifact: WorkspaceArtifact }) {
  const payload = artifact.payload ?? {};
  const kind = stringOf(payload.kind) || artifact.source_tool || "stats";
  const result = isRecord(payload.result) ? payload.result : payload;
  const highlights = Object.entries(result)
    .filter(([, value]) => typeof value === "number" || typeof value === "string")
    .slice(0, 4);
  return (
    <section className="stats-artifact">
      <div className="stats-artifact__heading">
        <span>{STATS_KIND_LABELS[kind] ?? kind}</span>
        <span className="artifact-status">已完成</span>
      </div>
      {highlights.length > 0 && (
        <dl>
          {highlights.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{typeof value === "number" ? formatNumber(value) : String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <details>
        <summary>完整结果</summary>
        <pre>{JSON.stringify(result, null, 2)}</pre>
      </details>
    </section>
  );
}

function ReportArtifact({ artifact }: { artifact: WorkspaceArtifact }) {
  const payload = artifact.payload ?? {};
  const mdUrl = stringOf(payload.md_url);
  const pdfUrl = stringOf(payload.pdf_url);
  const skipped = numberValue(payload.skipped_charts);
  return (
    <section className="report-artifact">
      <div className="stats-artifact__heading">
        <span>分析报告</span>
        <span className="artifact-status">已生成</span>
      </div>
      <div className="report-artifact__actions">
        {mdUrl && (
          <a href={fileUrl(mdUrl)} target="_blank" rel="noreferrer">下载 Markdown</a>
        )}
        {pdfUrl && (
          <a href={fileUrl(pdfUrl)} target="_blank" rel="noreferrer">下载 PDF</a>
        )}
      </div>
      {skipped !== null && skipped > 0 && (
        <p className="table-artifact__note">有 {skipped} 张图表因截图失败未纳入</p>
      )}
    </section>
  );
}

function ChatWelcome({ onUpload }: { onUpload: () => void }) {
  return (
    <div className="chat-welcome">
      <div className="chat-welcome__mark" aria-hidden="true">BI</div>
      <span className="chat-welcome__eyebrow">CHATBI WORKSPACE</span>
      <h1>从一份数据，开始一次分析对话</h1>
      <p>上传 Excel 后直接用自然语言提出需求，Agent 会自动规划并调用画像、统计、图表与报告工具，全过程可见。</p>
      <button type="button" onClick={onUpload}>
        <PaperclipIcon />
        上传 Excel 数据
      </button>
    </div>
  );
}

function UploadProgress() {
  return (
    <article className="message-row message-row--assistant">
      <div className="message-avatar" aria-hidden="true">BI</div>
      <div className="message-content">
        <div className="message-meta"><strong>ChatBI</strong></div>
        <div className="message-bubble upload-progress">
          <LoadingRing />
          <p>正在读取 Excel 并生成字段画像…</p>
        </div>
      </div>
    </article>
  );
}

// ── 工具函数 ──

function prettyArguments(value: unknown): string {
  const raw = typeof value === "string" ? value : JSON.stringify(value ?? {});
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function stringOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function formatNumber(value: unknown): string {
  if (typeof value !== "number") return String(value ?? "—");
  if (Number.isInteger(value)) return value.toLocaleString("zh-CN");
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function TypingCursor() {
  return <span className="typing-cursor" aria-label="正在生成">▍</span>;
}

function LoadingRing() {
  return <span className="loading-ring" aria-hidden="true" />;
}

function PaperclipIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="m20.5 11.5-8.8 8.8a6 6 0 0 1-8.5-8.5l9.5-9.5a4 4 0 0 1 5.7 5.7l-9.6 9.5a2 2 0 0 1-2.8-2.8l8.8-8.8" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="m5 12 14-7-4 14-3-6-7-1Z" /><path d="m12 13 7-8" />
    </svg>
  );
}

function TableIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9h18M9 9v11" />
    </svg>
  );
}
