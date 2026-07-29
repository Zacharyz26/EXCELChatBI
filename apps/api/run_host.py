"""API 进程内共享的阶段 2C TaskRun 执行宿主。"""

from apps.orchestrator.agent_loop import ConversationLockPool
from apps.orchestrator.run_manager import AgentRunManager

agent_run_manager = AgentRunManager()
conversation_locks = ConversationLockPool()
