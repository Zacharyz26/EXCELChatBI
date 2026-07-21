"""知识库检索质量评测：人类报告 + CI 可读 JSON 门禁。

默认按当前后端自动选择 baseline（hashing/local）或 semantic（bge/milvus）阈值：

    uv run python scripts/kb_eval.py --enforce
    uv run python scripts/kb_eval.py --enforce --json-output .data/kb-eval.json
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
from packages.rag.lifecycle import SourceDocument, sync_documents  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402

EVAL_SET = Path(__file__).parent / "kb_eval_set.jsonl"

THRESHOLD_PROFILES: dict[str, dict[str, float]] = {
    "baseline": {
        "lexical_hit3": 1.0,
        "semantic_hit1": 0.7,
        "semantic_hit3": 0.9,
        "negative_reject": 1.0,
        "citation_source_rate": 1.0,
    },
    "semantic": {
        "lexical_hit3": 1.0,
        "semantic_hit1": 0.9,
        "semantic_hit3": 1.0,
        "negative_reject": 1.0,
        "citation_source_rate": 1.0,
    },
}


def _build_components(index_dir: str) -> tuple[HybridRetriever, str]:
    """按线上配置构造组件，但把评测索引隔离到临时目录。"""
    from packages.rag.embedding import BGEEmbedder, Embedder, HashingEmbedder
    from packages.rag.rerank import BGEReranker, LexicalReranker, Reranker
    from packages.rag.store import KnowledgeStore, LocalKnowledgeStore

    settings = get_settings()
    embedder: Embedder = (
        BGEEmbedder(settings.embedding_model, device=settings.embedding_device)
        if settings.rag_embedder == "bge"
        else HashingEmbedder(dim=settings.embedding_dim)
    )
    reranker: Reranker = (
        BGEReranker(settings.rerank_model, device=settings.embedding_device)
        if settings.rag_reranker == "bge"
        else LexicalReranker()
    )
    store: KnowledgeStore
    if settings.rag_store == "milvus":
        from packages.rag.milvus_store import MilvusKnowledgeStore

        store = MilvusKnowledgeStore(
            str(Path(index_dir) / "milvus_eval.db"),
            collection=settings.milvus_collection,
        )
    else:
        store = LocalKnowledgeStore(index_dir)
    evaluation_store = (
        "milvus_lite_isolated" if settings.rag_store == "milvus" else "local_isolated"
    )
    backend = (
        f"embedder={settings.rag_embedder} "
        f"reranker={settings.rag_reranker} store={evaluation_store}"
    )
    return (
        HybridRetriever(
            embedder,
            store,
            reranker,
            min_relevance=settings.rag_min_relevance,
        ),
        backend,
    )


def _ingest(retriever: HybridRetriever) -> tuple[int, float]:
    docs_dir = ROOT / "docs" / "kb_samples"
    paths = sorted(docs_dir.glob("*.md")) + [Path(__file__).parent / "kb_eval_distractors.md"]
    documents = [
        SourceDocument(path.name, path.read_text(encoding="utf-8")) for path in paths
    ]
    started = time.perf_counter()
    result = sync_documents(documents, retriever.embedder, retriever.store, full=True)
    return result.total_chunks, time.perf_counter() - started


def _rank_of(hits: list[Any], expected_sections: list[str]) -> int | None:
    for rank, hit in enumerate(hits, start=1):
        if hit.section in expected_sections:
            return rank
    return None


def _evaluate(
    retriever: HybridRetriever, cases: list[dict[str, Any]], top_k: int
) -> tuple[dict[str, float], list[dict[str, Any]], dict[str, float | None]]:
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    positive_top_scores: list[float] = []
    negative_top_scores: list[float] = []
    for index, case in enumerate(cases, start=1):
        started = time.perf_counter()
        result = retriever.retrieve(str(case["query"]), top_k=top_k)
        latency = time.perf_counter() - started
        latencies.append(latency)
        rank = _rank_of(result.hits, list(case["expected_sections"]))
        top_score = result.hits[0].score if result.hits else None
        if case["type"] == "negative":
            if top_score is not None:
                negative_top_scores.append(top_score)
            ok = result.is_empty
        else:
            if top_score is not None:
                positive_top_scores.append(top_score)
            ok = rank is not None and rank <= 3
        rows.append(
            {
                "case": index,
                "type": case["type"],
                "rank": rank,
                "ok": ok,
                "is_empty": result.is_empty,
                "top_score": top_score,
                "sources": [hit.source for hit in result.hits],
                "latency_ms": round(latency * 1_000, 3),
                "rejection_reason": result.diagnostics.rejection_reason,
            }
        )

    metrics: dict[str, float] = {}
    for group in ("lexical", "semantic"):
        subset = [row for row in rows if row["type"] == group]
        metrics[f"{group}_hit1"] = sum(row["rank"] == 1 for row in subset) / len(subset)
        metrics[f"{group}_hit3"] = sum(
            row["rank"] is not None and row["rank"] <= 3 for row in subset
        ) / len(subset)
        metrics[f"{group}_mrr"] = sum(
            1 / row["rank"] for row in subset if row["rank"] is not None
        ) / len(subset)
    negatives = [row for row in rows if row["type"] == "negative"]
    positives = [row for row in rows if row["type"] != "negative"]
    metrics["negative_reject"] = sum(row["is_empty"] for row in negatives) / len(negatives)
    metrics["citation_source_rate"] = sum(
        bool(row["sources"]) and all(row["sources"]) for row in positives
    ) / len(positives)
    distribution: dict[str, float | None] = {
        "latency_avg_ms": round(statistics.mean(latencies) * 1_000, 3),
        "latency_max_ms": round(max(latencies) * 1_000, 3),
        "positive_top_min": min(positive_top_scores) if positive_top_scores else None,
        "positive_top_median": (
            statistics.median(positive_top_scores) if positive_top_scores else None
        ),
        "negative_top_max": max(negative_top_scores) if negative_top_scores else None,
    }
    return metrics, rows, distribution


def _print_human(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    distribution = report["distribution"]
    print(f"后端：{report['backend']}")
    print(f"摄入：{report['chunks']} 个片段，{report['ingest_seconds']:.2f}s\n")
    print("| 分组 | hit@1 | hit@3 | MRR |")
    print("|------|-------|-------|-----|")
    for group in ("lexical", "semantic"):
        print(
            f"| {group} | {metrics[f'{group}_hit1']:.0%} "
            f"| {metrics[f'{group}_hit3']:.0%} | {metrics[f'{group}_mrr']:.2f} |"
        )
    print(f"\n负例拒答率：{metrics['negative_reject']:.0%}")
    print(f"引用来源完整率：{metrics['citation_source_rate']:.0%}")
    print(
        f"单查询延迟：avg {distribution['latency_avg_ms']:.0f}ms · "
        f"max {distribution['latency_max_ms']:.0f}ms"
    )
    failed = [row for row in report["cases"] if not row["ok"]]
    if failed:
        print("\n未命中用例（是否阻断由所选 profile 阈值决定）：")
        for row in failed:
            print(
                f"  [case {row['case']} · {row['type']}] "
                f"rank={row['rank']} empty={row['is_empty']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库检索质量评测")
    parser.add_argument("--enforce", action="store_true", help="低于阈值时退出码为 1")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--profile", choices=("auto", "baseline", "semantic"), default="auto"
    )
    parser.add_argument("--json-output", help="把机器可读报告写入指定路径，'-' 表示 stdout")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k 必须大于 0")

    cases = [
        json.loads(line)
        for line in EVAL_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    settings = get_settings()
    profile = args.profile
    if profile == "auto":
        profile = "semantic" if settings.rag_embedder == "bge" else "baseline"
    thresholds = THRESHOLD_PROFILES[profile]

    with tempfile.TemporaryDirectory() as tmp:
        retriever, backend = _build_components(tmp)
        try:
            chunks, ingest_seconds = _ingest(retriever)
            metrics, rows, distribution = _evaluate(retriever, cases, args.top_k)
        finally:
            retriever.store.close()

    misses = {
        name: {"actual": metrics[name], "required": required}
        for name, required in thresholds.items()
        if metrics.get(name, 0.0) < required
    }
    report: dict[str, Any] = {
        "backend": backend,
        "profile": profile,
        "chunks": chunks,
        "ingest_seconds": round(ingest_seconds, 3),
        "metrics": metrics,
        "thresholds": thresholds,
        "distribution": distribution,
        "cases": rows,
        "passed": not misses,
        "misses": misses,
    }
    if args.json_output == "-":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
        if args.json_output:
            output = Path(args.json_output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON 报告：{output}")

    if args.enforce and misses:
        print(f"\n验收未通过（{profile}）：{misses}")
        return 1
    if args.enforce:
        print(f"\n验收通过：{profile} 全部指标达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
