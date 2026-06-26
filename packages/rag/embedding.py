"""中文 Embedding（bge-large-zh-v1.5 / bge-m3）。禁用英文默认 embedding。"""

from __future__ import annotations

import abc


class Embedder(abc.ABC):
    """文本向量化抽象。"""

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码为稠密向量。"""


class BGEEmbedder(Embedder):
    """基于 FlagEmbedding 的 bge 中文 embedding。"""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("TODO: 加载 bge 模型并编码")
