"""中文 Embedding。

默认 `HashingEmbedder`：中文字符 unigram+bigram 哈希成定长向量，确定性、离线、
零重依赖，用于打通链路与测试。真实语义检索用 `BGEEmbedder`（bge-large-zh，需装
`.[rag]`）。后端由 config 选择（rag_embedder），模型名不硬编码在业务里。
禁用英文默认 embedding。
"""

from __future__ import annotations

import abc
import hashlib
import math

from packages.rag.tokenizer import tokenize


class Embedder(abc.ABC):
    """文本向量化抽象。"""

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码为稠密向量（L2 归一）。"""


class HashingEmbedder(Embedder):
    """确定性哈希向量器（中文 char unigram + bigram 特征）。

    非语义模型，但足以打通向量检索链路并做确定性测试；后续可切 BGEEmbedder。
    """

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for feat in self._features(text):
            h = int.from_bytes(hashlib.md5(feat.encode("utf-8")).digest()[:8], "big")
            idx = h % self._dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    @staticmethod
    def _features(text: str) -> list[str]:
        """中文字符 unigram + bigram + 词 token 作为特征。"""
        chars = [c for c in text if not c.isspace()]
        feats: list[str] = list(chars)
        feats += [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
        feats += tokenize(text)
        return feats


class BGEEmbedder(Embedder):
    """基于 FlagEmbedding 的 bge 中文 embedding（需装 .[rag]）。"""

    def __init__(self, model_name: str) -> None:
        # fail-fast：后端尚未实现，构造期即报错（配合启动自检），
        # 避免服务正常启动、首次检索请求才 500。
        raise NotImplementedError(
            "BGE embedding 后端尚未实现：请将配置 rag_embedder 改回 hashing；"
            f"真实接入 {model_name} 需安装 .[rag] 并实现 BGEEmbedder"
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "TODO: 装 .[rag] 后用 FlagEmbedding 加载模型并编码（归一）"
        )
