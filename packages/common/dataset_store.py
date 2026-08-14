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
from packages.common.identifiers import validate_dataset_ref


def _base_dir() -> Path:
    """数据集目录，不存在则创建。"""
    d = Path(get_settings().dataset_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_of(dataset_ref: str) -> Path:
    """由 dataset_ref 解析落盘路径。"""
    clean_ref = validate_dataset_ref(dataset_ref)
    base = _base_dir().resolve()
    path = (base / f"{clean_ref}.parquet").resolve()
    if path.parent != base:  # 纵深防御：即使标识符规则将来变化也不能越界。
        raise ValueError("数据集路径超出存储目录")
    return path


def _meta_path_of(dataset_ref: str) -> Path:
    """数据集 sidecar 元数据路径（存数据集级安全策略等）。"""
    clean_ref = validate_dataset_ref(dataset_ref)
    base = _base_dir().resolve()
    path = (base / f"{clean_ref}.meta.json").resolve()
    if path.parent != base:
        raise ValueError("数据集元数据路径超出存储目录")
    return path


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


def join_key_statistics(
    left_dataset_ref: str,
    right_dataset_ref: str,
    left_key: str,
    right_key: str,
    join_type: str,
) -> dict[str, Any]:
    """用 DuckDB 下推计算 Join 预检统计，不返回任何原始键值或数据行。"""
    if join_type not in {"inner", "left", "right", "full"}:
        raise ValueError(f"不支持的 Join 类型: {join_type}")

    left_columns = dataset_columns(left_dataset_ref)
    right_columns = dataset_columns(right_dataset_ref)
    if left_key not in left_columns:
        raise ValueError(f"左侧关联键不存在: {left_key}")
    if right_key not in right_columns:
        raise ValueError(f"右侧关联键不存在: {right_key}")

    left_path = _path_of(left_dataset_ref)
    right_path = _path_of(right_dataset_ref)
    left_ident = _quote_ident(left_key)
    right_ident = _quote_ident(right_key)
    con = duckdb.connect()
    try:
        left_profile = _join_key_profile(con, left_path, left_ident)
        right_profile = _join_key_profile(con, right_path, right_ident)
        compatible = _duckdb_type_family(left_profile["dtype"]) == _duckdb_type_family(
            right_profile["dtype"]
        )
        matching_key_count = 0
        matched_left_rows = 0
        matched_right_rows = 0
        inner_rows = 0
        left_matching_max_rows_per_key = 0
        right_matching_max_rows_per_key = 0
        if compatible:
            sql = f"""
                WITH left_keys AS (
                    SELECT {left_ident} AS join_key, COUNT(*) AS key_rows
                    FROM read_parquet(?)
                    WHERE {left_ident} IS NOT NULL
                    GROUP BY {left_ident}
                ), right_keys AS (
                    SELECT {right_ident} AS join_key, COUNT(*) AS key_rows
                    FROM read_parquet(?)
                    WHERE {right_ident} IS NOT NULL
                    GROUP BY {right_ident}
                )
                SELECT
                    COUNT(*),
                    COALESCE(SUM(left_keys.key_rows), 0),
                    COALESCE(SUM(right_keys.key_rows), 0),
                    COALESCE(SUM(left_keys.key_rows * right_keys.key_rows), 0),
                    COALESCE(MAX(left_keys.key_rows), 0),
                    COALESCE(MAX(right_keys.key_rows), 0)
                FROM left_keys
                INNER JOIN right_keys USING (join_key)
            """
            match = con.execute(
                sql,
                [left_path.as_posix(), right_path.as_posix()],
            ).fetchone()
            if match is not None:
                matching_key_count = int(match[0])
                matched_left_rows = int(match[1])
                matched_right_rows = int(match[2])
                inner_rows = int(match[3])
                left_matching_max_rows_per_key = int(match[4])
                right_matching_max_rows_per_key = int(match[5])
    finally:
        con.close()

    unmatched_left = left_profile["row_count"] - matched_left_rows
    unmatched_right = right_profile["row_count"] - matched_right_rows
    estimated_rows = {
        "inner": inner_rows,
        "left": inner_rows + unmatched_left,
        "right": inner_rows + unmatched_right,
        "full": inner_rows + unmatched_left + unmatched_right,
    }[join_type]
    return {
        "compatible_key_types": compatible,
        "left": left_profile,
        "right": right_profile,
        "matching_key_count": matching_key_count,
        "matched_left_rows": matched_left_rows,
        "matched_right_rows": matched_right_rows,
        "estimated_output_rows": estimated_rows,
        "left_matching_max_rows_per_key": left_matching_max_rows_per_key,
        "right_matching_max_rows_per_key": right_matching_max_rows_per_key,
    }


def materialize_join(
    left_dataset_ref: str,
    right_dataset_ref: str,
    left_key: str,
    right_key: str,
    join_type: str,
) -> dict[str, Any]:
    """用固定等值 Join 生成新 Parquet；调用方必须先完成预检与授权。"""
    if join_type not in {"inner", "left", "right", "full"}:
        raise ValueError(f"不支持的 Join 类型: {join_type}")
    left_path = _path_of(left_dataset_ref)
    right_path = _path_of(right_dataset_ref)
    for path, side in ((left_path, "左侧"), (right_path, "右侧")):
        if not path.exists():
            raise FileNotFoundError(f"{side}数据集不存在")

    left_columns = dataset_columns(left_dataset_ref)
    right_columns = dataset_columns(right_dataset_ref)
    if left_key not in left_columns:
        raise ValueError(f"左侧关联键不存在: {left_key}")
    if right_key not in right_columns:
        raise ValueError(f"右侧关联键不存在: {right_key}")

    output_columns: list[str] = list(left_columns)
    used_names = {column.casefold() for column in output_columns}
    right_projection: list[tuple[str, str]] = []
    for column in right_columns:
        if column == right_key and right_key == left_key:
            continue
        output_name = column
        suffix = 1
        while output_name.casefold() in used_names:
            tail = "_right" if suffix == 1 else f"_right_{suffix}"
            output_name = f"{column}{tail}"
            suffix += 1
        used_names.add(output_name.casefold())
        output_columns.append(output_name)
        right_projection.append((column, output_name))

    projections: list[str] = []
    for column in left_columns:
        source = f"left_data.{_quote_ident(column)}"
        if (
            column == left_key
            and left_key == right_key
            and join_type in {"right", "full"}
        ):
            source = (
                f"COALESCE({source}, right_data.{_quote_ident(right_key)})"
            )
        projections.append(f"{source} AS {_quote_ident(column)}")
    projections.extend(
        f"right_data.{_quote_ident(column)} AS {_quote_ident(output_name)}"
        for column, output_name in right_projection
    )
    join_keyword = {
        "inner": "INNER JOIN",
        "left": "LEFT JOIN",
        "right": "RIGHT JOIN",
        "full": "FULL OUTER JOIN",
    }[join_type]
    output_ref = uuid.uuid4().hex
    output_path = _path_of(output_ref)
    output_literal = output_path.as_posix().replace("'", "''")
    query = f"""
        COPY (
            SELECT {', '.join(projections)}
            FROM read_parquet(?) AS left_data
            {join_keyword} read_parquet(?) AS right_data
              ON left_data.{_quote_ident(left_key)} = right_data.{_quote_ident(right_key)}
        ) TO '{output_literal}' (FORMAT PARQUET)
    """
    connection = duckdb.connect()
    try:
        connection.execute(query, [left_path.as_posix(), right_path.as_posix()])
        row = connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [output_path.as_posix()]
        ).fetchone()
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    return {
        "dataset_ref": output_ref,
        "rows": int(row[0]) if row is not None else 0,
        "columns": output_columns,
        "right_column_mapping": {
            source: output for source, output in right_projection
        },
    }


def _join_key_profile(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    key_ident: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        f"SELECT {key_ident} FROM read_parquet(?) LIMIT 0",
        [path.as_posix()],
    )
    dtype = str(cursor.description[0][1])
    row = connection.execute(
        f"""
        SELECT
            COUNT(*),
            COUNT({key_ident}),
            COUNT(DISTINCT {key_ident})
        FROM read_parquet(?)
        """,
        [path.as_posix()],
    ).fetchone()
    if row is None:  # pragma: no cover - aggregate query always yields one row
        raise RuntimeError("Join 预检统计为空")
    row_count = int(row[0])
    non_null_count = int(row[1])
    distinct_count = int(row[2])
    return {
        "dtype": dtype,
        "row_count": row_count,
        "non_null_count": non_null_count,
        "null_count": row_count - non_null_count,
        "distinct_count": distinct_count,
        "duplicate_key_rows": non_null_count - distinct_count,
        "unique_non_null": non_null_count > 0 and non_null_count == distinct_count,
    }


def _duckdb_type_family(dtype: str) -> str:
    normalized = dtype.upper()
    numeric_tokens = (
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "FLOAT",
        "DOUBLE",
        "DECIMAL",
    )
    if any(token in normalized for token in numeric_tokens):
        return "numeric"
    if normalized.startswith(("DATE", "TIMESTAMP")):
        return "date_time"
    if normalized.startswith("TIME"):
        return "time"
    if normalized == "VARCHAR":
        return "string"
    if normalized == "BOOLEAN":
        return "boolean"
    return f"exact:{normalized}"
