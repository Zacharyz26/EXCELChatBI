import { useEffect, useRef, useState, type FormEvent } from "react";
import type { AgentClarification } from "@/types";

interface ClarificationPromptProps {
  clarification: AgentClarification;
  busy: boolean;
  className?: string;
  onAnswer: (answer: string) => Promise<void>;
  onCancel?: () => Promise<void>;
}

/** Render durable waiting_user input from its answer schema instead of hiding it in chat text. */
export function ClarificationPrompt({
  clarification,
  busy,
  className = "",
  onAnswer,
  onCancel,
}: ClarificationPromptProps) {
  const [draft, setDraft] = useState("");
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const actionLocked = useRef(false);
  const options = clarificationOptions(clarification);
  const disabled = busy || pendingAction !== null;

  useEffect(() => {
    setDraft("");
    setPendingAction(null);
    actionLocked.current = false;
  }, [clarification.question_id, clarification.sequence]);

  async function submitAnswer(answer: string) {
    const clean = answer.trim();
    if (!clean || disabled || actionLocked.current) return;
    actionLocked.current = true;
    setPendingAction(clean);
    try {
      await onAnswer(clean);
    } finally {
      actionLocked.current = false;
      setPendingAction(null);
    }
  }

  async function submitOpenAnswer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await submitAnswer(draft);
  }

  async function cancelTask() {
    if (!onCancel || disabled || actionLocked.current) return;
    if (!window.confirm("取消当前任务？取消后不能恢复。")) return;
    actionLocked.current = true;
    setPendingAction("cancel");
    try {
      await onCancel();
    } finally {
      actionLocked.current = false;
      setPendingAction(null);
    }
  }

  return (
    <section
      className={`clarification-prompt ${className}`.trim()}
      role="region"
      aria-label="需要你的输入"
      aria-live="polite"
    >
      <div className="clarification-prompt__title">
        <span>需要你的输入</span>
        <small>{clarification.about || clarification.question_id}</small>
      </div>
      <h3>{clarification.question}</h3>
      {clarification.reason && <p>{clarification.reason}</p>}

      {options.length > 0 ? (
        <div className="clarification-options" role="group" aria-label="澄清选项">
          {options.map((option) => (
            <button
              key={option}
              type="button"
              className="clarification-option"
              onClick={() => void submitAnswer(option)}
              disabled={disabled}
              aria-pressed={pendingAction === option}
            >
              {pendingAction === option ? "正在提交…" : option}
            </button>
          ))}
        </div>
      ) : (
        <form onSubmit={(event) => void submitOpenAnswer(event)}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            maxLength={20_000}
            placeholder="输入明确答案，提交后继续同一个任务"
            aria-label="澄清答案"
            disabled={disabled}
            required
          />
          <button
            type="submit"
            className="agent-primary-button"
            disabled={!draft.trim() || disabled}
          >
            {pendingAction ? "提交并恢复中…" : "提交答案并继续"}
          </button>
        </form>
      )}

      {onCancel && (
        <div className="clarification-prompt__actions">
          <span>选择后继续原任务，或取消后重新提问。</span>
          <button
            type="button"
            className="clarification-cancel"
            onClick={() => void cancelTask()}
            disabled={disabled}
          >
            {pendingAction === "cancel" ? "取消中…" : "取消当前任务"}
          </button>
        </div>
      )}
    </section>
  );
}

function clarificationOptions(clarification: AgentClarification): string[] {
  const raw = clarification.answer_schema.enum;
  if (!Array.isArray(raw)) return [];
  return [...new Set(raw.filter(
    (value): value is string => typeof value === "string" && value.trim().length > 0,
  ).map((value) => value.trim()))];
}
