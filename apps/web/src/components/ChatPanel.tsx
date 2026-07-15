import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceArtifact, WorkspaceMessage } from "@/types";

/** 阶段 1 对话主区：历史消息、画像工件、Excel 上传和纯 LLM SSE 正文。 */
export function ChatPanel() {
  const messages = useWorkspaceStore((state) => state.messages);
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const activeConversationId = useWorkspaceStore((state) => state.activeConversationId);
  const loading = useWorkspaceStore((state) => state.loading);
  const uploading = useWorkspaceStore((state) => state.uploading);
  const streaming = useWorkspaceStore((state) => state.streaming);
  const streamingMessageId = useWorkspaceStore((state) => state.streamingMessageId);
  const error = useWorkspaceStore((state) => state.error);
  const sendMessage = useWorkspaceStore((state) => state.sendMessage);
  const uploadFile = useWorkspaceStore((state) => state.uploadFile);
  const clearError = useWorkspaceStore((state) => state.clearError);
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
  }, [messages, streaming]);

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
        ) : messages.length === 0 ? (
          <ChatWelcome onUpload={() => fileInputRef.current?.click()} />
        ) : (
          <div className="message-list">
            {messages.map((message) => (
              <MessageItem
                key={message.id}
                message={message}
                artifacts={artifacts.filter((item) => item.message_id === message.id)}
                streaming={streaming && message.id === streamingMessageId}
              />
            ))}
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
            disabled={!activeConversationId || uploading || streaming}
            aria-label="上传 Excel"
            title="上传 Excel"
          >
            <PaperclipIcon />
          </button>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="描述你的数据问题…"
            rows={1}
            maxLength={20_000}
            disabled={!activeConversationId || loading}
            aria-label="消息内容"
          />
          <button
            type="submit"
            className="composer-send"
            disabled={!draft.trim() || !activeConversationId || uploading || streaming}
            aria-label={streaming ? "正在生成回复" : "发送消息"}
          >
            {streaming ? <LoadingRing /> : <SendIcon />}
          </button>
        </form>
        <p className="composer-hint">
          {uploading ? "正在解析并生成数据画像…" : "Enter 发送 · Shift + Enter 换行 · 当前阶段仅支持画像问答"}
        </p>
      </div>
    </section>
  );
}

function ChatWelcome({ onUpload }: { onUpload: () => void }) {
  return (
    <div className="chat-welcome">
      <div className="chat-welcome__mark" aria-hidden="true">BI</div>
      <span className="chat-welcome__eyebrow">CHATBI WORKSPACE</span>
      <h1>从一份数据，开始一次分析对话</h1>
      <p>上传 Excel 后会生成可追溯的数据画像。你也可以先提问；统计、图表和报告工具将在后续 Agent 阶段接入。</p>
      <button type="button" onClick={onUpload}>
        <PaperclipIcon />
        上传 Excel 数据
      </button>
    </div>
  );
}

function MessageItem({
  message,
  artifacts,
  streaming,
}: {
  message: WorkspaceMessage;
  artifacts: WorkspaceArtifact[];
  streaming: boolean;
}) {
  const isUser = message.role === "user";
  return (
    <article className={`message-row message-row--${isUser ? "user" : "assistant"}`}>
      {!isUser && <div className="message-avatar" aria-hidden="true">BI</div>}
      <div className="message-content">
        <div className="message-meta">
          <strong>{isUser ? "你" : "ChatBI"}</strong>
          <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
        </div>
        <div className="message-bubble">
          {message.content || streaming ? (
            <p>{message.content}{streaming && <TypingCursor />}</p>
          ) : null}
          {artifacts.map((artifact) => (
            artifact.type === "profile"
              ? <ProfileArtifact key={artifact.id} artifact={artifact} />
              : null
          ))}
        </div>
      </div>
    </article>
  );
}

function ProfileArtifact({ artifact }: { artifact: WorkspaceArtifact }) {
  const payload = artifact.payload ?? {};
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
      {names.length > 0 && (
        <div className="profile-artifact__fields">
          {names.slice(0, 8).map((name) => <span key={name}>{name}</span>)}
          {names.length > 8 && <span>+{names.length - 8}</span>}
        </div>
      )}
    </section>
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

function numberValue(value: unknown): number | null {
  return typeof value === "number" ? value : null;
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
