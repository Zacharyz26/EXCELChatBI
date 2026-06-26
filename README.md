# ChatBI 智能体

中文优先的对话式 ChatBI 智能体：自然语言完成知识库问答、Excel 自动分析出可视化报告、多轮多模态追问、高级统计分析，并通过 MCP 协议调用工具。

> 开发约束以 [`CLAUDE.md`](./CLAUDE.md) 为准；完整设计见 [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)。
> 当前为 **MVP 脚手架**：仅有结构与接口骨架，业务逻辑标注 `NotImplementedError` / `TODO`，尚未实现。

## 架构（五层）

```
前端(React) → 编排双轨(Dify / LangGraph) + 模型路由 → MCP工具层 → 治理安全层 → 存储层
```

## 目录速览

| 路径 | 职责 |
|------|------|
| `apps/api` | FastAPI 网关 / BFF，对外 HTTP + SSE |
| `apps/orchestrator` | 意图路由 + 模型路由封装（B轨占位） |
| `apps/web` | React 18 + ECharts 5 前端（Vite + TS） |
| `mcp_servers/*` | 各 MCP 工具，独立可部署 |
| `packages/common` | 共享配置加载 + 结构化日志 |
| `packages/models` | 模型路由网关 + registry |
| `packages/governance` | schema 校验 / 权限 / 沙箱 / 审计（红线落点） |
| `packages/rag` | 中文检索：embedding / rerank / 分词 / 分块 |
| `packages/session` | 会话状态 / 上下文压缩 / 指代消解 |

## 快速开始

```bash
# 1. 安装 uv（若未安装）：https://docs.astral.sh/uv/
# 2. 配置环境变量
cp .env.example .env
cp config/models.example.yaml config/models.yaml   # 按需填写

# 3. 安装依赖
uv sync

# 4. 启动后端
uv run uvicorn apps.api.main:app --reload

# 5. 启动某个 MCP 工具服务（示例：统计）
uv run python -m mcp_servers.stats.server

# 6. 前端
cd apps/web && pnpm install && pnpm dev

# 7. 测试 / 检查
uv run pytest
uv run ruff check .
uv run mypy .
```

> 未装 uv/pytest 时，占位测试也可用标准库直接跑：`python3 -m unittest discover -s tests`
