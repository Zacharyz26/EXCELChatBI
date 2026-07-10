"""检索结果重排。

默认 `LexicalReranker`：基于中文分词的 query/候选词项重叠打分，确定性、离线。
真实 `BGEReranker`（bge-reranker-v2-m3，需装 `.[rag]`）后续切换。中文显著提升精度。
"""

from __future__ import annotations

import abc

from packages.rag.tokenizer import tokenize


class Reranker(abc.ABC):
    """对召回结果按 query 相关性重排。"""

    @abc.abstractmethod
    def rerank(
        self, query: str, candidates: list[str], top_k: int = 5
    ) -> list[tuple[int, float]]:
        """返回 (候选下标, 分数) 列表，按分数降序取 top_k。"""


class LexicalReranker(Reranker):
    """中文词项重叠重排（确定性，无需模型）。"""

    def rerank(
        self, query: str, candidates: list[str], top_k: int = 5
    ) -> list[tuple[int, float]]:
        q_terms = set(tokenize(query))
        scored: list[tuple[int, float]] = []
        for i, text in enumerate(candidates):
            c_terms = set(tokenize(text))
            if not q_terms:
                score = 0.0
            else:
                overlap = len(q_terms & c_terms)
                score = overlap / len(q_terms)
            scored.append((i, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


class BGEReranker(Reranker):
    """基于 FlagEmbedding 的 bge-reranker（需装 .[rag]）。"""

    def __init__(self, model_name: str) -> None:
        # fail-fast：后端尚未实现，构造期即报错（配合启动自检），
        # 避免服务正常启动、首次检索请求才 500。
        raise NotImplementedError(
            "BGE rerank 后端尚未实现：请将配置 rag_reranker 改回 lexical；"
            f"真实接入 {model_name} 需安装 .[rag] 并实现 BGEReranker"
        )

    def rerank(
        self, query: str, candidates: list[str], top_k: int = 5
    ) -> list[tuple[int, float]]:
        raise NotImplementedError(
            "TODO: 装 .[rag] 后用 bge-reranker 对 (query, candidate) 打分排序"
        )
