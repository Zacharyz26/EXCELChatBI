# ChatBI 智能体

中文优先的对话式 ChatBI 智能体：自然语言完成知识库问答、Excel 自动分析出可视化报告、多轮多模态追问、高级统计分析，并通过 MCP 协议调用工具。

> 开发约束以 [`CLAUDE.md`](./CLAUDE.md) 为准；完整设计见 [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)。

## 进度

A 轨（Dify 定位）MVP 主链路已闭环，前后端在浏览器跑通：

**已实现**
- **Excel 自动分析出图**：上传 → 数据画像（脱敏）→ DeepSeek 规划 → 真实数据聚合出 ECharts 图（含带错重规划）。
- **统计分析 + 中文解读**：趋势（STL/移动平均/预测）、异常（IQR/3σ/孤立森林/STL）、回归（OLS/Logit）；结果经**摘要门控**后交模型生成中文洞察。
- **知识库问答**：中文混合检索（向量 + BM25，RRF 融合）+ 重排，答案带引用、无结果如实告知。
- **前端**：React 18 + ECharts 5，覆盖上传/画像/出图/统计/问答全流程。
- **安全红线**：数据与推理分离（明细不进 LLM）、数值必来自工具、工具入参 schema 校验、数据脱敏与小分组保护、防注入、问答带引用；上传/摄入做了路径与大小硬化。

**未实现（后续阶段）**
- report 导出（Markdown/PDF、图表截图）、B 轨 LangGraph、多轮对话/追问（session）、真实 bge/Milvus、内部数据接入、鉴权/审计、沙箱、大表 DuckDB 下推、MCP-over-HTTP。

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
