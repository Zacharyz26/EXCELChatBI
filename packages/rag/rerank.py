"""检索结果重排（bge-reranker-v2-m3）。中文显著提升精度。"""

from __future__ import annotations


class Reranker:
    """对召回结果按 query 相关性重排。"""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name

    def rerank(
        self, query: str, candidates: list[str], top_k: int = 5
    ) -> list[tuple[int, float]]:
        """返回 (候选下标, 分数) 列表，按分数降序取 top_k。"""
        raise NotImplementedError("TODO: 加载 bge-reranker 打分并排序")
