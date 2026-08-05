# 全栈部署与真实 E2E

> 状态：v2.4 生产结构与 v2.5 阶段 3A–3E 的记忆、固定引用、血缘和联合恢复门禁
> 已通过提交 `0b5980c` 的 Docker/Compose CI · 更新日期：2026-07-31

## 单机生产结构 Compose

根 `compose.yaml` 的默认拓扑是：

```text
宿主机 :8080
    │
  Web ── edge network ── API
                         │
                    internal app network
          ┌──────────┬──────────┬──────────┬──────────┬──────────┐
       data-tools stats-tools chart-tools report-tools knowledge-tools
```

- 默认只发布 Web；API 和 `/mcp` 均无宿主端口；
- API 使用 Bearer 登录，默认 token `chatbi-local-e2e-token-00000001` 只用于本地启动；
- 五个 MCP 服务各自使用独立 Bearer secret，并共同校验另一个上下文 HMAC secret；
- `storage-init` 先完成 SQLite 迁移和卷初始化，工具 readiness 全部通过后才启动 API；
- SQLite、upload、dataset、artifact、workspace-backup、KB 是六个独立卷。
  stats/knowledge 的 dataset/KB
  使用只读卷，data 仅写 dataset，chart/report 仅写 artifact。SQLite WAL 读者为维护
  `-shm` 协调必须挂载可写目录，但服务连接固定使用 SQLite URI `mode=ro`，不能执行写入；
- Python 容器非 root、只读根文件系统、drop all capabilities、禁止提权并带 CPU/内存/PID
  上限；服务退出有有界宽限期。

本地空环境可直接运行：

```bash
docker compose up --build
```

打开 `http://127.0.0.1:8080`。仓库内 `deploy/secrets/*.dev` 是公开开发凭据，只为满足
一条命令启动，绝不能用于 staging/production。

生产部署：

```bash
cp deploy/.env.production.example deploy/.env.production
# 创建六个独立 secret 文件，并把 *_FILE_PATH 改成真实绝对路径；
# 同时替换 AUTH_TOKENS_JSON 与模型供应商 secret。
docker compose --env-file deploy/.env.production up -d --build
docker compose ps
```

浏览器令牌只写入 `sessionStorage`。容器内服务地址使用 Docker DNS；不得把
`127.0.0.1` 当作另一容器。Web 只代理 `/api/`，不会公开 `/mcp`。

## 数据、权限与清理

- 上传源文件成功或失败后均删除；parquet 由 32 位服务端 `dataset_ref` 定位；
- MCP Server 在已签名 Host 上下文之外，再校验 project、conversation 和 dataset 登记归属；
- 衍生 dataset 文件由 data-tools 写，血缘登记回到 API 单一 SQLite writer；
- 报告文件由 report-tools 原子生成，API 在同一工具成功事务中提交 Artifact、Evidence 和
  report publication；
- 删除项目清理 parquet、sidecar 和报告；删除对话清理对话报告；
- 工作区备份必须先停止 API 和所有写服务，再一致覆盖 `chatbi-db`、
  `chatbi-datasets` 和 `chatbi-artifacts`；`chatbi-kb` 使用独立知识库备份流程。

工作区离线备份、校验和恢复：

```bash
# 先停止 API/MCP 等写服务
docker compose stop

backup_json="$(
  docker compose run --rm --no-deps storage-init \
    python -m apps.api.workspace_admin backup --service-stopped
)"

# 从 backup_json 的 path 字段取得容器内备份路径后校验和恢复
docker compose run --rm --no-deps storage-init \
  python -m apps.api.workspace_admin verify --input <backup-path>
docker compose run --rm --no-deps storage-init \
  python -m apps.api.workspace_admin restore \
  --input <backup-path> --service-stopped --yes --replace-files
docker compose up -d
```

manifest 会校验 SQLite schema、v4/v5 migration checksum、关键控制面表行数，以及每个
Dataset/Artifact 文件的大小和 SHA-256。恢复会先保存 `pre-restore-*` 覆盖前副本；
缺少任一显式确认或备份被篡改时均在覆盖前拒绝。

Compose 联合恢复探针还会固定一个 TaskRun 的 `compaction_id`，随后创建更新的压缩版本；
同时把 `HOST_COREF_V1` 与 `HOST_MEMORY_REF_V1` 写入不可变 TaskPlan。API 重启和离线恢复后
必须证明 TaskRun 仍读取旧压缩版本、最新版本未丢失、摘要 hash 未漂移，并保持相同
plan ID/version/hash、对象/记忆 resolution hash 和目标 Artifact。

## 三层 E2E

快速 UI 协议测试：

```bash
cd apps/web
pnpm test:e2e
```

本机 Web/API/Excel/SQLite/parquet 测试（不启动容器、不调用模型）：

```bash
cd apps/web
pnpm test:e2e:fullstack
```

生产结构容器门禁：

```bash
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web exec playwright install chromium
./scripts/run_compose_e2e.sh
```

最后一条从空卷构建并启动 Web、API、五个真实 MCP 服务和独立模型 HTTP 边界，浏览器完成
“Bearer 登录→上传真实 Excel→结构化计划→MCP 工具→Evidence/Artifact→Markdown/PDF
报告→下载”。随后脚本先验证 API/report-tools 普通重启，再停止所有写服务，执行
工作区备份，主动移走 SQLite 和报告文件，恢复后验证：

- SQLite 中同一 run 仍为完成态；
- paused 恢复探针 TaskRun 与 non-empty MemorySnapshot 的 ID/content hash 不变；
- 不可变 TaskPlan 的 ID/version/hash 以及对象/记忆 resolution hash 不变；
- MemoryRecord 与 Artifact 文件仍可读取；
- `generate_report` 的 `step.completed` 只有一次，不重复副作用；
- 恢复探针本身没有产生任何工具调用；
- 持久卷中的 PDF 仍可鉴权下载。

为了让 CI 可复现，只有外部非确定性模型供应商由
`apps/e2e_model` 的 OpenAI-compatible 确定性进程替代；Agent、MCP SDK/HTTP、工具、
SQLite、文件卷、Nginx、SSE 和浏览器均为真实实现，不使用 `page.route` 或 API mock。
该 fixture 不进入根 Compose，也不用于生产。

## 已知部署边界

- SQLite 和进程内对话锁仍要求 API 单 worker、单实例；
- API 重启可从 Checkpoint 恢复安全任务；活动且结果未知的副作用调用保持 fail-closed；
- 知识索引仍是实例级共享资源；Milvus 根 CPU/GPU profile 已由 5B-5 提供，租户级隔离留在
  后续阶段；
- BGE/GPU、外置 PostgreSQL/对象存储、多副本和发布供应链不属于 v2.4 2E；
- 前端 ECharts 主包仍约 1.38 MB，后续应动态拆包。
