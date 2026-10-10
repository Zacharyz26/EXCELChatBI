#!/usr/bin/env python3
"""Bounded synthetic Milvus Lite resource probe; never reads project data."""

from __future__ import annotations

import argparse
import json
import math
import resource
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.rag.embedding import HashingEmbedder  # noqa: E402
from packages.rag.milvus_store import MilvusKnowledgeStore  # noqa: E402
from packages.rag.pipeline import chunk_and_embed  # noqa: E402
from packages.rag.rerank import LexicalReranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402

MAX_CHUNKS = 5_000
MAX_REQUESTS = 1_000
MAX_CONCURRENCY = 8
MAX_DIMENSION = 1_024


def validate_probe_shape(
    chunks: int,
    requests: int,
    concurrency: int,
    dimension: int,
) -> None:
    """Keep the diagnostic probe within a laptop-safe envelope."""
    bounds = (
        ("chunks", chunks, 1, MAX_CHUNKS),
        ("requests", requests, 1, MAX_REQUESTS),
        ("concurrency", concurrency, 1, MAX_CONCURRENCY),
        ("dimension", dimension, 8, MAX_DIMENSION),
    )
    for label, value, minimum, maximum in bounds:
        if not minimum <= value <= maximum:
            raise ValueError(f"{label} 必须在 {minimum} 到 {maximum} 之间")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def run_probe(
    *,
    chunks: int,
    requests: int,
    concurrency: int,
    dimension: int,
) -> dict[str, Any]:
    """Run a temporary Milvus Lite probe and return metadata-only metrics."""
    validate_probe_shape(chunks, requests, concurrency, dimension)
    embedder = HashingEmbedder(dim=dimension)
    queries = ("指标定义", "统计口径", "项目边界", "知识检索")
    with tempfile.TemporaryDirectory(prefix="chatbi-rag-probe-") as temp_dir:
        store = MilvusKnowledgeStore(str(Path(temp_dir) / "probe.db"), collection="probe_chunks")
        try:
            records = []
            for index in range(chunks):
                text = f"合成知识片段 {index}：指标定义与统计口径 {index % 17}。"
                records.extend(chunk_and_embed(text, f"synthetic-{index}.md", embedder))
            insert_started = time.perf_counter()
            inserted = store.add(records)
            insert_ms = (time.perf_counter() - insert_started) * 1_000
            retriever = HybridRetriever(
                embedder,
                store,
                LexicalReranker(),
                max_concurrent_queries=concurrency,
                max_queued_queries=0,
                queue_timeout_seconds=2,
                max_query_chars=200,
            )
            latencies: list[float] = []
            errors: list[str] = []
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [
                    executor.submit(retriever.retrieve, queries[index % len(queries)], 3)
                    for index in range(requests)
                ]
                for future in as_completed(futures):
                    try:
                        latencies.append(float(future.result().diagnostics.total_ms))
                    except Exception as exc:
                        errors.append(type(exc).__name__)
            wall_ms = (time.perf_counter() - started) * 1_000
            usage = resource.getrusage(resource.RUSAGE_SELF)
            return {
                "probe": "synthetic_milvus_lite",
                "semantic_model_used": False,
                "persistent_project_data_read": False,
                "chunks_requested": chunks,
                "chunks_inserted": inserted,
                "requests": requests,
                "completed": len(latencies),
                "concurrency": concurrency,
                "dimension": dimension,
                "insert_ms": round(insert_ms, 3),
                "query_wall_ms": round(wall_ms, 3),
                "throughput_rps": round(len(latencies) / (wall_ms / 1_000), 3),
                "latency_ms": {
                    "avg": round(statistics.mean(latencies), 3) if latencies else None,
                    "p50": round(_percentile(latencies, 0.5), 3) if latencies else None,
                    "p95": round(_percentile(latencies, 0.95), 3) if latencies else None,
                    "max": round(max(latencies), 3) if latencies else None,
                },
                "max_rss_kib": int(usage.ru_maxrss),
                "error_count": len(errors),
                "error_types": sorted(set(errors)),
                "passed": len(latencies) == requests and not errors,
            }
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded synthetic Milvus Lite resource probe")
    parser.add_argument("--chunks", type=int, default=200)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dimension", type=int, default=64)
    args = parser.parse_args()
    try:
        report = run_probe(
            chunks=args.chunks,
            requests=args.requests,
            concurrency=args.concurrency,
            dimension=args.dimension,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
