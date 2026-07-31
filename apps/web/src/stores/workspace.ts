import { create, type StoreApi } from "zustand";
import {
  answerAgentClarification,
  cancelAgentRun,
  createConversation as createConversationRequest,
  createProject as createProjectRequest,
  decideAgentApproval,
  deleteConversation as deleteConversationRequest,
  deleteDataset as deleteDatasetRequest,
  getAgentRun,
  getAgentRunEvents,
  getConversation,
  listAgentApprovals,
  listConversations,
  listDatasets,
  listProjects,
  pauseAgentRun,
  resumeAgentRun,
  retryAgentStep,
  reviseAgentPlan,
  streamChat,
  updateConversation as updateConversationRequest,
  updateDataset as updateDatasetRequest,
  uploadExcel,
} from "@/api/client";
import type {
  AgentApproval,
  AgentClarification,
  AgentPlanDefinition,
  AgentRunDetail,
  AgentRunStatus,
  AgentTaskEvent,
  ChatStreamEvent,
  LiveTurnItem,
  ToolStep,
  WorkspaceArtifact,
  WorkspaceConversation,
  WorkspaceDataset,
  WorkspaceMessage,
  WorkspaceProject,
} from "@/types";

interface WorkspaceState {
  initialized: boolean;
  loading: boolean;
  uploading: boolean;
  streaming: boolean;
  error: string | null;
  projects: WorkspaceProject[];
  conversations: WorkspaceConversation[];
  datasets: WorkspaceDataset[];
  messages: WorkspaceMessage[];
  artifacts: WorkspaceArtifact[];
  activeProjectId: string | null;
  activeConversationId: string | null;
  /** 正在流式进行的 Agent 轮次卡片（理解/执行/工件/正文）；结束后并入 messages。 */
  liveTurn: LiveTurnItem[];
  /** 当前对话最近一次已知 TaskRun；runId 在收到响应头时即建立，详情按需刷新。 */
  activeRunId: string | null;
  activeRun: AgentRunDetail | null;
  approvals: AgentApproval[];
  pendingClarification: AgentClarification | null;
  collaborationBusy: string | null;
  initialize: () => Promise<void>;
  selectProject: (projectId: string) => Promise<void>;
  addProject: (name: string) => Promise<void>;
  addConversation: () => Promise<void>;
  selectConversation: (conversationId: string) => Promise<void>;
  renameConversation: (conversationId: string, title: string) => Promise<void>;
  removeConversation: (conversationId: string) => Promise<void>;
  /** 上下文面板当前查看的数据集（点击侧边栏条目设置） */
  activeDatasetRef: string | null;
  selectDataset: (datasetRef: string) => void;
  renameDataset: (datasetRef: string, filename: string) => Promise<void>;
  /** 删除数据集；被引用时不删除并返回后端的影响面警告文案（由调用方二次确认后 force） */
  removeDataset: (datasetRef: string, force?: boolean) => Promise<string | null>;
  uploadFile: (file: File) => Promise<void>;
  sendMessage: (message: string) => Promise<void>;
  refreshActiveRun: (runId?: string) => Promise<void>;
  pauseActiveRun: () => Promise<void>;
  resumeActiveRun: () => Promise<void>;
  cancelActiveRun: () => Promise<void>;
  answerClarification: (answer: unknown) => Promise<void>;
  retryActiveStep: (stepId: string) => Promise<void>;
  reviseActivePlan: (
    plan: AgentPlanDefinition,
    reason: string,
    skippedStepIds: string[],
  ) => Promise<void>;
  decideApproval: (
    approvalId: string,
    decision: "approved" | "denied",
    reason: string,
  ) => Promise<void>;
  clearError: () => void;
}

let navigationSequence = 0;
let liveItemSequence = 0;

function nextItemId(): string {
  liveItemSequence += 1;
  return `live-${liveItemSequence}`;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  initialized: false,
  loading: false,
  uploading: false,
  streaming: false,
  error: null,
  projects: [],
  conversations: [],
  datasets: [],
  messages: [],
  artifacts: [],
  activeProjectId: null,
  activeConversationId: null,
  activeDatasetRef: null,
  liveTurn: [],
  activeRunId: null,
  activeRun: null,
  approvals: [],
  pendingClarification: null,
  collaborationBusy: null,

  initialize: async () => {
    if (get().initialized || get().loading) return;
    set({ loading: true, error: null });
    try {
      let projects = await listProjects();
      if (projects.length === 0) {
        projects = [await createProjectRequest("我的分析项目")];
      }
      set({ projects });
      await get().selectProject(projects[0].id);
      set({ initialized: true });
    } catch (error) {
      set({ error: errorMessage(error) });
    } finally {
      set({ loading: false });
    }
  },

  selectProject: async (projectId) => {
    if (get().streaming || get().uploading) return;
    const requestSequence = ++navigationSequence;
    set({
      activeProjectId: projectId,
      activeConversationId: null,
      activeDatasetRef: null,
      conversations: [],
      datasets: [],
      messages: [],
      artifacts: [],
      liveTurn: [],
      activeRunId: null,
      activeRun: null,
      approvals: [],
      pendingClarification: null,
      loading: true,
      error: null,
    });
    try {
      const [projectConversations, datasets] = await Promise.all([
        listConversations(projectId),
        listDatasets(projectId),
      ]);
      if (requestSequence !== navigationSequence) return;

      let conversations = projectConversations;
      if (conversations.length === 0) {
        conversations = [await createConversationRequest(projectId)];
      }
      const conversationId = conversations[0].id;
      const detail = await getConversation(conversationId);
      if (requestSequence !== navigationSequence) return;
      set({
        conversations,
        datasets,
        activeConversationId: conversationId,
        messages: detail.messages,
        artifacts: detail.artifacts,
      });
      const rememberedRunId = rememberedRun(conversationId);
      if (rememberedRunId) await get().refreshActiveRun(rememberedRunId);
    } catch (error) {
      if (requestSequence === navigationSequence) set({ error: errorMessage(error) });
    } finally {
      if (requestSequence === navigationSequence) set({ loading: false });
    }
  },

  addProject: async (name) => {
    const cleanName = name.trim();
    if (!cleanName || get().streaming || get().uploading) return;
    set({ loading: true, error: null });
    try {
      const project = await createProjectRequest(cleanName);
      set((state) => ({ projects: [project, ...state.projects] }));
      await get().selectProject(project.id);
    } catch (error) {
      set({ error: errorMessage(error), loading: false });
    }
  },

  addConversation: async () => {
    const projectId = get().activeProjectId;
    if (!projectId || get().streaming || get().uploading) return;
    const requestSequence = ++navigationSequence;
    set({ loading: true, error: null });
    try {
      const conversation = await createConversationRequest(projectId);
      if (requestSequence !== navigationSequence) return;
      set((state) => ({
        conversations: [conversation, ...state.conversations],
        activeConversationId: conversation.id,
        messages: [],
        artifacts: [],
        liveTurn: [],
        activeRunId: null,
        activeRun: null,
        approvals: [],
        pendingClarification: null,
      }));
    } catch (error) {
      if (requestSequence === navigationSequence) set({ error: errorMessage(error) });
    } finally {
      if (requestSequence === navigationSequence) set({ loading: false });
    }
  },

  selectConversation: async (conversationId) => {
    if (
      conversationId === get().activeConversationId
      || get().streaming
      || get().uploading
    ) return;
    const requestSequence = ++navigationSequence;
    set({
      activeConversationId: conversationId,
      messages: [],
      artifacts: [],
      liveTurn: [],
      activeRunId: null,
      activeRun: null,
      approvals: [],
      pendingClarification: null,
      loading: true,
      error: null,
    });
    try {
      const detail = await getConversation(conversationId);
      if (requestSequence !== navigationSequence) return;
      set({ messages: detail.messages, artifacts: detail.artifacts });
      const rememberedRunId = rememberedRun(conversationId);
      if (rememberedRunId) await get().refreshActiveRun(rememberedRunId);
    } catch (error) {
      if (requestSequence === navigationSequence) set({ error: errorMessage(error) });
    } finally {
      if (requestSequence === navigationSequence) set({ loading: false });
    }
  },

  selectDataset: (datasetRef) => set({ activeDatasetRef: datasetRef }),

  renameDataset: async (datasetRef, filename) => {
    const clean = filename.trim();
    if (!clean || get().streaming || get().uploading) return;
    try {
      const updated = await updateDatasetRequest(datasetRef, clean);
      set((state) => ({
        datasets: state.datasets.map((item) => (item.ref === datasetRef ? updated : item)),
      }));
    } catch (error) {
      set({ error: errorMessage(error) });
    }
  },

  removeDataset: async (datasetRef, force = false) => {
    if (get().streaming || get().uploading) return null;
    try {
      const result = await deleteDatasetRequest(datasetRef, force);
      if (!result.deleted) return result.warning ?? "数据集正在被使用。";
    } catch (error) {
      set({ error: errorMessage(error) });
      return null;
    }
    set((state) => ({
      datasets: state.datasets.filter((item) => item.ref !== datasetRef),
      activeDatasetRef: state.activeDatasetRef === datasetRef ? null : state.activeDatasetRef,
    }));
    return null;
  },

  renameConversation: async (conversationId, title) => {
    const cleanTitle = title.trim();
    if (!cleanTitle || get().streaming || get().uploading) return;
    try {
      const updated = await updateConversationRequest(conversationId, cleanTitle);
      set((state) => ({
        conversations: state.conversations.map((item) => (
          item.id === conversationId ? updated : item
        )),
      }));
    } catch (error) {
      set({ error: errorMessage(error) });
    }
  },

  removeConversation: async (conversationId) => {
    if (get().streaming || get().uploading) return;
    const projectId = get().activeProjectId;
    try {
      await deleteConversationRequest(conversationId);
    } catch (error) {
      set({ error: errorMessage(error) });
      return;
    }
    const remaining = get().conversations.filter((item) => item.id !== conversationId);
    set({ conversations: remaining });
    if (get().activeConversationId !== conversationId) return;

    // 删的是当前对话：切到最近一条；一条不剩则新建（项目内始终有可用对话）
    const requestSequence = ++navigationSequence;
    set({
      activeConversationId: null,
      messages: [],
      artifacts: [],
      liveTurn: [],
      activeRunId: null,
      activeRun: null,
      approvals: [],
      pendingClarification: null,
      loading: true,
    });
    try {
      let next = remaining[0];
      if (!next && projectId) {
        next = await createConversationRequest(projectId);
        if (requestSequence !== navigationSequence) return;
        set({ conversations: [next] });
      }
      if (!next) return;
      const detail = await getConversation(next.id);
      if (requestSequence !== navigationSequence) return;
      set({
        activeConversationId: next.id,
        messages: detail.messages,
        artifacts: detail.artifacts,
      });
      const rememberedRunId = rememberedRun(next.id);
      if (rememberedRunId) await get().refreshActiveRun(rememberedRunId);
    } catch (error) {
      if (requestSequence === navigationSequence) set({ error: errorMessage(error) });
    } finally {
      if (requestSequence === navigationSequence) set({ loading: false });
    }
  },

  uploadFile: async (file) => {
    const projectId = get().activeProjectId;
    const conversationId = get().activeConversationId;
    if (!projectId || !conversationId || get().streaming || get().uploading) return;
    set({ uploading: true, error: null });
    try {
      await uploadExcel(file, { projectId, conversationId });
      const [datasets, conversations, detail] = await Promise.all([
        listDatasets(projectId),
        listConversations(projectId),
        getConversation(conversationId),
      ]);
      if (
        get().activeProjectId === projectId
        && get().activeConversationId === conversationId
      ) {
        set({
          datasets,
          conversations,
          messages: detail.messages,
          artifacts: detail.artifacts,
        });
      }
    } catch (error) {
      set({ error: errorMessage(error) });
    } finally {
      set({ uploading: false });
    }
  },

  sendMessage: async (message) => {
    const content = message.trim();
    const projectId = get().activeProjectId;
    const conversationId = get().activeConversationId;
    if (
      !content
      || !projectId
      || !conversationId
      || get().streaming
      || get().uploading
    ) return;

    const temporaryUserId = `pending-user-${crypto.randomUUID()}`;
    const now = new Date().toISOString();
    const pendingUser: WorkspaceMessage = {
      id: temporaryUserId,
      conversation_id: conversationId,
      role: "user",
      content,
      tool_calls: null,
      created_at: now,
    };
    let terminalEventReceived = false;
    let streamError: string | null = null;
    set((state) => ({
      messages: [...state.messages, pendingUser],
      streaming: true,
      liveTurn: [],
      activeRunId: null,
      activeRun: null,
      approvals: [],
      pendingClarification: null,
      error: null,
    }));

    try {
      await streamChat(conversationId, content, (event) => {
        if (get().activeConversationId !== conversationId) return;
        applyCollaborationEvent(event, conversationId, set);
        if (event.event === "meta") {
          applyMetaEvent(event, temporaryUserId, conversationId, set);
        } else if (event.event === "error") {
          terminalEventReceived = true;
          streamError = stringValue(event.data.message) || "对话生成失败，请重试。";
          set({ error: streamError });
        } else if (event.event === "done") {
          terminalEventReceived = true;
        } else {
          applyTurnEvent(event, set);
        }
      }, (runId) => {
        if (get().activeConversationId !== conversationId) return;
        rememberRun(conversationId, runId);
        set({ activeRunId: runId });
      });
      if (!terminalEventReceived) {
        streamError = "流式连接意外中断，请重试。";
      }
    } catch (error) {
      streamError = errorMessage(error);
    }

    try {
      // 工具轮可能产生了新消息、工件与衍生数据集：一并刷新
      const [detail, conversations, datasets] = await Promise.all([
        getConversation(conversationId),
        listConversations(projectId),
        listDatasets(projectId),
      ]);
      if (get().activeConversationId === conversationId) {
        const runId = get().activeRunId;
        set({
          messages: detail.messages,
          artifacts: detail.artifacts,
          conversations,
          datasets,
          liveTurn: [],
          error: streamError,
        });
        if (runId) await get().refreshActiveRun(runId);
      }
    } catch (error) {
      set({ error: streamError ?? errorMessage(error) });
    } finally {
      set({ streaming: false, liveTurn: [] });
    }
  },

  refreshActiveRun: async (requestedRunId) => {
    const runId = requestedRunId ?? get().activeRunId;
    const conversationId = get().activeConversationId;
    if (!runId || !conversationId) return;
    const ownsBusy = get().collaborationBusy === null;
    if (ownsBusy) set({ collaborationBusy: "refresh", error: null });
    try {
      const [detail, approvals, events] = await Promise.all([
        getAgentRun(runId),
        listAgentApprovals(runId),
        getAgentRunEvents(runId),
      ]);
      if (
        get().activeConversationId !== conversationId
        || detail.run.conversation_id !== conversationId
      ) return;
      rememberRun(conversationId, runId);
      set({
        activeRunId: runId,
        activeRun: detail,
        approvals,
        pendingClarification: pendingClarification(detail, events.events),
      });
    } catch (error) {
      if (requestedRunId && rememberedRun(conversationId) === requestedRunId) {
        forgetRun(conversationId);
      }
      set({ error: errorMessage(error) });
    } finally {
      if (ownsBusy && get().collaborationBusy === "refresh") {
        set({ collaborationBusy: null });
      }
    }
  },

  pauseActiveRun: async () => {
    const runId = get().activeRunId;
    if (!runId || get().collaborationBusy) return;
    set({ collaborationBusy: "pause", error: null });
    try {
      const fresh = await getAgentRun(runId);
      const response = await pauseAgentRun(runId, fresh.run.state_version);
      set({
        activeRun: { ...fresh, run: response.run },
        pendingClarification: null,
      });
      await get().refreshActiveRun(runId);
    } catch (error) {
      set({ error: errorMessage(error) });
    } finally {
      set({ collaborationBusy: null });
    }
  },

  resumeActiveRun: async () => {
    await continueActiveRun(
      get,
      set,
      "resume",
      async (fresh, onEvent) => {
        await resumeAgentRun(
          fresh.run.run_id,
          fresh.run.state_version,
          onEvent,
        );
      },
    );
  },

  cancelActiveRun: async () => {
    const runId = get().activeRunId;
    if (!runId || get().collaborationBusy) return;
    set({ collaborationBusy: "cancel", error: null });
    try {
      const fresh = await getAgentRun(runId);
      const response = await cancelAgentRun(runId, fresh.run.state_version);
      set({
        activeRun: { ...fresh, run: response.run },
        pendingClarification: null,
      });
      await get().refreshActiveRun(runId);
    } catch (error) {
      set({ error: errorMessage(error) });
    } finally {
      set({ collaborationBusy: null });
    }
  },

  answerClarification: async (answer) => {
    const clarification = get().pendingClarification;
    if (!clarification) return;
    await continueActiveRun(
      get,
      set,
      "clarification",
      async (fresh, onEvent) => {
        await answerAgentClarification(
          fresh.run.run_id,
          fresh.run.state_version,
          clarification.question_id,
          clarification.resume_token,
          answer,
          onEvent,
        );
      },
    );
  },

  retryActiveStep: async (stepId) => {
    await continueActiveRun(
      get,
      set,
      "retry",
      async (fresh, onEvent) => {
        await retryAgentStep(
          fresh.run.run_id,
          fresh.run.state_version,
          stepId,
          onEvent,
        );
      },
    );
  },

  reviseActivePlan: async (plan, reason, skippedStepIds) => {
    const runId = get().activeRunId;
    if (!runId || get().collaborationBusy) return;
    set({ collaborationBusy: "plan", error: null });
    try {
      const fresh = await getAgentRun(runId);
      const response = await reviseAgentPlan(
        runId,
        fresh.run.state_version,
        plan,
        reason,
        skippedStepIds,
      );
      set({
        activeRun: {
          ...fresh,
          run: response.run,
          plan: response.plan,
          steps: response.steps,
        },
      });
    } catch (error) {
      set({ error: errorMessage(error) });
      throw error;
    } finally {
      set({ collaborationBusy: null });
    }
  },

  decideApproval: async (approvalId, decision, reason) => {
    const runId = get().activeRunId;
    if (!runId || get().collaborationBusy) return;
    set({ collaborationBusy: `approval-${decision}`, error: null });
    try {
      const [fresh, currentApprovals] = await Promise.all([
        getAgentRun(runId),
        listAgentApprovals(runId),
      ]);
      const approval = currentApprovals.find(
        (item) => item.approval_id === approvalId,
      );
      if (!approval) throw new Error("授权请求已不存在或当前主体无权查看。");
      const response = await decideAgentApproval(
        runId,
        fresh.run.state_version,
        approvalId,
        approval.version,
        decision,
        reason,
      );
      set({
        activeRun: { ...fresh, run: response.run },
        approvals: currentApprovals.map((item) => (
          item.approval_id === approvalId ? response.approval : item
        )),
      });
    } catch (error) {
      set({ error: errorMessage(error) });
      throw error;
    } finally {
      set({ collaborationBusy: null });
    }
  },

  clearError: () => set({ error: null }),
}));

type WorkspaceSetter = StoreApi<WorkspaceState>["setState"];
type WorkspaceGetter = StoreApi<WorkspaceState>["getState"];

async function continueActiveRun(
  get: WorkspaceGetter,
  set: WorkspaceSetter,
  operation: string,
  execute: (
    fresh: AgentRunDetail,
    onEvent: (event: ChatStreamEvent) => void,
  ) => Promise<void>,
): Promise<void> {
  const runId = get().activeRunId;
  const conversationId = get().activeConversationId;
  const projectId = get().activeProjectId;
  if (
    !runId
    || !conversationId
    || !projectId
    || get().collaborationBusy
    || get().uploading
  ) return;

  let streamError: string | null = null;
  set({
    collaborationBusy: operation,
    streaming: true,
    liveTurn: [],
    error: null,
  });
  try {
    const fresh = await getAgentRun(runId);
    if (fresh.run.conversation_id !== conversationId) {
      throw new Error("当前 TaskRun 不属于活动对话。");
    }
    set({ activeRun: fresh });
    await execute(fresh, (event) => {
      if (get().activeConversationId !== conversationId) return;
      applyCollaborationEvent(event, conversationId, set);
      if (event.event === "error") {
        streamError = stringValue(event.data.message)
          || "任务继续执行失败，请刷新状态后重试。";
        set({ error: streamError });
      } else if (event.event !== "meta" && event.event !== "done") {
        applyTurnEvent(event, set);
      }
    });
  } catch (error) {
    streamError = errorMessage(error);
  }

  try {
    const [detail, conversations, datasets] = await Promise.all([
      getConversation(conversationId),
      listConversations(projectId),
      listDatasets(projectId),
    ]);
    if (get().activeConversationId === conversationId) {
      set({
        messages: detail.messages,
        artifacts: detail.artifacts,
        conversations,
        datasets,
        liveTurn: [],
        error: streamError,
      });
      await get().refreshActiveRun(runId);
    }
  } catch (error) {
    set({ error: streamError ?? errorMessage(error) });
  } finally {
    set({
      streaming: false,
      liveTurn: [],
      collaborationBusy: null,
    });
  }
}

function applyCollaborationEvent(
  event: ChatStreamEvent,
  conversationId: string,
  set: WorkspaceSetter,
): void {
  const runId = stringValue(event.data.run_id);
  if (runId) rememberRun(conversationId, runId);
  const payload = objectValue(event.data.payload);
  const nextStatus = runStatusForEvent(event);
  const clarification = event.event === "waiting_user"
    ? clarificationFromPayload(payload, numberValue(event.data.sequence) ?? 0)
    : undefined;

  set((state) => {
    const activeRunId = runId || state.activeRunId;
    const activeRun = (
      state.activeRun
      && activeRunId === state.activeRun.run.run_id
      && nextStatus
    )
      ? {
        ...state.activeRun,
        run: { ...state.activeRun.run, status: nextStatus },
      }
      : state.activeRun;
    return {
      activeRunId,
      activeRun,
      pendingClarification: event.event === "clarification.answered"
        ? null
        : clarification ?? state.pendingClarification,
    };
  });
}

function runStatusForEvent(event: ChatStreamEvent): AgentRunStatus | undefined {
  if (event.event === "done" || event.event === "error") {
    const status = stringValue(event.data.run_status);
    return isRunStatus(status) ? status : undefined;
  }
  const statuses: Record<string, AgentRunStatus> = {
    "run.created": "planning",
    "run.started": "running",
    "run.resumed": "running",
    "run.paused": "paused",
    "approval.requested": "paused",
    "approval.waiting": "paused",
    waiting_user: "waiting_user",
    "clarification.answered": "planning",
    "verification.started": "verifying",
    "run.completed": "completed",
    "run.blocked": "blocked",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
  };
  return statuses[event.event];
}

function isRunStatus(value: string): value is AgentRunStatus {
  return [
    "planning",
    "waiting_user",
    "running",
    "verifying",
    "paused",
    "completed",
    "blocked",
    "failed",
    "cancelled",
  ].includes(value);
}

function pendingClarification(
  detail: AgentRunDetail,
  events: AgentTaskEvent[],
): AgentClarification | null {
  if (detail.run.status !== "waiting_user") return null;
  const waiting = [...events].reverse().find(
    (event) => event.event_type === "waiting_user",
  );
  return waiting
    ? clarificationFromPayload(waiting.payload, waiting.sequence) ?? null
    : null;
}

function clarificationFromPayload(
  payload: Record<string, unknown>,
  sequence: number,
): AgentClarification | undefined {
  const questionId = stringValue(payload.question_id);
  const question = stringValue(payload.question);
  const resumeToken = stringValue(payload.resume_token);
  if (!questionId || !question || !resumeToken) return undefined;
  return {
    question_id: questionId,
    question,
    reason: stringValue(payload.reason),
    about: stringValue(payload.about),
    resume_token: resumeToken,
    answer_schema: objectValue(payload.answer_schema),
    sequence,
  };
}

function runStorageKey(conversationId: string): string {
  return `chatbi.agentRun.${conversationId}`;
}

function rememberRun(conversationId: string, runId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(runStorageKey(conversationId), runId);
  } catch {
    /* 无存储权限时保留当前内存状态。 */
  }
}

function rememberedRun(conversationId: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(runStorageKey(conversationId));
  } catch {
    return null;
  }
}

function forgetRun(conversationId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(runStorageKey(conversationId));
  } catch {
    /* 忽略不可写存储。 */
  }
}

function applyMetaEvent(
  event: ChatStreamEvent,
  temporaryUserId: string,
  conversationId: string,
  set: WorkspaceSetter,
): void {
  const userMessageId = stringValue(event.data.user_message_id);
  const title = stringValue(event.data.title);
  set((state) => ({
    messages: state.messages.map((message) => (
      message.id === temporaryUserId && userMessageId
        ? { ...message, id: userMessageId }
        : message
    )),
    conversations: state.conversations.map((conversation) => (
      conversation.id === conversationId && title
        ? { ...conversation, title }
        : conversation
    )),
  }));
}

/** 把一条 14.5.3 透明度事件并入实时轮次卡片流。 */
function applyTurnEvent(event: ChatStreamEvent, set: WorkspaceSetter): void {
  if (event.event === "text.delta") {
    const delta = stringValue(event.data.delta);
    if (!delta) return;
    set((state) => {
      const items = [...state.liveTurn];
      const last = items[items.length - 1];
      if (last && last.kind === "text") {
        items[items.length - 1] = { ...last, content: `${last.content}${delta}` };
      } else {
        items.push({ kind: "text", id: nextItemId(), content: delta });
      }
      return { liveTurn: items };
    });
  } else if (event.event === "understanding") {
    const text = stringValue(event.data.text);
    if (!text) return;
    set((state) => {
      // 工具轮开场白此前以 text.delta 流出：就地转换为“理解卡”，避免重复展示
      const items = [...state.liveTurn];
      const last = items[items.length - 1];
      if (last && last.kind === "text") {
        items[items.length - 1] = { kind: "understanding", id: last.id, text };
      } else {
        items.push({ kind: "understanding", id: nextItemId(), text });
      }
      return { liveTurn: items };
    });
  } else if (event.event === "plan") {
    const steps = Array.isArray(event.data.steps) ? event.data.steps : [];
    const toolSteps: ToolStep[] = steps
      .filter((step): step is Record<string, unknown> => !!step && typeof step === "object")
      .map((step) => ({
        id: stringValue(step.id),
        tool: stringValue(step.tool),
        label: stringValue(step.label) || stringValue(step.tool),
        status: "pending",
      }));
    if (toolSteps.length === 0) return;
    set((state) => {
      if (
        state.liveTurn.some(
          (item) => item.kind === "tools" && item.source === "task_plan",
        )
      ) {
        return state;
      }
      return {
        liveTurn: [
          ...state.liveTurn,
          {
            kind: "tools",
            id: nextItemId(),
            steps: toolSteps,
            source: "tool_calls",
          },
        ],
      };
    });
  } else if (event.event === "plan.created" || event.event === "plan.revised") {
    const payload = objectValue(event.data.payload);
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    const plannedSteps: ToolStep[] = steps
      .filter((step): step is Record<string, unknown> => !!step && typeof step === "object")
      .map((step) => ({
        id: stringValue(step.step_id),
        tool: stringValue(step.capability),
        label: stringValue(step.purpose) || stringValue(step.capability),
        status: planStepStatus(step.status),
        summary: stringValue(step.status) === "skipped"
          ? "已根据执行结果跳过"
          : undefined,
        message: ["failed", "blocked"].includes(stringValue(step.status))
          ? "步骤未完成"
          : undefined,
        dependencies: stringArray(step.dependencies),
      }));
    if (plannedSteps.length === 0) return;
    const planVersion = numberValue(payload.plan_version);
    set((state) => {
      const withoutOlderPlan = state.liveTurn.filter(
        (item) => item.kind !== "tools" || item.source !== "task_plan",
      );
      return {
        liveTurn: [
          ...withoutOlderPlan,
          {
            kind: "tools",
            id: `plan-${stringValue(payload.plan_id) || nextItemId()}`,
            steps: plannedSteps,
            source: "task_plan",
            planVersion,
          },
        ],
      };
    });
  } else if (event.event === "tool_start") {
    const stepId = stringValue(event.data.step_id) || stringValue(event.data.id);
    updateToolStep(set, stepId, (step) => ({
      ...step,
      tool: stringValue(event.data.tool) || step.tool,
      status: "running",
      fields: stringValue(event.data.fields) || step.fields,
      argsPreview: stringValue(event.data.args_preview) || step.argsPreview,
    }));
  } else if (event.event === "tool_end") {
    const ok = stringValue(event.data.status) === "ok";
    const stepId = stringValue(event.data.step_id) || stringValue(event.data.id);
    updateToolStep(set, stepId, (step) => ({
      ...step,
      status: ok ? "ok" : "error",
      summary: stringValue(event.data.summary) || step.summary,
      message: stringValue(event.data.message) || step.message,
    }));
  } else if (event.event === "artifact") {
    const artifact = event.data as unknown as WorkspaceArtifact;
    if (!artifact || typeof artifact.id !== "string") return;
    set((state) => ({
      liveTurn: [...state.liveTurn, { kind: "artifact", id: nextItemId(), artifact }],
    }));
  }
}

function updateToolStep(
  set: WorkspaceSetter,
  stepId: string,
  update: (step: ToolStep) => ToolStep,
): void {
  if (!stepId) return;
  set((state) => ({
    liveTurn: state.liveTurn.map((item) => (
      item.kind === "tools" && item.steps.some((step) => step.id === stepId)
        ? {
          ...item,
          steps: item.steps.map((step) => (step.id === stepId ? update(step) : step)),
        }
        : item
    )),
  }));
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function planStepStatus(value: unknown): ToolStep["status"] {
  const status = stringValue(value);
  if (status === "completed" || status === "skipped") return "ok";
  if (status === "failed" || status === "blocked") return "error";
  if (status === "running") return "running";
  return "pending";
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? value as Record<string, unknown>
    : {};
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
