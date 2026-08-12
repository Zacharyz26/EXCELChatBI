import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import type {
  AgentApproval,
  AgentAutonomyMode,
  AgentPlanDefinition,
  AgentRunStatus,
  AgentStepStatus,
  AgentTaskStep,
  AgentToolAudit,
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

const AUTONOMY_LABELS: Record<AgentAutonomyMode, string> = {
  assisted: "辅助模式",
  read_only: "标准只读",
  autonomous: "自主模式",
};

const HYPOTHESIS_KIND_LABELS = {
  trend: "趋势",
  anomaly: "异常",
  segment_comparison: "分组对比",
  correlation: "相关性",
} as const;

const HYPOTHESIS_STATUS_LABELS = {
  eligible: "可验证",
  needs_confirmation: "字段待确认",
  rejected: "未通过门禁",
} as const;

const HYPOTHESIS_EXECUTION_LABELS = {
  planned: "已绑定计划",
  running: "验证执行中",
  evidence_collected: "Evidence 已收集",
  supported: "Evidence 支持",
  not_supported: "Evidence 未支持",
  inconclusive: "证据不充分",
  partial: "部分完成",
  failed: "验证失败",
  cancelled: "已取消",
} as const;

const HYPOTHESIS_OUTCOME_LABELS = {
  untested: "尚未形成验证结论",
  supported: "支持候选",
  not_supported: "未支持候选",
  inconclusive: "无法确定",
} as const;

const HYPOTHESIS_FOLLOWUP_LABELS = {
  stop: "停止探索",
  degrade: "降级收尾",
  supplement_evidence: "建议补充 Evidence",
  propose_next: "建议下一候选",
} as const;

const HYPOTHESIS_FOLLOWUP_REASON_LABELS: Record<string, string> = {
  cancellation_requested: "任务已进入取消边界",
  hypothesis_supported: "当前候选已获支持",
  post_hoc_expansion_blocked: "禁止结论后自动扩展探索",
  hypothesis_not_supported: "当前候选未获支持",
  next_eligible_candidate: "仍有一个通过门禁的候选",
  candidate_set_exhausted: "候选集合已穷尽",
  screening_unavailable: "原候选筛选快照不可用",
  screening_context_mismatch: "候选的数据版本上下文不一致",
  evidence_incomplete: "现有 Evidence 不足以形成结论",
  evidence_inconclusive: "Evidence 与 Verifier 无法确定结果",
  retryable_observation: "最近失败明确标记为可重试",
  bounded_retry_available: "预算允许一次有界补证",
  non_retryable_failure: "最近失败不允许自动重试",
  tool_budget_exhausted: "共享工具预算已耗尽",
  replan_budget_exhausted: "重规划预算已耗尽",
  cancellation_boundary_unsettled: "取消树尚未进入可安全跟进的终态",
};

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
  const submitRunFeedback = useWorkspaceStore((state) => state.submitActiveRunFeedback);
  const startBranch = useWorkspaceStore((state) => state.startBranch);
  const autonomyMode = useWorkspaceStore((state) => state.autonomyMode);
  const [clarificationDraft, setClarificationDraft] = useState("");
  const [editingPlan, setEditingPlan] = useState(false);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [reasonDraft, setReasonDraft] = useState("");
  const [purposeDrafts, setPurposeDrafts] = useState<Record<string, string>>({});
  const [skippedStepIds, setSkippedStepIds] = useState<Set<string>>(new Set());
  const [approvalReasons, setApprovalReasons] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");
  const [feedbackRating, setFeedbackRating] = useState<"helpful" | "not_helpful" | null>(null);
  const [feedbackComment, setFeedbackComment] = useState("");
  const [branchDraft, setBranchDraft] = useState("");

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
  const comparisonRuns = useMemo(() => {
    if (!detail) return [];
    const parentId = detail.run.parent_run_id;
    return detail.related_runs.filter((candidate) => (
      candidate.run_id === detail.run.run_id
      || candidate.run_id === parentId
      || candidate.parent_run_id === detail.run.run_id
      || (!!parentId && candidate.parent_run_id === parentId)
    )).slice(0, 8);
  }, [detail]);

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

  async function submitFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!feedbackRating || busy) return;
    try {
      await submitRunFeedback(feedbackRating, feedbackComment.trim() || null);
      setFeedbackRating(null);
      setFeedbackComment("");
      setNotice("反馈已绑定当前 Run 的 Evidence/Artifact，并会用于后续分支规划。");
    } catch {
      /* 共享错误区域展示版本或引用冲突。 */
    }
  }

  async function submitBranch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const goal = branchDraft.trim();
    if (!detail || !goal || busy) return;
    setBranchDraft("");
    onClose();
    await startBranch(detail.run.run_id, goal);
  }

  function adoptFollowupGoal() {
    const goal = detail?.hypothesis_followup?.suggested_goal;
    if (!goal) return;
    setBranchDraft(goal);
    setNotice("后续动作已填入新分支目标；确认内容后再显式创建，不会自动调用工具。");
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
                {` · ${AUTONOMY_LABELS[detail.run.autonomy_mode]}`}
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
                    {busy === "resume"
                      ? "恢复中…"
                      : detail.run.autonomy_mode === "assisted"
                        ? "确认计划并执行"
                        : "继续执行"}
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

            {detail.execution_control && (
              <section className="agent-section" aria-label="任务执行边界">
                <div className="agent-section__title">
                  <span>共享执行边界</span>
                  <small>control v{detail.execution_control.schema_version}</small>
                </div>
                <dl className="agent-execution-control">
                  <div>
                    <dt>预算</dt>
                    <dd>
                      {String(detail.run.usage.tool_attempts ?? 0)} / {detail.execution_control.max_tool_calls}
                    </dd>
                  </div>
                  <div>
                    <dt>并行上限</dt>
                    <dd>{detail.execution_control.max_parallelism}</dd>
                  </div>
                  <div>
                    <dt>数据版本</dt>
                    <dd>{detail.execution_control.data_version_hash
                      ? shortHash(detail.execution_control.data_version_hash)
                      : "未绑定"}</dd>
                  </div>
                  <div>
                    <dt>Evidence Ledger</dt>
                    <dd>v{detail.execution_control.evidence_ledger_version}</dd>
                  </div>
                </dl>
              </section>
            )}

            {detail.hypothesis_screening && (
              <section className="agent-section" aria-label="候选假设筛选">
                <div className="agent-section__title">
                  <span>候选假设</span>
                  <small>
                    {detail.hypothesis_screening.eligible_candidate_ids.length}
                    {` / ${detail.hypothesis_screening.candidates.length} 通过门禁`}
                  </small>
                </div>
                <p className="agent-hypothesis-disclaimer">
                  以下内容尚未验证，不是分析结论；确认后才会调用受治理工具取得 Evidence。
                </p>
                <div className="agent-hypothesis-list">
                  {detail.hypothesis_screening.candidates.map((candidate) => (
                    <article
                      key={candidate.hypothesis_id}
                      className={`agent-hypothesis-card agent-hypothesis-card--${candidate.status}`}
                    >
                      <div>
                        <strong>{HYPOTHESIS_KIND_LABELS[candidate.kind]}</strong>
                        <span>{HYPOTHESIS_STATUS_LABELS[candidate.status]}</span>
                      </div>
                      <p>{candidate.statement}</p>
                      <small>
                        {candidate.capability} · 预期 Evidence：{candidate.expected_evidence}
                      </small>
                      {detail.run.status === "waiting_user"
                        && clarification?.about === "hypothesis_selection"
                        && candidate.status === "eligible" && (
                          <button
                            type="button"
                            onClick={() => setClarificationDraft(candidate.statement)}
                            disabled={busy !== null}
                          >
                            选择此假设
                          </button>
                        )}
                    </article>
                  ))}
                </div>
              </section>
            )}

            {detail.hypothesis_execution && (
              <section className="agent-section" aria-label="候选假设验证状态">
                <div className="agent-section__title">
                  <span>假设验证链</span>
                  <small>{HYPOTHESIS_EXECUTION_LABELS[detail.hypothesis_execution.status]}</small>
                </div>
                <article
                  className={`agent-hypothesis-execution agent-hypothesis-execution--${detail.hypothesis_execution.status}`}
                >
                  <div>
                    <strong>{detail.hypothesis_execution.statement}</strong>
                    <span>
                      {HYPOTHESIS_OUTCOME_LABELS[detail.hypothesis_execution.outcome]}
                    </span>
                  </div>
                  <dl>
                    <div>
                      <dt>计划步骤</dt>
                      <dd>
                        v{detail.hypothesis_execution.execution_plan_version}
                        {` · ${detail.hypothesis_execution.logical_step_id}`}
                      </dd>
                    </div>
                    <div>
                      <dt>执行 / Evidence</dt>
                      <dd>
                        {detail.hypothesis_execution.invocation_ids.length}
                        {` / ${detail.hypothesis_execution.evidence_ids.length}`}
                      </dd>
                    </div>
                    <div>
                      <dt>Ledger</dt>
                      <dd>
                        {detail.hypothesis_execution.evidence_ledger_sequences.length > 0
                          ? detail.hypothesis_execution.evidence_ledger_sequences.join(", ")
                          : "尚无"}
                      </dd>
                    </div>
                    <div>
                      <dt>Verifier</dt>
                      <dd>{detail.hypothesis_execution.verification?.verdict ?? "等待验证"}</dd>
                    </div>
                  </dl>
                  {detail.hypothesis_execution.status === "evidence_collected" && (
                    <p>工具 Evidence 已到达，但 Verifier 尚未 PASS，不能作为最终结论。</p>
                  )}
                  {detail.hypothesis_execution.last_failure_code && (
                    <p>最近状态：{detail.hypothesis_execution.last_failure_code}</p>
                  )}
                </article>
              </section>
            )}

            {detail.hypothesis_followup && (
              <section className="agent-section" aria-label="结果驱动跟进">
                <div className="agent-section__title">
                  <span>结果驱动跟进</span>
                  <small>
                    {HYPOTHESIS_FOLLOWUP_LABELS[detail.hypothesis_followup.decision]}
                  </small>
                </div>
                <article
                  className={`agent-hypothesis-followup agent-hypothesis-followup--${detail.hypothesis_followup.decision}`}
                >
                  <p>
                    {detail.hypothesis_followup.reason_codes
                      .map((code) => HYPOTHESIS_FOLLOWUP_REASON_LABELS[code] ?? code)
                      .join("；")}。
                  </p>
                  {detail.hypothesis_followup.proposed_candidate && (
                    <div className="agent-hypothesis-followup__candidate">
                      <strong>{detail.hypothesis_followup.proposed_candidate.statement}</strong>
                      <small>
                        {detail.hypothesis_followup.proposed_candidate.capability}
                        {` · 预期 Evidence：${detail.hypothesis_followup.proposed_candidate.expected_evidence}`}
                      </small>
                    </div>
                  )}
                  <dl>
                    <div>
                      <dt>工具余量</dt>
                      <dd>
                        {detail.hypothesis_followup.limits.tool_calls_remaining}
                        {` / ${detail.hypothesis_followup.limits.max_tool_calls}`}
                      </dd>
                    </div>
                    <div>
                      <dt>重规划余量</dt>
                      <dd>
                        {detail.hypothesis_followup.limits.replans_remaining}
                        {` / ${detail.hypothesis_followup.limits.max_replans}`}
                      </dd>
                    </div>
                    <div>
                      <dt>自动执行</dt>
                      <dd>禁止</dd>
                    </div>
                  </dl>
                  {detail.hypothesis_followup.suggested_goal && (
                    <button
                      type="button"
                      onClick={adoptFollowupGoal}
                      disabled={busy !== null}
                    >
                      填入新分支目标
                    </button>
                  )}
                </article>
              </section>
            )}

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

            <section className="agent-section" aria-label="工具执行审计">
              <div className="agent-section__title">
                <span>工具、权限与证据</span>
                <small>{detail.tool_audits.length} 次调用</small>
              </div>
              {detail.tool_audits.length === 0 ? (
                <p className="agent-section__empty">当前任务还没有工具调用。</p>
              ) : (
                <div className="agent-tool-audits">
                  {detail.tool_audits.map((audit) => (
                    <ToolAudit key={audit.invocation_id} audit={audit} />
                  ))}
                </div>
              )}
            </section>

            <section className="agent-section" aria-label="分支对比">
              <div className="agent-section__title">
                <span>分析分支对比</span>
                <small>{comparisonRuns.length} 个相关 Run</small>
              </div>
              <div className="agent-branch-grid">
                {comparisonRuns.map((candidate) => (
                  <article
                    key={candidate.run_id}
                    className={candidate.run_id === detail.run.run_id
                      ? "agent-branch-card agent-branch-card--active"
                      : "agent-branch-card"}
                  >
                    <div>
                      <strong>{candidate.run_id === detail.run.run_id ? "当前" : "分支"}</strong>
                      <span>{RUN_STATUS_LABELS[candidate.status]}</span>
                    </div>
                    <p>{candidate.goal}</p>
                    <small>
                      {shortHash(candidate.run_id)} · v{candidate.plan_version}
                      {` · ${AUTONOMY_LABELS[candidate.autonomy_mode]}`}
                      {` · ${Number(candidate.usage.tool_calls ?? 0)} 次工具`}
                    </small>
                    {candidate.run_id !== detail.run.run_id && (
                      <button
                        type="button"
                        onClick={() => void refresh(candidate.run_id)}
                        disabled={busy !== null}
                        aria-label={`查看分支 ${shortHash(candidate.run_id)}`}
                      >
                        查看计划、证据与反馈
                      </button>
                    )}
                  </article>
                ))}
              </div>
              {TERMINAL_STATUSES.has(detail.run.status) && (
                <form className="agent-branch-form" onSubmit={(event) => void submitBranch(event)}>
                  <label>
                    新分支目标
                    <textarea
                      value={branchDraft}
                      onChange={(event) => setBranchDraft(event.target.value)}
                      maxLength={20_000}
                      placeholder="说明要调整的假设、方法或交付结果"
                      aria-label="新分支目标"
                      required
                    />
                  </label>
                  <p>将使用当前选择的“{AUTONOMY_LABELS[autonomyMode]}”，并继承本 Run 的反馈作为规划上下文。</p>
                  <button type="submit" disabled={!branchDraft.trim() || busy !== null}>
                    创建分析分支
                  </button>
                </form>
              )}
            </section>

            <section className="agent-section" aria-label="结果反馈">
              <div className="agent-section__title">
                <span>结果反馈闭环</span>
                <small>{detail.feedback.length} 条</small>
              </div>
              {TERMINAL_STATUSES.has(detail.run.status) ? (
                <form className="agent-feedback-form" onSubmit={(event) => void submitFeedback(event)}>
                  <div role="radiogroup" aria-label="结果评分">
                    <button
                      type="button"
                      role="radio"
                      aria-checked={feedbackRating === "helpful"}
                      className={feedbackRating === "helpful" ? "is-selected" : ""}
                      onClick={() => setFeedbackRating("helpful")}
                    >
                      有帮助
                    </button>
                    <button
                      type="button"
                      role="radio"
                      aria-checked={feedbackRating === "not_helpful"}
                      className={feedbackRating === "not_helpful" ? "is-selected" : ""}
                      onClick={() => setFeedbackRating("not_helpful")}
                    >
                      需改进
                    </button>
                  </div>
                  <label>
                    补充说明
                    <textarea
                      value={feedbackComment}
                      onChange={(event) => setFeedbackComment(event.target.value)}
                      maxLength={1000}
                      placeholder="哪些假设、方法或结果需要保留或调整？"
                      aria-label="反馈说明"
                    />
                  </label>
                  <button type="submit" disabled={!feedbackRating || busy !== null}>
                    {busy === "feedback" ? "提交中…" : "提交反馈"}
                  </button>
                </form>
              ) : (
                <p className="agent-section__empty">任务进入终态后可提交反馈。</p>
              )}
              {detail.feedback.length > 0 && (
                <div className="agent-feedback-history">
                  {[...detail.feedback].reverse().slice(0, 5).map((feedback) => (
                    <article key={feedback.feedback_id}>
                      <strong>{feedback.rating === "helpful" ? "有帮助" : "需改进"}</strong>
                      <span>{formatDate(feedback.created_at)}</span>
                      {feedback.comment && <p>{feedback.comment}</p>}
                      <small>{feedback.evidence_ids.length} Evidence · {feedback.artifact_ids.length} Artifact</small>
                    </article>
                  ))}
                </div>
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

function ToolAudit({ audit }: { audit: AgentToolAudit }) {
  const health = audit.gateway_health
    ? `${audit.gateway_health}${audit.gateway_generation === null ? "" : ` · g${audit.gateway_generation}`}`
    : audit.status === "running" ? "等待执行结果" : "未记录";
  return (
    <article className="agent-tool-audit">
      <div className="agent-tool-audit__heading">
        <div>
          <strong>{audit.service_name ?? "受治理工具服务"}</strong>
          <span>{audit.tool_name}{audit.tool_version && ` v${audit.tool_version}`}</span>
        </div>
        <span className={`agent-step-status agent-step-status--${toolStatusClass(audit.status)}`}>
          {toolStatusLabel(audit.status)}
        </span>
      </div>
      <dl>
        <div><dt>风险</dt><dd>{audit.risk_level ?? "未记录"}</dd></div>
        <div><dt>权限</dt><dd>{audit.required_permissions.join("、") || "未记录"}</dd></div>
        <div><dt>Gateway</dt><dd>{health}{audit.degraded ? " · 降级" : ""}</dd></div>
        <div><dt>传输</dt><dd>{audit.transport ?? "未记录"}</dd></div>
        <div><dt>调度</dt><dd>{audit.parallel ? "受控并行" : "顺序执行"}</dd></div>
        <div>
          <dt>数据版本</dt>
          <dd>{audit.data_version_hash ? shortHash(audit.data_version_hash) : "未记录"}</dd>
        </div>
      </dl>
      <p>
        {audit.evidence_id
          ? `Evidence ${shortHash(audit.evidence_id)}`
          : "Evidence 尚未生成"}
        {audit.evidence_ledger_sequence !== null
          && ` · Ledger #${audit.evidence_ledger_sequence}`}
        {audit.artifact_id && ` · Artifact ${shortHash(audit.artifact_id)}`}
      </p>
    </article>
  );
}

function toolStatusClass(status: AgentToolAudit["status"]): AgentStepStatus {
  if (status === "succeeded") return "completed";
  if (status === "running") return "running";
  return "failed";
}

function toolStatusLabel(status: AgentToolAudit["status"]): string {
  if (status === "succeeded") return "已验证";
  if (status === "running") return "执行中";
  if (status === "unknown") return "结果未知";
  return "失败";
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
