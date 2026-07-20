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
    """bge-reranker-v2-m3 交叉编码重排（FlagEmbedding，需装 .[rag]）。

    - model_name 可为模型名或本地权重目录（离线侧载）。
    - device 走配置 auto/cpu/cuda（决策4）；cuda 下启用 fp16。
    - 分数经 sigmoid 归一到 (0,1)，便于配置统一的相关性阈值
      （RAG_MIN_RELEVANCE，按真实分数分布标定，见验收基线文档）。
    - 构造期加载模型（fail-fast）：依赖/权重缺失在启动时报错，而非首次检索 500。
    """

    def __init__(self, model_name: str, device: str = "auto") -> None:
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:
            raise RuntimeError(
                "缺少 FlagEmbedding：请先 `uv sync --extra rag`，"
                "或将配置 rag_reranker 改回 lexical"
            ) from exc
        from packages.rag.embedding import resolve_device

        resolved = resolve_device(device)
        try:
            self._model = FlagReranker(
                model_name, use_fp16=(resolved == "cuda"), device=resolved
            )
        except TypeError:
            # 旧版 FlagEmbedding 无 device 入参：退化为库内默认设备选择
            self._model = FlagReranker(model_name, use_fp16=(resolved == "cuda"))

    def rerank(
        self, query: str, candidates: list[str], top_k: int = 5
    ) -> list[tuple[int, float]]:
        if not candidates:
            return []
        scores = self._model.compute_score(
            [[query, text] for text in candidates], normalize=True
        )
        if isinstance(scores, int | float):  # 单候选时库返回标量
            scores = [scores]
        ranked = sorted(
            ((i, float(s)) for i, s in enumerate(scores)),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:top_k]
