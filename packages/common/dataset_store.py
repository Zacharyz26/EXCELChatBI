"""本地数据集存储（按 dataset_ref 落盘）。

红线1 的支撑：原始/结构化数据只在服务端以 `dataset_ref` 引用，LLM 不直接读。
本切片用本地 parquet（DuckDB 原生读写，无需 pyarrow）代替 MinIO；
生产环境切 MinIO / 对象存储（留 TODO）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from packages.common.config import get_settings


def _base_dir() -> Path:
    """数据集目录，不存在则创建。"""
    d = Path(get_settings().dataset_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_of(dataset_ref: str) -> Path:
    """由 dataset_ref 解析落盘路径。"""
    return _base_dir() / f"{dataset_ref}.parquet"


def _meta_path_of(dataset_ref: str) -> Path:
    """数据集 sidecar 元数据路径（存数据集级安全策略等）。"""
    return _base_dir() / f"{dataset_ref}.meta.json"


def _quote_ident(name: str) -> str:
    """安全引用 SQL 标识符（列名可能含中文/特殊字符）。"""
    return '"' + name.replace('"', '""') + '"'


def save_dataframe(df: pd.DataFrame) -> str:
    """落盘 DataFrame，返回 dataset_ref。

    Args:
        df: 解析得到的结构化数据。

    Returns:
        dataset_ref（唯一标识，供后续 load 引用）。
    """
    dataset_ref = uuid.uuid4().hex
    path = _path_of(dataset_ref)
    # DuckDB 原生写 parquet，保留列类型；df 通过本地变量被 DuckDB 引用
    con = duckdb.connect()
    try:
        con.register("df_view", df)
        con.execute(f"COPY df_view TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return dataset_ref


def load_dataframe(dataset_ref: str) -> pd.DataFrame:
    """按 dataset_ref 读回 DataFrame。

    TODO（大表）：当行数超过 large_table_row_threshold 时，应改为 DuckDB 分块/下推
    聚合，避免整表入内存；当前切片直接整表读回。

    Args:
        dataset_ref: 数据集引用。

    Raises:
        FileNotFoundError: 引用不存在。
    """
    path = _path_of(dataset_ref)
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在: {dataset_ref}")
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT * FROM read_parquet(?)", [path.as_posix()]
        ).df()
    finally:
        con.close()


# ── 数据集 sidecar 元数据（安全策略等）──

def save_metadata(dataset_ref: str, meta: dict[str, Any]) -> None:
    """写入数据集 sidecar 元数据（覆盖式）。"""
    _meta_path_of(dataset_ref).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def load_metadata(dataset_ref: str) -> dict[str, Any] | None:
    """读取数据集 sidecar 元数据；不存在返回 None。"""
    p = _meta_path_of(dataset_ref)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def delete_dataset(dataset_ref: str) -> bool:
    """删除数据集的 parquet 与元数据文件；返回是否确实删掉了数据文件。

    幂等：文件不存在时静默返回 False，不抛错（数据库登记与落盘可能不同步）。
    """
    removed = False
    path = _path_of(dataset_ref)
    if path.exists():
        path.unlink()
        removed = True
    meta = _meta_path_of(dataset_ref)
    if meta.exists():
        meta.unlink()
    return removed


def duplicate_row_count(dataset_ref: str) -> int:
    """整行完全重复的行数（总行数 - 去重行数），下推 DuckDB 计算不进内存。

    Args:
        dataset_ref: 数据集引用。

    Raises:
        FileNotFoundError: 引用不存在。
    """
    path = _path_of(dataset_ref)
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在: {dataset_ref}")
    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT * FROM read_parquet(?))) "
            "FROM read_parquet(?)",
            [path.as_posix(), path.as_posix()],
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


# ── 第2层：聚合下推到 DuckDB 执行（数据不出环境；大表友好）──

def dataset_columns(dataset_ref: str) -> list[str]:
    """返回数据集列名（用于校验列引用，避免整表入内存）。"""
    path = _path_of(dataset_ref)
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在: {dataset_ref}")
    con = duckdb.connect()
    try:
        cur = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [path.as_posix()])
        return [d[0] for d in cur.description]
    finally:
        con.close()


def aggregate(
    dataset_ref: str, group_col: str, value_col: str, agg: str
) -> list[tuple[Any, float, int]]:
    """按 group_col 分组聚合 value_col，下推到 DuckDB 执行。

    Args:
        dataset_ref: 数据集引用。
        group_col: 维度列。
        value_col: 度量列。
        agg: 聚合方式，sum/mean/count。

    Returns:
        [(分组键, 聚合值, 分组行数)]；分组行数供第3层小分组保护使用。

    Raises:
        ValueError: 列不存在或聚合方式不支持。
    """
    cols = dataset_columns(dataset_ref)
    for col in (group_col, value_col):
        if col not in cols:
            raise ValueError(f"列不存在: {col}")

    gi, vi = _quote_ident(group_col), _quote_ident(value_col)
    if agg == "sum":
        expr = f"SUM({vi})"
    elif agg == "mean":
        expr = f"AVG({vi})"
    elif agg == "count":
        expr = "COUNT(*)"
    else:
        raise ValueError(f"不支持的聚合方式: {agg}")

    path = _path_of(dataset_ref)
    sql = (
        f"SELECT {gi} AS g, {expr} AS v, COUNT(*) AS c "
        f"FROM read_parquet(?) WHERE {gi} IS NOT NULL GROUP BY {gi}"
    )
    con = duckdb.connect()
    try:
        rows = con.execute(sql, [path.as_posix()]).fetchall()
    finally:
        con.close()
    return [(r[0], float(r[1]) if r[1] is not None else 0.0, int(r[2])) for r in rows]
