#!/usr/bin/env python3
"""对当前知识库存储执行只读并发检索冒烟，并输出机器可读延迟报告。"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.common.config import get_settings  # noqa: E402
from packages.rag.embedding import BGEEmbedder, Embedder, HashingEmbedder  # noqa: E402
from packages.rag.rerank import BGEReranker, LexicalReranker, Reranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402
from packages.rag.store import KnowledgeStore, LocalKnowledgeStore  # noqa: E402

DEFAULT_QUERIES = (
    "活跃用户如何定义？",
    "收入按什么时间归属？",
    "次日留存率应该怎样计算？",
    "转化率的统计口径是什么？",
)


def _build_retriever() -> HybridRetriever:
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
            settings.milvus_uri,
            collection=settings.milvus_collection,
            token=settings.milvus_token,
        )
    else:
        store = LocalKnowledgeStore(settings.kb_index_dir)
    return HybridRetriever(
        embedder,
        store,
        reranker,
        min_relevance=settings.rag_min_relevance,
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _query_once(retriever: HybridRetriever, query: str) -> dict[str, Any]:
    result = retriever.retrieve(query, top_k=3)
    return {
        "latency_ms": result.diagnostics.total_ms,
        "is_empty": result.is_empty,
        "returned_hits": len(result.hits),
        "rejection_reason": result.diagnostics.rejection_reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库只读并发检索冒烟")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--query", action="append", help="可重复指定，不写入报告")
    parser.add_argument("--max-p95-ms", type=float, default=2_000.0)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--json-output", help="报告路径；'-' 表示 stdout", default="-")
    args = parser.parse_args()
    if (
        args.requests < 1
        or args.concurrency < 1
        or args.warmup < 0
        or args.max_p95_ms <= 0
    ):
        parser.error("requests、concurrency、max-p95-ms 必须大于 0，warmup 不得小于 0")

    retriever = _build_retriever()
    try:
        status = retriever.store.status()
        if not status.ready or status.chunk_count == 0:
            print("错误：知识库存储未就绪或尚未摄入文档", file=sys.stderr)
            return 1
        queries = tuple(args.query or DEFAULT_QUERIES)
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        warmup_latencies: list[float] = []
        for index in range(args.warmup):
            try:
                warmup_result = _query_once(
                    retriever, queries[index % len(queries)]
                )
                warmup_latencies.append(float(warmup_result["latency_ms"]))
            except Exception as exc:
                errors.append(type(exc).__name__)
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(_query_once, retriever, queries[index % len(queries)])
                for index in range(args.requests)
            ]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    errors.append(type(exc).__name__)

        latencies = [float(item["latency_ms"]) for item in results]
        empty_count = sum(bool(item["is_empty"]) for item in results)
        p95_ms = _percentile(latencies, 0.95) if latencies else None
        passed = (
            not errors
            and p95_ms is not None
            and p95_ms <= args.max_p95_ms
            and (args.allow_empty or empty_count == 0)
        )
        report: dict[str, Any] = {
            "backend": status.backend,
            "active_collection": status.active_collection,
            "requests": args.requests,
            "completed": len(results),
            "concurrency": args.concurrency,
            "warmup": {
                "requests": args.warmup,
                "max_ms": round(max(warmup_latencies), 3)
                if warmup_latencies
                else None,
            },
            "empty_count": empty_count,
            "error_count": len(errors),
            "error_types": sorted(set(errors)),
            "latency_ms": {
                "avg": round(statistics.mean(latencies), 3) if latencies else None,
                "p50": round(_percentile(latencies, 0.50), 3) if latencies else None,
                "p95": round(p95_ms, 3) if p95_ms is not None else None,
                "max": round(max(latencies), 3) if latencies else None,
                "required_p95_max": args.max_p95_ms,
            },
            "passed": passed,
        }
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.json_output == "-":
            print(payload)
        else:
            output = Path(args.json_output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
            print(f"JSON 报告：{output}")
        return 0 if passed else 1
    finally:
        retriever.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
