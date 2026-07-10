"""知识库问答测试：诚实无答（不调模型）+ 带引用（来源正确）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.kb_qa import _dedup_hits, answer_question  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402
from packages.rag.embedding import HashingEmbedder  # noqa: E402
from packages.rag.pipeline import chunk_and_embed  # noqa: E402
from packages.rag.rerank import LexicalReranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402
from packages.rag.store import LocalKnowledgeStore, SearchHit  # noqa: E402


class RecordingGateway:
    """记录是否被调用的假网关。"""

    def __init__(self, content: str = "答案[1]") -> None:
        self.called = False
        self._content = content

    async def complete(self, scenario: Scenario, messages: list[Message]) -> ModelResponse:
        self.called = True
        return ModelResponse(content=self._content, model="fake")


@pytest.fixture
def retriever(tmp_path: Path) -> HybridRetriever:
    store = LocalKnowledgeStore(index_dir=str(tmp_path / "kb"))
    emb = HashingEmbedder(256)
    store.add(chunk_and_embed("# 活跃用户\n活跃用户指去重登录用户数。", "指标.md", emb))
    return HybridRetriever(emb, store, LexicalReranker())


@pytest.mark.asyncio
async def test_no_result_does_not_call_model(retriever: HybridRetriever) -> None:
    gw = RecordingGateway()
    res = await answer_question("量子纠缠与航天器轨道", retriever, gw)
    assert res["is_empty"] is True
    assert res["citations"] == []
    assert "未找到" in res["answer"]
    assert gw.called is False  # 红线6：无结果不调用模型、不编造


@pytest.mark.asyncio
async def test_answer_has_traceable_citation(retriever: HybridRetriever) -> None:
    gw = RecordingGateway(content="活跃用户指去重登录用户数[1]。")
    res = await answer_question("活跃用户怎么定义", retriever, gw)
    assert res["is_empty"] is False
    assert gw.called is True
    assert res["citations"], "非空结果必须带引用（红线6）"
    assert res["citations"][0]["source"] == "指标.md"
    assert "活跃用户" in res["citations"][0]["snippet"]


# ── 引用去重（问题1）──

def test_dedup_hits_collapses_identical() -> None:
    hits = [
        SearchHit("1", "活跃用户指去重登录用户数。", "指标.md", 0.9, "活跃用户"),
        SearchHit("2", "活跃用户指去重登录用户数。", "指标.md", 0.8, "活跃用户"),  # 副本
        SearchHit("3", "复购率指周期内重复购买占比。", "留存.md", 0.7, "复购率"),
    ]
    out = _dedup_hits(hits)
    assert len(out) == 2                       # 同一片段只留一份
    assert out[0].chunk_id == "1"              # 保留排名最高的一份


def test_store_add_is_idempotent(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(index_dir=str(tmp_path / "kb"))
    emb = HashingEmbedder(256)
    doc = "# 活跃用户\n活跃用户指去重登录用户数。\n# 复购率\n复购率指重复购买占比。"
    first = store.add(chunk_and_embed(doc, "指标.md", emb))
    again = store.add(chunk_and_embed(doc, "指标.md", emb))   # 重复摄入
    assert first > 0 and again == 0            # 第二次全部去重、不累积
    assert store.count() == first


def test_store_topics_and_sources_distinct(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(index_dir=str(tmp_path / "kb"))
    emb = HashingEmbedder(256)
    store.add(chunk_and_embed("# 活跃用户\n定义A。\n# 复购率\n定义B。", "指标.md", emb))
    assert store.sources() == ["指标.md"]
    assert store.topics() == ["活跃用户", "复购率"]
