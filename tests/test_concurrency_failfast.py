"""并发健壮性 + 启动自检测试。

覆盖：工具调用离开事件循环执行（A5/V4）、KB 存储并发写安全（store 写锁）、
bge 存根构造期 fail-fast 与服务启动自检（D5）。
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import embedder_dep, reranker_dep, stats_tools_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.common.base_server import MCPServer  # noqa: E402
from mcp_servers.common.tool import Tool  # noqa: E402
from packages.common.config import get_settings  # noqa: E402
from packages.rag.embedding import BGEEmbedder  # noqa: E402
from packages.rag.rerank import BGEReranker  # noqa: E402
from packages.rag.store import LocalKnowledgeStore, StoredChunk  # noqa: E402

# ── A5/V4：工具在线程池执行，不阻塞事件循环 ──

def test_stats_tool_runs_off_event_loop() -> None:
    """路由里的 Tool.invoke 应在线程池线程执行（该线程无运行中的事件循环）。"""
    seen: dict[str, bool] = {}

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
            seen["in_loop"] = True      # 若可取到循环，说明仍在事件循环线程里阻塞执行
        except RuntimeError:
            seen["in_loop"] = False
        return {"ok": True}

    fake = MCPServer(name="stats", port=0)
    fake.register(Tool("trend_analysis", "假趋势工具", {"type": "object"}, handler))
    app.dependency_overrides[stats_tools_dep] = lambda: fake
    try:
        client = TestClient(app)
        resp = client.post("/analyze/stats", json={"dataset_ref": "x", "kind": "trend"})
        assert resp.status_code == 200, resp.text
        assert seen["in_loop"] is False
    finally:
        app.dependency_overrides.clear()


# ── store 写锁：并发摄入不丢数据、不写坏索引 ──

def test_kb_store_concurrent_add_is_safe(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(index_dir=str(tmp_path / "kb"))

    def _add(i: int) -> int:
        return store.add(
            [StoredChunk(text=f"片段{i}", source=f"来源{i}.md", tokens=[f"t{i}"], vector=[1.0])]
        )

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_add, range(64)))

    assert sum(results) == 64
    assert store.count() == 64
    # 重新加载落盘索引：与内存一致，未被并发写互相覆盖
    assert LocalKnowledgeStore(index_dir=str(tmp_path / "kb")).count() == 64


# ── D5：bge 存根 fail-fast ──

def test_bge_stubs_fail_at_construction() -> None:
    """未实现的 bge 后端在构造期即报错，并指引改回可用配置。"""
    with pytest.raises(NotImplementedError, match="hashing"):
        BGEEmbedder("bge-large-zh-v1.5")
    with pytest.raises(NotImplementedError, match="lexical"):
        BGEReranker("bge-reranker-v2-m3")


def test_startup_failfast_when_bge_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置 rag_embedder=bge（未实现）时，服务启动即失败，而非请求中途 500。"""
    monkeypatch.setenv("RAG_EMBEDDER", "bge")
    get_settings.cache_clear()
    embedder_dep.cache_clear()
    reranker_dep.cache_clear()
    try:
        with pytest.raises(NotImplementedError, match="rag_embedder"):
            with TestClient(app):   # context manager 触发 lifespan 启动自检
                pass
    finally:
        # 恢复环境（monkeypatch 撤销 env）后清缓存，避免污染其他测试
        get_settings.cache_clear()
        embedder_dep.cache_clear()
        reranker_dep.cache_clear()
