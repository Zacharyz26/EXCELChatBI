import { create, type StoreApi } from "zustand";
import {
  createConversation as createConversationRequest,
  createProject as createProjectRequest,
  getConversation,
  listConversations,
  listDatasets,
  listProjects,
  streamChat,
  uploadExcel,
} from "@/api/client";
import type {
  ChatStreamEvent,
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
  streamingMessageId: string | null;
  initialize: () => Promise<void>;
  selectProject: (projectId: string) => Promise<void>;
  addProject: (name: string) => Promise<void>;
  addConversation: () => Promise<void>;
  selectConversation: (conversationId: string) => Promise<void>;
  uploadFile: (file: File) => Promise<void>;
  sendMessage: (message: string) => Promise<void>;
  clearError: () => void;
}

let navigationSequence = 0;

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
  streamingMessageId: null,

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
      conversations: [],
      datasets: [],
      messages: [],
      artifacts: [],
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
      streamingMessageId: null,
      error: null,
    }));

    try {
      await streamChat(conversationId, content, (event) => {
        if (get().activeConversationId !== conversationId) return;
        if (event.event === "meta") {
          applyMetaEvent(event, temporaryUserId, conversationId, set);
        } else if (event.event === "text.delta") {
          const delta = stringValue(event.data.delta);
          if (delta) appendDelta(delta, conversationId, set, get);
        } else if (event.event === "error") {
          terminalEventReceived = true;
          streamError = stringValue(event.data.message) || "对话生成失败，请重试。";
          set({ error: streamError });
        } else if (event.event === "done") {
          terminalEventReceived = true;
        }
      });
      if (!terminalEventReceived) {
        streamError = "流式连接意外中断，请重试。";
      }
    } catch (error) {
      streamError = errorMessage(error);
    }

    try {
      const [detail, conversations] = await Promise.all([
        getConversation(conversationId),
        listConversations(projectId),
      ]);
      if (get().activeConversationId === conversationId) {
        set({
          messages: detail.messages,
          artifacts: detail.artifacts,
          conversations,
          error: streamError,
        });
      }
    } catch (error) {
      set({ error: streamError ?? errorMessage(error) });
    } finally {
      set({ streaming: false, streamingMessageId: null });
    }
  },

  clearError: () => set({ error: null }),
}));

type WorkspaceSetter = StoreApi<WorkspaceState>["setState"];
type WorkspaceGetter = StoreApi<WorkspaceState>["getState"];

function applyMetaEvent(
  event: ChatStreamEvent,
  temporaryUserId: string,
  conversationId: string,
  set: WorkspaceSetter,
): void {
  const assistantMessageId = stringValue(event.data.message_id);
  const userMessageId = stringValue(event.data.user_message_id);
  const title = stringValue(event.data.title);
  if (!assistantMessageId) return;
  const assistant: WorkspaceMessage = {
    id: assistantMessageId,
    conversation_id: conversationId,
    role: "assistant",
    content: "",
    tool_calls: null,
    created_at: new Date().toISOString(),
  };
  set((state) => ({
    streamingMessageId: assistantMessageId,
    messages: [
      ...state.messages.map((message) => (
        message.id === temporaryUserId && userMessageId
          ? { ...message, id: userMessageId }
          : message
      )),
      assistant,
    ],
    conversations: state.conversations.map((conversation) => (
      conversation.id === conversationId && title
        ? { ...conversation, title }
        : conversation
    )),
  }));
}

function appendDelta(
  delta: string,
  conversationId: string,
  set: WorkspaceSetter,
  get: WorkspaceGetter,
): void {
  const messageId = get().streamingMessageId;
  if (!messageId) return;
  set((state) => ({
    messages: state.messages.map((message) => (
      message.id === messageId && message.conversation_id === conversationId
        ? { ...message, content: `${message.content}${delta}` }
        : message
    )),
  }));
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
