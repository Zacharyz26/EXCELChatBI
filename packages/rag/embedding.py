"""中文 Embedding。

默认 `HashingEmbedder`：中文字符 unigram+bigram 哈希成定长向量，确定性、离线、
零重依赖，用于打通链路与测试。真实语义检索用 `BGEEmbedder`（bge-m3，决策1：
稠密+稀疏双路，稀疏 lexical weights 取代自实现 BM25；需装 `.[rag]`）。
后端由 config 选择（rag_embedder），模型名不硬编码在业务里；推理 device 为
配置项 auto/cpu/cuda（决策4），切换不改代码。禁用英文默认 embedding。
"""

from __future__ import annotations

import abc
import hashlib
import math

from packages.rag.tokenizer import tokenize

# bge-m3 稀疏表示：token_id(str) → 权重
SparseVector = dict[str, float]


def resolve_device(configured: str) -> str:
    """解析推理设备配置（决策4）：auto 按 torch 探测，显式 cpu/cuda 原样生效。"""
    if configured in ("cpu", "cuda"):
        return configured
    try:
        import torch
    except ImportError:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


class Embedder(abc.ABC):
    """文本向量化抽象。"""

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码为稠密向量（L2 归一）。"""

    def embed_with_sparse(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[SparseVector] | None]:
        """稠密 + 稀疏（lexical weights）一次编码。

        不支持稀疏路的后端返回 (dense, None)，检索层回退到中文 BM25 备路；
        bge-m3 覆写本方法单次前向同时产出两路，避免摄入/查询双份编码开销。
        """
        return self.embed(texts), None


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
    """bge-m3 稠密 + 稀疏双路 embedding（FlagEmbedding，需装 .[rag]）。

    - model_name 可为 HuggingFace 模型名或**本地权重目录**（离线侧载配
      HF_HUB_OFFLINE=1，见 README 环境准备）。
    - device 走配置 auto/cpu/cuda（决策4）；cuda 下启用 fp16。
    - 构造期加载模型（fail-fast）：依赖/权重缺失在启动时报错，而非首次检索 500。
    """

    def __init__(self, model_name: str, device: str = "auto") -> None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "缺少 FlagEmbedding：请先 `uv sync --extra rag`，"
                "或将配置 rag_embedder 改回 hashing"
            ) from exc
        resolved = resolve_device(device)
        self._model = BGEM3FlagModel(
            model_name,
            device=resolved,
            use_fp16=(resolved == "cuda"),
            normalize_embeddings=True,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = self._model.encode(texts, return_dense=True, return_sparse=False)
        dense = out["dense_vecs"]
        return [[float(v) for v in vec] for vec in dense]

    def embed_with_sparse(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[SparseVector] | None]:
        """单次前向同时产出稠密向量与 lexical weights 稀疏表示。"""
        out = self._model.encode(texts, return_dense=True, return_sparse=True)
        dense = [[float(v) for v in vec] for vec in out["dense_vecs"]]
        sparse: list[SparseVector] = [
            {str(token_id): float(weight) for token_id, weight in weights.items()}
            for weights in out["lexical_weights"]
        ]
        return dense, sparse
