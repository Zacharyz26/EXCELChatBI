# ChatBI 智能体

中文优先的对话式 ChatBI 智能体：自然语言完成知识库问答、Excel 自动分析出可视化报告、多轮多模态追问、高级统计分析，并通过 MCP 协议调用工具。

> 开发约束以 [`CLAUDE.md`](./CLAUDE.md) 为准；完整设计见 [`docs/ChatBI设计文档.md`](./docs/ChatBI设计文档.md)。

## 进度

MVP 主链路已闭环，前后端在浏览器跑通。当前阶段：**聊天助手**（方案与六项拍板决策见设计文档第 13 章）。

**已实现**
- **Excel 自动分析出图**：上传 → 数据画像（脱敏）→ DeepSeek 规划 → 真实数据聚合出 ECharts 图（含带错重规划）。
- **统计分析四件套 + 中文解读**：趋势（STL/移动平均/预测）、异常（IQR/3σ/孤立森林/STL）、回归（OLS/Logit）、相关性（Pearson/Spearman）；结果经**摘要门控**后交模型生成中文洞察。
- **报告导出**：Markdown + PDF（WeasyPrint），由真实工具结果组装，report 工具零 LLM。
- **图表截图**：Playwright 无头浏览器服务端渲染。
- **知识库问答**：中文混合检索（向量 + BM25，RRF 融合）+ 重排，答案带引用、无结果如实告知（当前为 hashing/词面替身后端，语义升级开发中）。
- **前端**：React 18 + ECharts 5，覆盖上传/画像/出图/统计/报告/问答全流程。
- **安全红线**：数据与推理分离（原有端点门控不变；/chat 助手通道例外已拍板，见 CLAUDE.md 红线1）、数值必来自工具、工具入参 schema 校验、脱敏与小分组保护、防注入、问答带引用；上传/摄入做了路径与大小硬化。

**开发中（聊天助手阶段）**
- bge-m3 + Milvus Lite 语义检索（device 配置项 auto/cpu/cuda，权重离线侧载）。
- 内存版多轮 session（Redis 暂不上，触发条件见 CLAUDE.md 第 10 节）。
- `/chat` 自由问答助手（DeepSeek function-calling 编排，可调统计工具、查知识库、结合当前数据画像作答）。

**未实现（后续阶段）**
- query_dataset 自由取数（看真实提问再定）、复杂多步分析（LangGraph 或自研状态机）、图表追问（chart_registry 后置）、内部数据接入、鉴权/审计、沙箱、大表 DuckDB 下推、MCP-over-HTTP、MinIO/Redis/PostgreSQL 接入。

## 架构（五层）

```
前端(React) → 自研编排(FastAPI直调 + function-calling循环) + 模型路由 → MCP工具层(进程内Tool.invoke) → 治理安全层 → 存储层(当前本地落盘)
```

> Dify 已放弃（2026-07 拍板），编排全部自研；理由与记录见设计文档 5.2。

## 目录速览

| 路径 | 职责 |
|------|------|
| `apps/api` | FastAPI 网关 / BFF，对外 HTTP + SSE |
| `apps/orchestrator` | 自研编排：分析/统计/问答编排函数 + 聊天助手（开发中） |
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
cp config/data_policy.example.yaml config/data_policy.yaml  # 可选，不拷用内置宽松默认

# 3. 安装依赖（核心）
uv sync

# 4. 启动后端
uv run uvicorn apps.api.main:app --reload

# 5. 前端
cd apps/web && pnpm install && pnpm dev

# 6. 测试 / 检查
uv run pytest
uv run ruff check .
uv run mypy .
```

> 未装 uv/pytest 时，骨架冒烟测试也可用标准库直接跑：`python3 -m unittest discover -s tests`

## 环境准备（按功能启用的系统级依赖）

各重依赖按域拆在 optional-dependencies，按需安装；以下坑都踩过/预判过，别跳过。

```bash
# 统计分析（趋势/异常/回归/相关）
uv sync --extra stats

# 图表截图：Playwright chromium + 系统依赖 + 中文字体（缺字体则截图中文变豆腐块）
uv sync --extra chart-screenshot
uv run playwright install --with-deps chromium
sudo apt install fonts-noto-cjk

# 报告 PDF：WeasyPrint 需系统库；中文字体同上
uv sync --extra report
sudo apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0

# 语义检索（聊天助手阶段启用；FlagEmbedding 含 torch≈2GB，pymilvus 自带 Milvus Lite）
uv sync --extra rag
# 模型权重离线侧载（局域网无外网）：在有网机器预下载
#   BAAI/bge-m3（≈2.3GB）与 BAAI/bge-reranker-v2-m3（≈2.3GB），
# 拷入服务器本地目录，配 HF_HUB_OFFLINE=1 与模型路径；
# 推理 device 走配置（auto/cpu/cuda），本地开发与 GPU 服务器切换不改代码。
```
