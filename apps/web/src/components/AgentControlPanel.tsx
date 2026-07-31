import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import type {
  AgentApproval,
  AgentPlanDefinition,
  AgentRunStatus,
  AgentStepStatus,
  AgentTaskStep,
} from "@/types";

const RUN_STATUS_LABELS: Record<AgentRunStatus, string> = {
  planning: "规划中",
  waiting_user: "等待澄清",
  running: "执行中",
  verifying: "验证中",
  paused: "已暂停",
  completed: "已完成",
  blocked: "已阻塞",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_STATUS_LABELS: Record<AgentStepStatus, string> = {
  pending: "待执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
  blocked: "已阻塞",
};

const TERMINAL_STATUSES = new Set<AgentRunStatus>([
  "completed",
  "blocked",
  "failed",
  "cancelled",
]);

/** v2.5 4B：真实 TaskRun、计划、澄清、审批和运行控制的统一协作面板。 */
export function AgentControlPanel({ onClose }: { onClose: () => void }) {
  const activeRunId = useWorkspaceStore((state) => state.activeRunId);
  const detail = useWorkspaceStore((state) => state.activeRun);
  const approvals = useWorkspaceStore((state) => state.approvals);
  const clarification = useWorkspaceStore((state) => state.pendingClarification);
  const busy = useWorkspaceStore((state) => state.collaborationBusy);
  const error = useWorkspaceStore((state) => state.error);
  const refresh = useWorkspaceStore((state) => state.refreshActiveRun);
  const pause = useWorkspaceStore((state) => state.pauseActiveRun);
  const resume = useWorkspaceStore((state) => state.resumeActiveRun);
  const cancel = useWorkspaceStore((state) => state.cancelActiveRun);
  const answer = useWorkspaceStore((state) => state.answerClarification);
  const retryStep = useWorkspaceStore((state) => state.retryActiveStep);
  const revisePlan = useWorkspaceStore((state) => state.reviseActivePlan);
  const decideApproval = useWorkspaceStore((state) => state.decideApproval);
  const [clarificationDraft, setClarificationDraft] = useState("");
  const [editingPlan, setEditingPlan] = useState(false);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [reasonDraft, setReasonDraft] = useState("");
  const [purposeDrafts, setPurposeDrafts] = useState<Record<string, string>>({});
  const [skippedStepIds, setSkippedStepIds] = useState<Set<string>>(new Set());
  const [approvalReasons, setApprovalReasons] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (activeRunId) void refresh(activeRunId);
  }, [activeRunId, refresh]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (editingPlan) setEditingPlan(false);
      else onClose();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [editingPlan, onClose]);

  const pendingApprovals = useMemo(
    () => approvals.filter((approval) => approval.status === "pending"),
    [approvals],
  );
  const decidedApprovals = useMemo(
    () => approvals.filter((approval) => approval.status !== "pending").reverse(),
    [approvals],
  );
  const run = detail?.run;
  const canCancel = !!run && !TERMINAL_STATUSES.has(run.status);

  function beginPlanEdit() {
    if (!detail?.plan) return;
    setSummaryDraft(detail.plan.definition.summary);
    setReasonDraft("");
    setPurposeDrafts(Object.fromEntries(
      detail.plan.definition.steps.map((step) => [step.step_id, step.purpose]),
    ));
    setSkippedStepIds(new Set(
      detail.steps
        .filter((step) => step.status === "skipped")
        .map((step) => step.step_id),
    ));
    setNotice("");
    setEditingPlan(true);
  }

  async function submitClarification(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = clarificationDraft.trim();
    if (!value || busy) return;
    await answer(value);
    setClarificationDraft("");
  }

  async function submitPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail?.plan || !reasonDraft.trim() || busy) return;
    const plan: AgentPlanDefinition = {
      ...detail.plan.definition,
      summary: summaryDraft.trim(),
      steps: detail.plan.definition.steps.map((step) => ({
        ...step,
        purpose: (purposeDrafts[step.step_id] ?? step.purpose).trim(),
      })),
    };
    if (!plan.summary || plan.steps.some((step) => !step.purpose)) return;
    try {
      await revisePlan(
        plan,
        reasonDraft.trim(),
        [...skippedStepIds],
      );
      setEditingPlan(false);
      setNotice("计划已保存为不可变新版本；任务仍保持暂停。");
    } catch {
      /* 详细冲突由共享错误提示展示，保留编辑草稿。 */
    }
  }

  async function decide(
    approval: AgentApproval,
    decision: "approved" | "denied",
  ) {
    const reason = (approvalReasons[approval.approval_id] ?? "").trim();
    if (!reason || busy) return;
    try {
      await decideApproval(approval.approval_id, decision, reason);
      setApprovalReasons((current) => ({
        ...current,
        [approval.approval_id]: "",
      }));
      setNotice(
        decision === "approved"
          ? "授权已批准。请检查任务状态后显式点击“继续执行”。"
          : "授权已拒绝；对应高风险工具不会执行。",
      );
    } catch {
      /* 详细冲突由共享错误提示展示。 */
    }
  }

  return (
    <div className="agent-control-backdrop" role="presentation">
      <aside
        className="agent-control-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-control-title"
      >
        <header className="agent-control-header">
          <div>
            <span>HUMAN CONTROL</span>
            <h2 id="agent-control-title">任务协作</h2>
            <p>计划、澄清与审批均以服务端 TaskRun 为准</p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭任务协作">×</button>
        </header>

        <div className="agent-control-safety">
          浏览器不能直接授权工具。批准只更新后端 ApprovalRecord，任务必须显式恢复后才能继续。
        </div>

        {!activeRunId ? (
          <p className="agent-control-empty">
            当前对话还没有 TaskRun。发送一个分析目标后可在这里查看和干预执行。
          </p>
        ) : !detail ? (
          <p className="agent-control-empty">正在读取任务控制面…</p>
        ) : (
          <div className="agent-control-scroll">
            <section className="agent-run-card" aria-label="任务状态">
              <div className="agent-run-card__heading">
                <div>
                  <span className={`agent-status agent-status--${detail.run.status}`}>
                    {RUN_STATUS_LABELS[detail.run.status]}
                  </span>
                  <span>状态版本 {detail.run.state_version}</span>
                </div>
                <button
                  type="button"
                  onClick={() => void refresh()}
                  disabled={busy !== null}
                >
                  刷新
                </button>
              </div>
              <h3>{detail.run.goal}</h3>
              <p>
                Run {shortHash(detail.run.run_id)} · 计划 v{detail.run.plan_version}
                {detail.run.terminal_reason && ` · ${detail.run.terminal_reason}`}
              </p>
              <div className="agent-run-actions">
                {detail.run.status === "running" && (
                  <button
                    type="button"
                    onClick={() => void pause()}
                    disabled={busy !== null}
                  >
                    {busy === "pause" ? "暂停中…" : "暂停"}
                  </button>
                )}
                {detail.run.status === "paused" && (
                  <button
                    type="button"
                    className="agent-primary-button"
                    onClick={() => void resume()}
                    disabled={busy !== null || pendingApprovals.length > 0}
                    title={pendingApprovals.length > 0 ? "请先决定待处理授权" : ""}
                  >
                    {busy === "resume" ? "恢复中…" : "继续执行"}
                  </button>
                )}
                {canCancel && (
                  <button
                    type="button"
                    className="agent-danger-button"
                    onClick={() => {
                      if (window.confirm("取消当前任务？取消后不能恢复。")) void cancel();
                    }}
                    disabled={busy !== null}
                  >
                    {busy === "cancel" ? "取消中…" : "取消任务"}
                  </button>
                )}
              </div>
            </section>

            {error && <div className="agent-control-message agent-control-message--error">{error}</div>}
            {notice && <div className="agent-control-message">{notice}</div>}

            {detail.run.status === "waiting_user" && clarification && (
              <section className="agent-section agent-clarification-card">
                <div className="agent-section__title">
                  <span>需要你的输入</span>
                  <small>{clarification.about || clarification.question_id}</small>
                </div>
                <h3>{clarification.question}</h3>
                {clarification.reason && <p>{clarification.reason}</p>}
                <form onSubmit={(event) => void submitClarification(event)}>
                  <textarea
                    value={clarificationDraft}
                    onChange={(event) => setClarificationDraft(event.target.value)}
                    maxLength={20_000}
                    placeholder="输入明确答案，提交后继续同一个任务"
                    aria-label="澄清答案"
                    required
                  />
                  <button
                    type="submit"
                    className="agent-primary-button"
                    disabled={!clarificationDraft.trim() || busy !== null}
                  >
                    {busy === "clarification" ? "提交并恢复中…" : "提交答案并继续"}
                  </button>
                </form>
              </section>
            )}

            <section className="agent-section">
              <div className="agent-section__title">
                <span>执行计划</span>
                <div>
                  {detail.plan && <small>v{detail.plan.version}</small>}
                  {detail.run.status === "paused" && detail.plan && (
                    <button
                      type="button"
                      onClick={beginPlanEdit}
                      disabled={busy !== null}
                    >
                      修改计划
                    </button>
                  )}
                </div>
              </div>
              {detail.plan ? (
                <>
                  <h3>{detail.plan.definition.summary}</h3>
                  <div className="agent-plan-steps">
                    {detail.steps.map((step) => (
                      <PlanStep
                        key={step.persisted_step_id}
                        step={step}
                        paused={detail.run.status === "paused"}
                        busy={busy !== null}
                        onRetry={() => void retryStep(step.step_id)}
                      />
                    ))}
                  </div>
                </>
              ) : (
                <p className="agent-section__empty">计划尚未生成。</p>
              )}
            </section>

            <section className="agent-section">
              <div className="agent-section__title">
                <span>高风险审批</span>
                <small>{approvals.length} 条记录</small>
              </div>
              {pendingApprovals.length === 0 && decidedApprovals.length === 0 ? (
                <p className="agent-section__empty">当前任务没有高风险授权请求。</p>
              ) : (
                <div className="agent-approval-list">
                  {pendingApprovals.map((approval) => (
                    <article className="agent-approval agent-approval--pending" key={approval.approval_id}>
                      <ApprovalFacts approval={approval} />
                      <label>
                        决定原因
                        <textarea
                          value={approvalReasons[approval.approval_id] ?? ""}
                          onChange={(event) => setApprovalReasons((current) => ({
                            ...current,
                            [approval.approval_id]: event.target.value,
                          }))}
                          maxLength={500}
                          placeholder="说明批准范围或拒绝原因"
                          aria-label={`授权原因 ${approval.tool_name}`}
                        />
                      </label>
                      <div className="agent-approval__actions">
                        <button
                          type="button"
                          className="agent-danger-button"
                          disabled={
                            busy !== null
                            || !(approvalReasons[approval.approval_id] ?? "").trim()
                          }
                          onClick={() => void decide(approval, "denied")}
                        >
                          拒绝
                        </button>
                        <button
                          type="button"
                          className="agent-primary-button"
                          disabled={
                            busy !== null
                            || !(approvalReasons[approval.approval_id] ?? "").trim()
                          }
                          onClick={() => void decide(approval, "approved")}
                        >
                          批准一次
                        </button>
                      </div>
                    </article>
                  ))}
                  {decidedApprovals.slice(0, 5).map((approval) => (
                    <article className="agent-approval" key={approval.approval_id}>
                      <ApprovalFacts approval={approval} />
                      {approval.decision_reason && (
                        <p className="agent-approval__reason">{approval.decision_reason}</p>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </section>
          </div>
        )}

        {editingPlan && detail?.plan && (
          <form className="agent-plan-editor" onSubmit={(event) => void submitPlan(event)}>
            <div className="agent-plan-editor__heading">
              <div><span>IMMUTABLE REVISION</span><h3>修改计划</h3></div>
              <button type="button" onClick={() => setEditingPlan(false)} aria-label="关闭计划编辑">×</button>
            </div>
            <label>
              计划摘要
              <input
                value={summaryDraft}
                onChange={(event) => setSummaryDraft(event.target.value)}
                maxLength={500}
                required
              />
            </label>
            <div className="agent-plan-editor__steps">
              {detail.steps.map((step) => {
                const locked = ["completed", "skipped"].includes(step.status);
                return (
                  <fieldset key={step.step_id} disabled={locked || busy !== null}>
                    <legend>
                      {step.step_id} · {STEP_STATUS_LABELS[step.status]}
                    </legend>
                    <label>
                      步骤目的
                      <input
                        value={purposeDrafts[step.step_id] ?? step.definition.purpose}
                        onChange={(event) => setPurposeDrafts((current) => ({
                          ...current,
                          [step.step_id]: event.target.value,
                        }))}
                        maxLength={500}
                        required
                      />
                    </label>
                    <label className="agent-plan-editor__skip">
                      <input
                        type="checkbox"
                        checked={skippedStepIds.has(step.step_id)}
                        onChange={(event) => setSkippedStepIds((current) => {
                          const next = new Set(current);
                          if (event.target.checked) next.add(step.step_id);
                          else next.delete(step.step_id);
                          return next;
                        })}
                      />
                      跳过此步骤
                    </label>
                  </fieldset>
                );
              })}
            </div>
            <label>
              修改原因
              <textarea
                value={reasonDraft}
                onChange={(event) => setReasonDraft(event.target.value)}
                maxLength={500}
                placeholder="说明为什么调整计划"
                required
              />
            </label>
            <p>已完成或已跳过步骤不可改写；服务端会再次校验 capability、依赖和版本。</p>
            <div className="agent-plan-editor__actions">
              <button type="button" onClick={() => setEditingPlan(false)}>取消</button>
              <button
                type="submit"
                className="agent-primary-button"
                disabled={
                  busy !== null
                  || !summaryDraft.trim()
                  || !reasonDraft.trim()
                }
              >
                {busy === "plan" ? "保存中…" : "保存新版本"}
              </button>
            </div>
          </form>
        )}
      </aside>
    </div>
  );
}

function PlanStep({
  step,
  paused,
  busy,
  onRetry,
}: {
  step: AgentTaskStep;
  paused: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <article className="agent-plan-step">
      <div className="agent-plan-step__marker">{step.position + 1}</div>
      <div className="agent-plan-step__body">
        <div>
          <strong>{step.definition.purpose}</strong>
          <span className={`agent-step-status agent-step-status--${step.status}`}>
            {STEP_STATUS_LABELS[step.status]}
          </span>
        </div>
        <p>{step.definition.capability}</p>
        {step.definition.dependencies.length > 0 && (
          <small>依赖：{step.definition.dependencies.join("、")}</small>
        )}
        {paused && ["failed", "blocked"].includes(step.status) && (
          <button type="button" onClick={onRetry} disabled={busy}>
            仅重试此步骤
          </button>
        )}
      </div>
    </article>
  );
}

function ApprovalFacts({ approval }: { approval: AgentApproval }) {
  const statusLabel: Record<AgentApproval["status"], string> = {
    pending: "待决定",
    approved: "已批准",
    denied: "已拒绝",
    consumed: "已消费",
    revoked: "已撤销",
  };
  return (
    <>
      <div className="agent-approval__heading">
        <div>
          <span className={`agent-risk agent-risk--${approval.risk_level}`}>
            {approval.risk_level === "critical" ? "严重风险" : "高风险"}
          </span>
          <strong>{approval.tool_name}</strong>
        </div>
        <span>{statusLabel[approval.status]}</span>
      </div>
      <dl>
        <div><dt>计划/步骤</dt><dd>v{approval.plan_version} · {approval.step_id}</dd></div>
        <div><dt>有效期</dt><dd>{formatDate(approval.expires_at)}</dd></div>
        <div><dt>工具契约</dt><dd>{shortHash(approval.tool_schema_hash)}</dd></div>
        <div><dt>参数摘要</dt><dd>{shortHash(approval.parameter_summary_hash)}</dd></div>
      </dl>
    </>
  );
}

function shortHash(value: string): string {
  return value.length > 12 ? `${value.slice(0, 10)}…` : value;
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}
