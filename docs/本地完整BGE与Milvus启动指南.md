# 本地完整 BGE 与 Docker Milvus 启动指南

> 状态：现行、已实机验证 · 更新日期：2026-08-24
> 验证环境：WSL2 Ubuntu 24.04、CPU、约 16 GiB 内存、Docker Desktop、Milvus 2.6.20

本指南对应 README 的“方案 B”：Milvus、etcd、MinIO 在 Docker 中运行，ChatBI API、完整
BGE embedding/reranker 和 Web 在本机或 WSL 中运行。该拓扑避免 API 与 knowledge-tools
容器各加载一份模型，适合内存有限的开发机。

## 1. 资源和端口

- Python 3.11、uv、Node.js 20、pnpm 9；
- Docker Engine/Compose 或启用 WSL integration 的 Docker Desktop；
- 建议给 WSL/宿主机分配至少 16 GiB 内存，并准备 8 GiB 以上空闲磁盘；两份模型权重
  实际约 4.4 GiB；
- 端口：Web `5173`、API `8000`、Milvus `19530`、Milvus health `9091`、MinIO
  `9000/9001`。

先确认：

```bash
python3 --version
uv --version
node --version
pnpm --version
docker compose version
```

## 2. 创建应用配置并安装依赖

```bash
cp .env.example .env
cp config/models.example.yaml config/models.yaml
cp config/data_policy.example.yaml config/data_policy.yaml
uv sync --extra rag
pnpm --dir apps/web install
```

`uv sync` 是精确同步；以后不要再单独运行无 extra 的 `uv sync`，否则会移除 BGE/Milvus
和统计依赖。

## 3. 精确侧载完整模型

直接把 Hugging Face 模型名交给 FlagEmbedding 可能连同 ONNX 等非运行必需文件一起下载。
下面只侧载本项目 CPU/CUDA 推理需要的完整官方权重和 tokenizer，并禁用容易在代理环境中
卡住的 Xet 下载路径。

### BGE-M3 embedding

```bash
HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=120 \
HF_HOME="$PWD/.data/model_cache/huggingface" \
HF_HUB_CACHE="$PWD/.data/model_cache/huggingface/hub" \
.venv/bin/python -c '
from huggingface_hub import snapshot_download
print(snapshot_download(
    repo_id="BAAI/bge-m3",
    local_dir=".data/models/bge-m3",
    cache_dir=".data/model_cache/huggingface/hub",
    allow_patterns=[
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "sentencepiece.bpe.model",
        "pytorch_model.bin", "colbert_linear.pt", "sparse_linear.pt",
        "modules.json", "1_Pooling/config.json",
        "config_sentence_transformers.json", "sentence_bert_config.json",
    ],
    max_workers=1,
))'
```

### BGE reranker

```bash
HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=120 \
HF_HOME="$PWD/.data/model_cache/huggingface" \
HF_HUB_CACHE="$PWD/.data/model_cache/huggingface/hub" \
.venv/bin/python -c '
from huggingface_hub import snapshot_download
print(snapshot_download(
    repo_id="BAAI/bge-reranker-v2-m3",
    local_dir=".data/models/bge-reranker-v2-m3",
    cache_dir=".data/model_cache/huggingface/hub",
    allow_patterns=[
        "config.json", "model.safetensors", "sentencepiece.bpe.model",
        "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
    ],
    max_workers=1,
))'
```

网络中断后原命令可直接重跑并断点续传。验证完全离线加载：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -c '
from packages.rag.embedding import BGEEmbedder
m = BGEEmbedder(".data/models/bge-m3", device="cpu", cache_dir=".data/model_cache")
dense, sparse = m.embed_with_sparse(["ChatBI 完整 BGE 模型加载验证"])
print("dense_dim=", len(dense[0]), "sparse_terms=", len(sparse[0]))'

HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -c '
from packages.rag.rerank import BGEReranker
m = BGEReranker(".data/models/bge-reranker-v2-m3", device="cpu", cache_dir=".data/model_cache")
print(m.rerank("销售额增长", ["销售额同比增长 20%", "天气晴朗"], top_k=2))'
```

第一条应输出 `dense_dim=1024`；第二条应把候选 `0` 排在候选 `1` 前。

## 4. 启动并初始化 Milvus

```bash
cp deploy/milvus/.env.example deploy/milvus/.env
chmod 600 deploy/milvus/.env
```

编辑 `deploy/milvus/.env`，至少替换 `MILVUS_MINIO_ACCESS_KEY` 和
`MILVUS_MINIO_SECRET_KEY`。然后启动：

```bash
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml config --quiet
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml up -d
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml ps
curl --fail http://127.0.0.1:9091/healthz
```

首次启动需要创建业务账号并轮换默认 root 密码。以下输入不会回显；请把新 root 密码保存在
密码管理器中：

```bash
read -rsp 'ChatBI Milvus 业务密码: ' MILVUS_APP_PASSWORD; printf '\n'
read -rsp 'Milvus 新 root 密码: ' MILVUS_NEW_ROOT_PASSWORD; printf '\n'
export MILVUS_APP_PASSWORD MILVUS_NEW_ROOT_PASSWORD
MILVUS_BOOTSTRAP_TOKEN='root:Milvus' \
  uv run --no-sync python scripts/milvus_bootstrap.py
unset MILVUS_BOOTSTRAP_TOKEN MILVUS_APP_PASSWORD MILVUS_NEW_ROOT_PASSWORD
```

成功输出应包含 `status: ready`。业务角色在 `default` 数据库获得 `DatabaseAdmin` 和
`CollectionAdmin`，用于创建代际集合、写入/索引和切换 alias。日常运行使用业务账号，禁止
把 root token 配给 API。

## 5. 配置完整 RAG

将根目录 `.env` 的 RAG 部分设置为：

```dotenv
RAG_EMBEDDER=bge
RAG_RERANKER=bge
RAG_STORE=milvus
RAG_RUNTIME_PROFILE=cpu
EMBEDDING_DEVICE=cpu
RAG_MIN_RELEVANCE=0.02

MILVUS_URI=http://127.0.0.1:19530
MILVUS_TOKEN=chatbi:<第 4 步输入的业务密码>
MILVUS_COLLECTION=kb_chunks

EMBEDDING_MODEL=.data/models/bge-m3
RERANK_MODEL=.data/models/bge-reranker-v2-m3
MODEL_CACHE_DIR=.data/model_cache
```

然后执行 `chmod 600 .env`。如果使用 GPU，把 profile/device 同时改为 `gpu/cuda`；只有一个
字段变化会被 Settings 拒绝启动。

## 6. 启动 API、初始化知识库和 Web

终端 1 保持 Milvus 运行。终端 2 启动 API：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 \
uv run --no-sync uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

CPU 完整模型不要启用 `--reload`，否则重载进程可能重复占用内存。看到
`Application startup complete` 后，在另一个终端执行首次重建：

```bash
curl --fail --request POST http://127.0.0.1:8000/kb/rebuild \
  --header 'Content-Type: application/json' --data '{}'
curl --fail http://127.0.0.1:8000/health/ready
uv run --no-sync python scripts/kb_admin.py status
```

终端 3 启动 Web：

```bash
pnpm --dir apps/web dev --host 127.0.0.1 --port 5173
```

浏览器打开 `http://127.0.0.1:5173`。Vite 会将 `/api` 代理到 8000。

## 7. Semantic 验收

评测会另开一个进程加载模型；16 GiB 环境建议先停止 API，或确认有足够可用内存：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
uv run --no-sync python scripts/kb_eval.py \
  --enforce --profile semantic \
  --json-output .data/kb-eval-semantic.json
```

2026-08-24 CPU 实测：lexical hit@3 `100%`、semantic hit@1 `90%`、semantic hit@3
`100%`、负例拒答率 `100%`、引用来源完整率 `100%`。

## 8. 日常启动与停止

模型和 Milvus 初始化只做一次。以后按顺序执行：Milvus `up -d` → API → Web。查看状态：

```bash
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml ps
curl --fail http://127.0.0.1:8000/health/ready
curl --fail http://127.0.0.1:5173/api/health/ready
```

API/Web 在各自终端按 `Ctrl+C` 停止；Milvus 可停止但保留数据：

```bash
docker compose --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml stop
```

不要删除 `deploy/milvus/volumes`，除非明确要销毁知识库数据。

## 9. 常见问题

### Docker Desktop credential helper 找不到

先启用 Docker Desktop 的 WSL integration。若 Windows CLI 仍引用缺失的 credential helper，
可创建项目级空配置 `.data/docker-cli/config.json`（内容为 `{}`），然后使用：

```bash
"/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe" \
  --config .data/docker-cli compose \
  --env-file deploy/milvus/.env \
  -f deploy/milvus/docker-compose.yml up -d
```

### Hugging Face 读取 10 秒超时或 Xet 卡住

保留 `HF_HUB_DISABLE_XET=1`、`HF_HUB_DOWNLOAD_TIMEOUT=120` 和 `max_workers=1`，重新执行原
下载命令续传。模型完成后启动 API 时设置 `HF_HUB_OFFLINE=1`。

### Milvus 报 CreateCollection permission deny

旧初始化只授予 `CollectionAdmin` 时会发生。用已轮换的 root 密码再次运行当前
`scripts/milvus_bootstrap.py`，它会幂等补齐 `default` 数据库的 `DatabaseAdmin`。不要改用
root token 启动应用。

```bash
read -rsp '当前 Milvus root 密码: ' MILVUS_ROOT_PASSWORD; printf '\n'
read -rsp '原 ChatBI Milvus 业务密码: ' MILVUS_APP_PASSWORD; printf '\n'
export MILVUS_APP_PASSWORD
MILVUS_BOOTSTRAP_TOKEN="root:${MILVUS_ROOT_PASSWORD}" \
  uv run --no-sync python scripts/milvus_bootstrap.py
unset MILVUS_ROOT_PASSWORD MILVUS_BOOTSTRAP_TOKEN MILVUS_APP_PASSWORD
```

### API readiness 为 200，但知识库计数为 0

首次运行尚未重建是正常状态。执行 `POST /kb/rebuild`；发布后立即返回的同步统计可能短暂为
0，再次读取 `/health/ready` 或 `kb_admin.py status` 应显示稳定计数。

### CPU 内存不足或启动两次模型

关闭 `--reload`，不要同时运行 API、`kb_rebuild.py` 和 semantic eval。优先通过 API 的
`POST /kb/rebuild` 复用已加载模型；必要时增加 WSL 内存/交换空间。
