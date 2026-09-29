"""Excel 解析工具实现（pandas / openpyxl，超限工作簿明确拒绝）。

要点：仅产出"数据画像"，原始整表不进 LLM（红线1）；行数上限用于避免解压后 OOM。
"""

from __future__ import annotations

import math
from typing import Any
from zipfile import ZipFile, is_zipfile

import pandas as pd
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe, save_dataframe
from packages.governance.data_boundary import resolve_policy
from packages.governance.redaction import apply_policy

from mcp_servers.excel_parser.profile import ColumnProfile, DataProfile

# 默认样本行数（属"画像"范畴，可喂 LLM；见设计文档 6.1）
_SAMPLE_ROWS = 5
# 每列展示的样本值个数
_SAMPLE_VALUES = 5


class TableTooLargeError(ValueError):
    """工作簿超过行数、列数、单元格或解压大小上限。"""


def parse_excel(args: dict[str, Any]) -> dict[str, Any]:
    """解析 Excel 文件，落地为数据集引用（dataset_ref），不返回整表（红线1）。

    Args:
        args: file_ref（必填）、sheet、header_row、nrows（均可选）。

    Returns:
        {dataset_ref, row_count, column_count}。
    """
    file_ref: str = args["file_ref"]
    sheet: str | int = args.get("sheet", 0)
    header_row: int = args.get("header_row", 0)
    nrows: int | None = args.get("nrows")

    settings = get_settings()
    limit = settings.large_table_row_threshold
    if not isinstance(header_row, int) or not 0 <= header_row <= limit:
        raise ValueError(f"header_row 必须在 0 到 {limit} 之间")
    if nrows is not None and (not isinstance(nrows, int) or nrows < 1):
        raise ValueError("nrows 必须是正整数")
    _guard_archive_size(file_ref)
    # pandas 根据内容选择引擎，不根据扩展名；nrows 始终有界，绝不默认读整表。
    read_rows = min(nrows, limit + 1) if nrows is not None else limit + 1
    with pd.ExcelFile(file_ref) as workbook:
        if workbook.engine == "openpyxl":
            _guard_worksheet(workbook.book, sheet, header_row, nrows)
        df = pd.read_excel(workbook, sheet_name=sheet, header=header_row, nrows=read_rows)
    if len(df) > limit:
        raise TableTooLargeError(f"表行数超过处理上限（{limit} 行），请拆分文件后重试")
    if len(df.columns) > settings.excel_max_columns or df.size > settings.excel_max_cells:
        raise TableTooLargeError("表列数或单元格数超过处理上限，请缩小数据范围后重试")
    dataset_ref = save_dataframe(df)
    return {
        "dataset_ref": dataset_ref,
        "row_count": int(df.shape[0]),
        "column_count": int(df.shape[1]),
    }


def infer_schema(args: dict[str, Any]) -> DataProfile:
    """推断 schema 与统计摘要，生成数据画像（DataProfile）。

    Args:
        args: dataset_ref（必填）。

    Returns:
        DataProfile —— 喂给推理模型的唯一数据视图。
    """
    dataset_ref: str = args["dataset_ref"]
    df = load_dataframe(dataset_ref)
    columns = [_profile_column(df[col]) for col in df.columns]
    sample_rows = _json_safe_records(df.head(_SAMPLE_ROWS))
    profile = DataProfile(
        dataset_ref=dataset_ref,
        row_count=int(df.shape[0]),
        column_count=int(df.shape[1]),
        columns=columns,
        sample_rows=sample_rows,
    )
    # 第1层：按数据集安全策略脱敏采样后再返回（红线1）。
    # 默认宽松；数据集 sidecar 可收紧。仅 VALUES 列的真实单元格才进入 payload。
    return apply_policy(profile, resolve_policy(dataset_ref))


def data_preview(args: dict[str, Any]) -> dict[str, Any]:
    """返回少量样本行供用户确认（前端先展示画像再分析）。"""
    dataset_ref: str = args["dataset_ref"]
    rows: int = args.get("rows", 20)
    df = load_dataframe(dataset_ref)
    return {"rows": _json_safe_records(df.head(rows))}


# ── 内部辅助 ──

def _guard_archive_size(file_ref: str) -> None:
    """在 openpyxl 加载共享字符串等内容之前约束 ZIP 解压预算。"""
    if not is_zipfile(file_ref):
        return
    with ZipFile(file_ref) as archive:
        members = archive.infolist()
        limit = get_settings().excel_max_uncompressed_mb * 1024 * 1024
        if len(members) > 10_000 or sum(item.file_size for item in members) > limit:
            raise TableTooLargeError("工作簿解压大小或文件数超过处理上限，请拆分文件后重试")


def _guard_worksheet(
    workbook: Any, sheet: str | int, header_row: int, nrows: int | None
) -> None:
    """流式核对实际行/宽度，在 pandas 构造矩形 DataFrame 前拒绝越界。

    dimension 可以缺失或伪造，必须 reset；格式化空行及稀疏行也占读取预算。
    显式 nrows <= 上限表示允许读取样本，不要求工作簿剩余部分也在行数上限内。
    """
    settings = get_settings()
    limit = settings.large_table_row_threshold
    names = workbook.sheetnames
    if isinstance(sheet, int) and 0 <= sheet < len(names):
        sheet = names[sheet]
    if sheet not in names:
        raise ValueError(f"工作表不存在: {sheet}")
    worksheet = workbook[sheet]
    worksheet.reset_dimensions()
    sample_only = nrows is not None and nrows <= limit
    max_rows = header_row + 1 + (nrows if sample_only and nrows is not None else limit)
    width = 0
    rows = worksheet.iter_rows(values_only=True)
    try:
        for count, row in enumerate(rows, start=1):
            if count > max_rows:
                raise TableTooLargeError(f"表行数超过处理上限（{limit} 行），请拆分文件后重试")
            width = max(width, len(row))
            if width > settings.excel_max_columns or width * count > settings.excel_max_cells:
                raise TableTooLargeError("表列数或单元格数超过处理上限，请缩小数据范围后重试")
            if sample_only and count == max_rows:
                break
    finally:
        rows.close()


def _dtype_name(series: pd.Series) -> str:
    """把 pandas dtype 归一为画像用的简单类型名。"""
    if pd.api.types.is_bool_dtype(series):
        return "bool"
    if pd.api.types.is_integer_dtype(series):
        return "int"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "str"


def _profile_column(series: pd.Series) -> ColumnProfile:
    """生成单列画像。数值列附 describe 统计摘要。"""
    dtype = _dtype_name(series)
    total = len(series)
    null_ratio = float(series.isna().mean()) if total else 0.0
    distinct_count = int(series.nunique(dropna=True))
    # 先采原始样本值；随后由 governance.redaction 按数据集策略脱敏（见 infer_schema）。
    sample_values = [
        _scalar_to_str(v) for v in series.dropna().unique()[:_SAMPLE_VALUES]
    ]

    profile = ColumnProfile(
        name=str(series.name),
        dtype=dtype,
        null_ratio=null_ratio,
        distinct_count=distinct_count,
        sample_values=sample_values,
    )
    if dtype in ("int", "float"):
        desc = series.astype("float64")
        profile.min = _safe_float(desc.min())
        profile.max = _safe_float(desc.max())
        profile.mean = _safe_float(desc.mean())
        profile.std = _safe_float(desc.std())
        profile.median = _safe_float(desc.median())
    return profile


def _safe_float(value: Any) -> float | None:
    """把统计值转为 JSON 安全的 float（NaN/inf → None）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)


def _scalar_to_str(value: Any) -> str:
    """标量转字符串（样本值用）。"""
    return "" if value is None else str(value)


def _json_safe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → JSON 安全的记录列表（NaN→None，时间→iso 字符串）。"""
    safe = df.copy()
    for col in safe.columns:
        if pd.api.types.is_datetime64_any_dtype(safe[col]):
            safe[col] = safe[col].astype(str)
    records = safe.to_dict(orient="records")
    out: list[dict[str, Any]] = []
    for rec in records:
        out.append(
            {
                str(k): (None if (isinstance(v, float) and math.isnan(v)) else v)
                for k, v in rec.items()
            }
        )
    return out
