"""知识库问答测试：诚实无答（不调模型）+ 带引用（来源正确）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.kb_qa import answer_question  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402
from packages.rag.embedding import HashingEmbedder  # noqa: E402
from packages.rag.pipeline import chunk_and_embed  # noqa: E402
from packages.rag.rerank import LexicalReranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402
from packages.rag.store import LocalKnowledgeStore  # noqa: E402


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
