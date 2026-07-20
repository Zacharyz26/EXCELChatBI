"""知识库检索评测（验收基线执行器，见 docs/知识库升级验收基线.md）。

对 docs/kb_samples 语料跑 scripts/kb_eval_set.jsonl 评测集，输出：
hit@1 / hit@3 / MRR（按 lexical / semantic 分组）、负例拒答率、单查询延迟、
重排分数分布（供 RAG_MIN_RELEVANCE 阈值标定）。

用法（后端由 .env / 环境变量选择，与线上同一构造路径）：
    uv run python scripts/kb_eval.py                  # 当前配置的后端
    RAG_EMBEDDER=bge RAG_RERANKER=bge RAG_STORE=milvus \
        uv run python scripts/kb_eval.py              # bge + Milvus Lite
    uv run python scripts/kb_eval.py --enforce        # 按验收阈值判定退出码（CI 用）

评测索引写入临时目录，不污染 .data 下的真实索引。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.common.config import get_settings  # noqa: E402
from packages.rag.pipeline import chunk_and_embed  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402

EVAL_SET = Path(__file__).parent / "kb_eval_set.jsonl"

# 验收阈值（bge-m3 + bge-reranker 达标线，见基线文档；替身跑分仅记录不判定）
THRESHOLDS = {
    "lexical_hit3": 1.0,
    "semantic_hit1": 0.9,
    "semantic_hit3": 1.0,
    "negative_reject": 1.0,
}


def _build_components(index_dir: str) -> tuple[HybridRetriever, str]:
    """按当前配置构造检索组件（与 apps/api/deps.py 同一选择逻辑），索引落临时目录。"""
    from packages.rag.embedding import Embedder
    from packages.rag.rerank import Reranker
    from packages.rag.store import KnowledgeStore

    s = get_settings()
    embedder: Embedder
    reranker: Reranker
    store: KnowledgeStore
    if s.rag_embedder == "bge":
        from packages.rag.embedding import BGEEmbedder
        embedder = BGEEmbedder(s.embedding_model, device=s.embedding_device)
    else:
        from packages.rag.embedding import HashingEmbedder
        embedder = HashingEmbedder(dim=s.embedding_dim)
    if s.rag_reranker == "bge":
        from packages.rag.rerank import BGEReranker
        reranker = BGEReranker(s.rerank_model, device=s.embedding_device)
    else:
        from packages.rag.rerank import LexicalReranker
        reranker = LexicalReranker()
    if s.rag_store == "milvus":
        from packages.rag.milvus_store import MilvusKnowledgeStore
        store = MilvusKnowledgeStore(str(Path(index_dir) / "milvus_eval.db"))
    else:
        from packages.rag.store import LocalKnowledgeStore
        store = LocalKnowledgeStore(index_dir)
    backend = f"embedder={s.rag_embedder} reranker={s.rag_reranker} store={s.rag_store}"
    return (
        HybridRetriever(embedder, store, reranker, min_relevance=s.rag_min_relevance),
        backend,
    )


def _ingest(retriever: HybridRetriever) -> tuple[int, float]:
    """摄入 docs/kb_samples + 评测干扰语料，返回 (片段数, 耗时秒)。

    干扰语料（scripts/kb_eval_distractors.md）扩大候选池，避免语料过小时
    top-k 命中率虚高，保证评测区分度。
    """
    docs_dir = ROOT / "docs" / "kb_samples"
    paths = sorted(docs_dir.glob("*.md")) + [Path(__file__).parent / "kb_eval_distractors.md"]
    started = time.perf_counter()
    total = 0
    for path in paths:
        chunks = chunk_and_embed(
            path.read_text(encoding="utf-8"), path.name, retriever.embedder
        )
        total += retriever.store.add(chunks)
    return total, time.perf_counter() - started


def _rank_of(hits: list[Any], expected_sections: list[str]) -> int | None:
    for rank, hit in enumerate(hits, start=1):
        if hit.section in expected_sections:
            return rank
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库检索评测")
    parser.add_argument("--enforce", action="store_true", help="按验收阈值判定退出码")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    lines = EVAL_SET.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]

    with tempfile.TemporaryDirectory() as tmp:
        retriever, backend = _build_components(tmp)
        n_chunks, ingest_seconds = _ingest(retriever)
        print(f"后端：{backend}")
        print(f"摄入：{n_chunks} 个片段，{ingest_seconds:.2f}s\n")

        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        positive_top_scores: list[float] = []
        negative_top_scores: list[float] = []
        for case in cases:
            started = time.perf_counter()
            result = retriever.retrieve(case["query"], top_k=args.top_k)
            latency = time.perf_counter() - started
            latencies.append(latency)
            rank = _rank_of(result.hits, case["expected_sections"])
            top_score = result.hits[0].score if result.hits else None
            if case["type"] == "negative":
                if top_score is not None:
                    negative_top_scores.append(top_score)
                ok = result.is_empty
            else:
                if top_score is not None:
                    positive_top_scores.append(top_score)
                ok = rank is not None and rank <= 3
            rows.append({**case, "rank": rank, "ok": ok, "latency": latency,
                         "empty": result.is_empty, "top_score": top_score})

        metrics: dict[str, float] = {}
        for group in ("lexical", "semantic"):
            sub = [r for r in rows if r["type"] == group]
            metrics[f"{group}_hit1"] = sum(1 for r in sub if r["rank"] == 1) / len(sub)
            hit3 = sum(1 for r in sub if r["rank"] and r["rank"] <= 3)
            metrics[f"{group}_hit3"] = hit3 / len(sub)
            metrics[f"{group}_mrr"] = sum(1 / r["rank"] for r in sub if r["rank"]) / len(sub)
        negatives = [r for r in rows if r["type"] == "negative"]
        metrics["negative_reject"] = sum(1 for r in negatives if r["empty"]) / len(negatives)

        print("| 分组 | hit@1 | hit@3 | MRR |")
        print("|------|-------|-------|-----|")
        for group in ("lexical", "semantic"):
            print(f"| {group} | {metrics[f'{group}_hit1']:.0%} "
                  f"| {metrics[f'{group}_hit3']:.0%} | {metrics[f'{group}_mrr']:.2f} |")
        print(f"\n负例拒答率：{metrics['negative_reject']:.0%}"
              f"（{sum(1 for r in negatives if r['empty'])}/{len(negatives)}）")
        print(f"单查询延迟：avg {statistics.mean(latencies) * 1000:.0f}ms · "
              f"max {max(latencies) * 1000:.0f}ms")

        # 阈值标定素材：正例应答分 vs 负例应答分的分布（RAG_MIN_RELEVANCE 取分离点）
        if positive_top_scores:
            print(f"正例 top1 重排分：min {min(positive_top_scores):.4f} · "
                  f"median {statistics.median(positive_top_scores):.4f}")
        if negative_top_scores:
            print(f"负例 top1 重排分（漏拒答的）：max {max(negative_top_scores):.4f}")
        else:
            print("负例 top1 重排分：全部已被阈值拒答")

        failed = [r for r in rows if not r["ok"]]
        if failed:
            print("\n未达标用例：")
            for r in failed:
                print(f"  [{r['type']}] {r['query']} → rank={r['rank']} empty={r['empty']}")

        if args.enforce:
            misses = {k: v for k, v in THRESHOLDS.items() if metrics.get(k, 0.0) < v}
            if misses:
                detail = {k: f"{metrics[k]:.0%} < {v:.0%}" for k, v in misses.items()}
                print(f"\n验收未通过：{detail}")
                return 1
            print("\n验收通过：全部指标达标")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
