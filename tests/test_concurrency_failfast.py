"""并发健壮性 + 启动自检测试。

覆盖：工具调用离开事件循环执行（A5/V4）、KB 存储并发写安全（store 写锁）、
bge 存根构造期 fail-fast 与服务启动自检（D5）。
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
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
from packages.rag.embedding import BGEEmbedder, HashingEmbedder  # noqa: E402
from packages.rag.lifecycle import SourceDocument, sync_documents  # noqa: E402
from packages.rag.rerank import BGEReranker, LexicalReranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402
from packages.rag.store import LocalKnowledgeStore, SearchHit, StoredChunk  # noqa: E402

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


def test_kb_reads_keep_one_snapshot_during_concurrent_rebuild(tmp_path: Path) -> None:
    """并发重建时，每次双路检索只能看到完整的旧代或新代。"""
    sparse_finished = Event()
    continue_dense = Event()

    class PausingStore(LocalKnowledgeStore):
        def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
            hits = super().bm25_search(query_tokens, top_k)
            sparse_finished.set()
            assert continue_dense.wait(timeout=5)
            return hits

    store = PausingStore(index_dir=str(tmp_path / "kb"))
    embedder = HashingEmbedder(dim=64)
    retriever = HybridRetriever(embedder, store, LexicalReranker())
    old_text = "# 收入口径\n旧版本确认收入按签约日期计算"
    new_text = "# 收入口径\n新版本确认收入按验收日期计算"
    sync_documents([SourceDocument("metric.md", old_text)], embedder, store, full=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(retriever.retrieve, "收入确认日期", 3)
        assert sparse_finished.wait(timeout=5)
        writer = executor.submit(
            sync_documents,
            [SourceDocument("metric.md", new_text)],
            embedder,
            store,
            full=True,
        )
        writer.result(timeout=5)
        continue_dense.set()
        result = reader.result(timeout=5)

    assert result.hits
    assert all("旧版本" in hit.text and "新版本" not in hit.text for hit in result.hits)
    assert "新版本" in retriever.retrieve("收入确认日期", top_k=1).hits[0].text


# ── D5：bge 后端缺依赖时的 fail-fast ──

_HAS_FLAG_EMBEDDING = True
try:
    import FlagEmbedding  # noqa: F401
except ImportError:
    _HAS_FLAG_EMBEDDING = False

_NEEDS_MISSING_RAG_EXTRA = pytest.mark.skipif(
    _HAS_FLAG_EMBEDDING,
    reason="已安装 FlagEmbedding：构造会真实加载权重，缺依赖契约不适用",
)


@_NEEDS_MISSING_RAG_EXTRA
def test_bge_backends_fail_fast_without_rag_extra() -> None:
    """未装 .[rag] 时 bge 后端构造期即报错，并指引安装或改回替身配置。"""
    with pytest.raises(RuntimeError, match="hashing"):
        BGEEmbedder("bge-m3")
    with pytest.raises(RuntimeError, match="lexical"):
        BGEReranker("bge-reranker-v2-m3")


@_NEEDS_MISSING_RAG_EXTRA
def test_startup_failfast_when_bge_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置 rag_embedder=bge 但缺依赖时，服务启动即失败，而非请求中途 500。"""
    monkeypatch.setenv("RAG_EMBEDDER", "bge")
    monkeypatch.setenv("RAG_STORE", "milvus")
    get_settings.cache_clear()
    embedder_dep.cache_clear()
    reranker_dep.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="FlagEmbedding"):
            with TestClient(app):   # context manager 触发 lifespan 启动自检
                pass
    finally:
        # 恢复环境（monkeypatch 撤销 env）后清缓存，避免污染其他测试
        get_settings.cache_clear()
        embedder_dep.cache_clear()
        reranker_dep.cache_clear()
