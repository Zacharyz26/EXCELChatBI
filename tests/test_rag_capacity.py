"""Bounded RAG query concurrency and load-probe input contracts."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import anyio
import pytest
from packages.rag.embedding import HashingEmbedder
from packages.rag.rerank import LexicalReranker
from packages.rag.retriever import HybridRetriever, RetrievalCapacityError
from packages.rag.store import LocalKnowledgeStore, SearchHit
from scripts.kb_load_test import validate_load_shape
from scripts.rag_resource_probe import validate_probe_shape


class _BlockingStore(LocalKnowledgeStore):
    def __init__(self, started: Event, release: Event, tmp_path: str) -> None:
        super().__init__(tmp_path)
        self._started = started
        self._release = release

    def count(self) -> int:
        return 1

    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        del query_tokens, top_k
        return []

    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        del query_vec, top_k
        self._started.set()
        assert self._release.wait(timeout=2)
        return []


def test_retriever_rejects_excess_concurrency_without_unbounded_queue(
    tmp_path,
) -> None:
    started = Event()
    release = Event()
    retriever = HybridRetriever(
        HashingEmbedder(dim=8),
        _BlockingStore(started, release, str(tmp_path / "kb")),
        LexicalReranker(),
        max_concurrent_queries=1,
        queue_timeout_seconds=0.01,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(retriever.retrieve, "第一个查询")
        assert started.wait(timeout=1)
        with pytest.raises(RetrievalCapacityError, match="并发"):
            retriever.retrieve("第二个查询")
        release.set()
        first.result(timeout=1)


def test_retriever_rejects_when_bounded_wait_queue_is_full(tmp_path) -> None:
    started = Event()
    release = Event()
    retriever = HybridRetriever(
        HashingEmbedder(dim=8),
        _BlockingStore(started, release, str(tmp_path / "kb")),
        LexicalReranker(),
        max_concurrent_queries=1,
        max_queued_queries=1,
        queue_timeout_seconds=1,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(retriever.retrieve, "执行中的查询")
        assert started.wait(timeout=1)
        waiting = executor.submit(retriever.retrieve, "唯一等待查询")
        time.sleep(0.05)
        with pytest.raises(RetrievalCapacityError, match="队列已满"):
            retriever.retrieve("不得进入队列")
        release.set()
        first.result(timeout=1)
        waiting.result(timeout=1)


async def test_abandoned_sync_worker_keeps_slot_until_underlying_call_returns(
    tmp_path,
) -> None:
    started = Event()
    release = Event()
    retriever = HybridRetriever(
        HashingEmbedder(dim=8),
        _BlockingStore(started, release, str(tmp_path / "kb")),
        LexicalReranker(),
        max_concurrent_queries=1,
        max_queued_queries=0,
        queue_timeout_seconds=0.05,
    )
    scopes: list[anyio.CancelScope] = []

    async def abandoned_call() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await anyio.to_thread.run_sync(
                retriever.retrieve,
                "被取消的查询",
                abandon_on_cancel=True,
            )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(abandoned_call)
        assert await anyio.to_thread.run_sync(started.wait, 1)
        scopes[0].cancel()
        await anyio.sleep(0)
        with pytest.raises(RetrievalCapacityError, match="队列已满"):
            retriever.retrieve("取消后立即重试")
        release.set()

    for _ in range(20):
        try:
            result = await anyio.to_thread.run_sync(retriever.retrieve, "底层返回后重试")
        except RetrievalCapacityError:
            await anyio.sleep(0.01)
        else:
            assert result.is_empty is True
            break
    else:
        pytest.fail("底层同步调用结束后没有释放检索容量")


def test_retriever_rejects_negative_queue_bound(tmp_path) -> None:
    with pytest.raises(ValueError, match="max_queued_queries"):
        HybridRetriever(
            HashingEmbedder(dim=8),
            LocalKnowledgeStore(str(tmp_path / "kb")),
            LexicalReranker(),
            max_queued_queries=-1,
        )


@pytest.mark.parametrize(
    ("query", "top_k", "message"),
    [
        ("", 1, "不能为空"),
        ("超" * 9, 1, "长度"),
        ("正常", 0, "top_k"),
        ("正常", 11, "top_k"),
    ],
)
def test_retriever_bounds_query_shape(
    tmp_path,
    query: str,
    top_k: int,
    message: str,
) -> None:
    retriever = HybridRetriever(
        HashingEmbedder(dim=8),
        LocalKnowledgeStore(str(tmp_path / "kb")),
        LexicalReranker(),
        max_query_chars=8,
    )

    with pytest.raises(ValueError, match=message):
        retriever.retrieve(query, top_k=top_k)


@pytest.mark.parametrize(
    ("requests", "concurrency", "warmup"),
    [
        (0, 1, 0),
        (1001, 1, 0),
        (1, 0, 0),
        (1, 33, 0),
        (1, 1, -1),
        (1, 1, 11),
    ],
)
def test_load_probe_rejects_unbounded_shapes(
    requests: int,
    concurrency: int,
    warmup: int,
) -> None:
    with pytest.raises(ValueError):
        validate_load_shape(requests, concurrency, warmup)


@pytest.mark.parametrize(
    ("chunks", "requests", "concurrency", "dimension"),
    [
        (0, 1, 1, 8),
        (5_001, 1, 1, 8),
        (1, 0, 1, 8),
        (1, 1_001, 1, 8),
        (1, 1, 0, 8),
        (1, 1, 9, 8),
        (1, 1, 1, 7),
        (1, 1, 1, 1_025),
    ],
)
def test_resource_probe_rejects_unbounded_shapes(
    chunks: int, requests: int, concurrency: int, dimension: int
) -> None:
    with pytest.raises(ValueError):
        validate_probe_shape(chunks, requests, concurrency, dimension)
