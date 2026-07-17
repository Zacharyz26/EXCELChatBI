import { create, type StoreApi } from "zustand";
import {
  createConversation as createConversationRequest,
  createProject as createProjectRequest,
  deleteConversation as deleteConversationRequest,
  deleteDataset as deleteDatasetRequest,
  getConversation,
  listConversations,
  listDatasets,
  listProjects,
  streamChat,
  updateConversation as updateConversationRequest,
  updateDataset as updateDatasetRequest,
  uploadExcel,
} from "@/api/client";
import type {
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
      loading: true,
      error: null,
    });
    try {
      const detail = await getConversation(conversationId);
      if (requestSequence !== navigationSequence) return;
      set({ messages: detail.messages, artifacts: detail.artifacts });
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
    set({ activeConversationId: null, messages: [], artifacts: [], liveTurn: [], loading: true });
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
      error: null,
    }));

    try {
      await streamChat(conversationId, content, (event) => {
        if (get().activeConversationId !== conversationId) return;
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
        set({
          messages: detail.messages,
          artifacts: detail.artifacts,
          conversations,
          datasets,
          error: streamError,
        });
      }
    } catch (error) {
      set({ error: streamError ?? errorMessage(error) });
    } finally {
      set({ streaming: false, liveTurn: [] });
    }
  },

  clearError: () => set({ error: null }),
}));

type WorkspaceSetter = StoreApi<WorkspaceState>["setState"];

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
    set((state) => ({
      liveTurn: [...state.liveTurn, { kind: "tools", id: nextItemId(), steps: toolSteps }],
    }));
  } else if (event.event === "tool_start") {
    updateToolStep(set, stringValue(event.data.id), (step) => ({
      ...step,
      status: "running",
      fields: stringValue(event.data.fields) || step.fields,
      argsPreview: stringValue(event.data.args_preview) || step.argsPreview,
    }));
  } else if (event.event === "tool_end") {
    const ok = stringValue(event.data.status) === "ok";
    updateToolStep(set, stringValue(event.data.id), (step) => ({
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
