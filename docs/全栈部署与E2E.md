# 全栈部署与真实 E2E

## 单机 Compose

根目录 `compose.yaml` 同时构建并运行：

- `api`：非 root FastAPI，包含统计、PDF、Playwright 与 Chromium；
- `web`：非 root Nginx 静态站点，通过 `/api/` 反向代理 HTTP、下载和 SSE；
- `chatbi-data`：SQLite、parquet、报告和本地知识索引持久卷。

本地验收可直接运行：

```bash
docker compose up --build
```

打开 `http://127.0.0.1:8080`。API 仅绑定本机
`http://127.0.0.1:8000`，浏览器正常通过 Web 容器的同源代理访问。

生产部署必须启用 Bearer 认证：

```bash
cp deploy/.env.production.example deploy/.env.production
# 编辑文件，替换 AUTH_TOKENS_JSON 与 DEEPSEEK_API_KEY
docker compose --env-file deploy/.env.production up -d --build
docker compose ps
```

`APP_ENV=staging/production` 会拒绝在 `AUTH_MODE=disabled` 下启动。浏览器会显示令牌
录入页，令牌只写入 `sessionStorage`，不会烘焙进 Web 镜像或持久化到磁盘。

默认 Compose 使用 hashing/lexical/local 知识库，确保单机无需外部服务即可启动。
需要 BGE + Milvus 时，应使用 `deploy/milvus/docker-compose.yml` 启动固定版本
Milvus，并为 API 镜像增加 `rag` 依赖、模型卷和容器可访问的 `MILVUS_URI`；不要在
容器内使用 `127.0.0.1` 指向另一服务。

## 数据与清理

- 上传的 `.xlsx/.xls` 只作临时解析输入，成功或失败后均删除；
- parquet 和 sidecar 由服务端生成的 32 位 `dataset_ref` 定位；
- 报告发布记录绑定项目，下载按项目成员鉴权；
- 删除项目会清理其 parquet、元数据和报告；删除对话会清理对话报告；
- 报告内嵌完成后立即删除图表截图，启动时再回收超过宽限期的遗留截图和原子写临时文件。

备份至少应覆盖 `chatbi-data` 卷。SQLite 与文件卷需要处于同一恢复点，否则启动对账会
报告缺失或未登记文件。

## 真实全栈 E2E

普通 `pnpm test:e2e` 保留快速 UI 协议测试。真实全栈测试不使用 `page.route` 或 API
mock，会启动独立 FastAPI、Vite 和 Chromium，生成真实 XLSX，并覆盖：

1. API/KV 存储 readiness；
2. Bearer 登录页、令牌校验与浏览器会话保存；
3. 前端自动创建真实项目和对话；
4. 浏览器 multipart 上传真实 Excel；
5. 后端解析、parquet 落盘、SQLite 原子登记；
6. 前端刷新并展示画像、消息和数据集。

```bash
cd apps/web
pnpm exec playwright install chromium
pnpm test:e2e:fullstack
```

测试数据只写入 `.data/e2e`；准备脚本在每次运行前仅清理这个固定目录。

## 已知部署边界

- 当前 SQLite、进程内对话锁和本地文件存储要求 API 单 worker、单实例；
- 执行中任务在超时、断连或进程重启后会安全收敛为失败/未知，尚不能从 Checkpoint
  自动续跑；`waiting_user` 和 `paused` 状态会跨重启保留；
- 项目、对话、数据集、任务和报告已隔离，知识库索引仍是实例级共享资源；
- 生产工具执行仍是进程内 `Tool.invoke`；MCP 已有同源契约与探针，但尚未切为生产传输；
- 前端 ECharts 仍形成约 1.38 MB 的主包，后续应按图表/报告卡片做动态拆包。
