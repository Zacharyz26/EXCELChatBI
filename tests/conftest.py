"""pytest 配置：确保项目根在 sys.path（顶层包 apps/mcp_servers/packages 可导入）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 在任何测试模块导入 API/工具之前建立全进程隔离。TemporaryDirectory
# 在解释器退出时回收；测试从不得读取根目录 .env 或写入仓库 .data。
_TEST_RUNTIME = tempfile.TemporaryDirectory(prefix="excel-chatbi-pytest-")
TEST_RUNTIME_ROOT = Path(_TEST_RUNTIME.name).resolve()
os.environ.update(
    {
        "CHATBI_TEST_ISOLATION": "1",
        "APP_ENV": "testing",
        "AUTH_MODE": "disabled",
        "MODEL_REGISTRY_PATH": str(ROOT / "config" / "models.example.yaml"),
        "CHAT_DB_PATH": str(TEST_RUNTIME_ROOT / "chatbi.db"),
        "WORKSPACE_BACKUP_DIR": str(TEST_RUNTIME_ROOT / "workspace_backups"),
        "DATASET_DIR": str(TEST_RUNTIME_ROOT / "datasets"),
        "UPLOAD_DIR": str(TEST_RUNTIME_ROOT / "uploads"),
        "REPORT_DIR": str(TEST_RUNTIME_ROOT / "reports"),
        "KB_INDEX_DIR": str(TEST_RUNTIME_ROOT / "kb_index"),
        "KB_SOURCE_DIR": str(TEST_RUNTIME_ROOT / "kb_sources"),
        "KB_BACKUP_DIR": str(TEST_RUNTIME_ROOT / "kb_backups"),
        "MODEL_CACHE_DIR": str(TEST_RUNTIME_ROOT / "model_cache"),
        # pymilvus reads this generic variable at import time and only accepts an
        # HTTP URI there. Tests that exercise Lite pass their tmp_path explicitly.
        "MILVUS_URI": "http://127.0.0.1:19530",
        "RAG_EMBEDDER": "hashing",
        "RAG_RERANKER": "lexical",
        "RAG_STORE": "local",
        "RAG_RUNTIME_PROFILE": "baseline",
        "EMBEDDING_DEVICE": "cpu",
        "AGENT_MCP_TRANSPORT": "in_process",
    }
)
