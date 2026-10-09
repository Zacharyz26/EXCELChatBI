"""表格解析工具实现（XLSX / legacy XLS / CSV，超限输入明确拒绝）。

要点：仅产出"数据画像"，原始整表不进 LLM（红线1）；行数上限用于避免解压后 OOM。
"""

from __future__ import annotations

import codecs
import csv
import math
import re
from itertools import zip_longest
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile, is_zipfile

import pandas as pd
from openpyxl import load_workbook
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe, save_dataframe
from packages.governance.data_boundary import resolve_policy
from packages.governance.redaction import apply_policy, redact_records
from xlrd.biffh import XLRDError

from mcp_servers.excel_parser.profile import ColumnProfile, DataProfile

# 默认样本行数（属"画像"范畴，可喂 LLM；见设计文档 6.1）
_SAMPLE_ROWS = 5
# 每列展示的样本值个数
_SAMPLE_VALUES = 5
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")
_CSV_DELIMITERS = ",;\t|"
_ISO_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T].*)?$")
_LEADING_ZERO_NUMBER = re.compile(r"^[+-]?0\d+(?:\.\d+)?$")


class TableTooLargeError(ValueError):
    """表格超过行数、列数、单元格或解压大小上限。"""


class FormulaCacheMissingError(ValueError):
    """所选数据范围含公式，但文件没有可供读取的计算结果缓存。"""


class SpreadsheetParseError(ValueError):
    """上传内容不是受支持或可读取的表格文件。"""


def parse_excel(args: dict[str, Any]) -> dict[str, Any]:
    """解析 XLSX、legacy XLS 或单表 CSV，落地为 dataset_ref（红线1）。

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
    read_rows = min(nrows, limit + 1) if nrows is not None else limit + 1
    try:
        if Path(file_ref).suffix.lower() == ".csv":
            if "sheet" in args:
                raise SpreadsheetParseError("CSV 是单表文件，不支持指定工作表")
            df = _read_csv(file_ref, header_row=header_row, nrows=read_rows)
        else:
            df = _read_workbook(
                file_ref,
                sheet=sheet,
                header_row=header_row,
                nrows=nrows,
                read_rows=read_rows,
            )
    except (TableTooLargeError, FormulaCacheMissingError, SpreadsheetParseError):
        raise
    except (BadZipFile, KeyError, UnicodeError, ValueError, XLRDError) as exc:
        raise SpreadsheetParseError(
            "无法读取表格内容；请确认文件未损坏，且格式与 .xlsx、.xls 或 .csv 扩展名一致"
        ) from exc
    if len(df) > limit:
        raise TableTooLargeError(f"表格行数超过处理上限（{limit} 行），请拆分文件后重试")
    if len(df.columns) > settings.excel_max_columns or df.size > settings.excel_max_cells:
        raise TableTooLargeError("表列数或单元格数超过处理上限，请缩小数据范围后重试")
    dataset_ref = save_dataframe(df)
    return {
        "dataset_ref": dataset_ref,
        "row_count": int(df.shape[0]),
        "column_count": int(df.shape[1]),
    }


def _read_workbook(
    file_ref: str,
    *,
    sheet: str | int,
    header_row: int,
    nrows: int | None,
    read_rows: int,
) -> pd.DataFrame:
    """按内容选择 XLSX/XLS 引擎；仅读取已保存值，不执行宏或重算公式。"""
    zipped = is_zipfile(file_ref)
    if zipped:
        _guard_archive_size(file_ref)
    engine = "openpyxl" if zipped else "xlrd"
    with pd.ExcelFile(file_ref, engine=engine) as workbook:
        if engine == "openpyxl":
            _guard_worksheet(workbook.book, sheet, header_row, nrows)
            _guard_formula_cache(file_ref, workbook.book, sheet, header_row, nrows)
        else:
            _guard_legacy_worksheet(workbook.book, sheet, header_row, nrows)
        frame = pd.read_excel(
            workbook,
            sheet_name=sheet,
            header=header_row,
            nrows=read_rows,
            dtype=object,
        )
    return frame.infer_objects(copy=False)


def _read_csv(file_ref: str, *, header_row: int, nrows: int) -> pd.DataFrame:
    """读取 UTF-8/GB18030 单表 CSV，并确定性识别常见分隔符。"""
    encoding, sample = _detect_csv_encoding(file_ref)
    delimiter = _detect_csv_delimiter(sample)
    try:
        frame = pd.read_csv(
            file_ref,
            encoding=encoding,
            sep=delimiter,
            header=header_row,
            nrows=nrows,
            dtype=object,
            engine="python",
            on_bad_lines="error",
        )
    except UnicodeError as exc:
        raise SpreadsheetParseError(
            "CSV 编码无法识别；请使用 UTF-8（可带 BOM）或 GB18030"
        ) from exc
    except pd.errors.ParserError as exc:
        raise SpreadsheetParseError("CSV 结构无效；请检查引号、换行和每行字段数") from exc
    if len(frame.columns) == 0:
        raise SpreadsheetParseError("CSV 没有可读取的表头")
    return _normalize_csv_dataframe(frame)


def _detect_csv_encoding(file_ref: str) -> tuple[str, str]:
    """用严格解码识别受支持编码；采样只用于分隔符识别。"""
    with Path(file_ref).open("rb") as source:
        raw = source.read(256 * 1024)
    if b"\0" in raw:
        raise SpreadsheetParseError("CSV 包含二进制空字节，请确认文件格式")
    for encoding in _CSV_ENCODINGS:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        try:
            sample = decoder.decode(raw, final=False)
        except UnicodeDecodeError:
            continue
        return encoding, sample
    raise SpreadsheetParseError("CSV 编码无法识别；请使用 UTF-8（可带 BOM）或 GB18030")


def _detect_csv_delimiter(sample: str) -> str:
    """识别逗号、分号、制表符或竖线；单列 CSV 回退到逗号。"""
    try:
        return csv.Sniffer().sniff(sample, delimiters=_CSV_DELIMITERS).delimiter
    except csv.Error as exc:
        first_line = next((line for line in sample.splitlines() if line.strip()), "")
        if first_line and not any(delimiter in first_line for delimiter in _CSV_DELIMITERS):
            return ","
        raise SpreadsheetParseError("CSV 分隔符无法识别；请使用逗号、分号或制表符") from exc


def _normalize_csv_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """恢复明确的日期/数值列，同时保留带前导零的文本编号。"""
    normalized = frame.copy()
    for column in normalized.columns:
        series = normalized[column]
        present = series.dropna()
        if present.empty or not present.map(lambda value: isinstance(value, str)).all():
            continue
        texts = present.astype(str).str.strip()
        if texts.map(lambda value: bool(_LEADING_ZERO_NUMBER.fullmatch(value))).any():
            continue
        numeric = pd.to_numeric(texts, errors="coerce")
        if numeric.notna().all():
            normalized[column] = pd.to_numeric(series, errors="coerce")
            continue
        if texts.map(lambda value: bool(_ISO_DATE.fullmatch(value))).all():
            parsed = pd.to_datetime(series, errors="coerce", format="mixed")
            if parsed.loc[present.index].notna().all():
                normalized[column] = parsed
    return normalized.infer_objects(copy=False)


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
    records = _json_safe_records(df.head(rows))
    columns = [_profile_column(df[col]) for col in df.columns]
    return {"rows": redact_records(records, resolve_policy(dataset_ref), columns)}


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


def _guard_worksheet(workbook: Any, sheet: str | int, header_row: int, nrows: int | None) -> None:
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


def _guard_legacy_worksheet(
    workbook: Any,
    sheet: str | int,
    header_row: int,
    nrows: int | None,
) -> None:
    """用 BIFF 目录元数据在 DataFrame 构造前执行 XLS 行、列和单元格预算。"""
    names = workbook.sheet_names()
    if isinstance(sheet, int):
        if not 0 <= sheet < len(names):
            raise SpreadsheetParseError(f"工作表不存在: {sheet}")
        worksheet = workbook.sheet_by_index(sheet)
    else:
        if sheet not in names:
            raise SpreadsheetParseError(f"工作表不存在: {sheet}")
        worksheet = workbook.sheet_by_name(sheet)

    settings = get_settings()
    limit = settings.large_table_row_threshold
    data_rows = max(0, worksheet.nrows - header_row - 1)
    sample_only = nrows is not None and nrows <= limit
    if not sample_only and data_rows > limit:
        raise TableTooLargeError(f"表格行数超过处理上限（{limit} 行），请拆分文件后重试")
    if sample_only:
        assert nrows is not None
        checked_rows = min(worksheet.nrows, header_row + 1 + nrows)
    else:
        checked_rows = worksheet.nrows
    if (
        worksheet.ncols > settings.excel_max_columns
        or worksheet.ncols * checked_rows > settings.excel_max_cells
    ):
        raise TableTooLargeError("表列数或单元格数超过处理上限，请缩小数据范围后重试")


def _guard_formula_cache(
    file_ref: str,
    cached_workbook: Any,
    sheet: str | int,
    header_row: int,
    nrows: int | None,
) -> None:
    """拒绝所选读取范围内没有可用结果缓存的 OOXML 公式。

    pandas/openpyxl 以 ``data_only=True`` 读取公式结果；没有缓存时会返回 ``None``，
    与真实空单元格不可区分。并行流式读取公式视图与结果视图，在发布数据集前明确拒绝，
    避免把“尚未计算”静默解释成业务空值。未选择的工作表及显式采样范围之外不参与检查。
    """
    # 传文件对象而不是路径，沿用 pandas 的“按内容识别”行为；这样实际为 OOXML、
    # 但扩展名被改成 .xls 的文件仍可接受同一套公式与资源边界检查。
    with open(file_ref, "rb") as formula_source:
        formula_workbook = load_workbook(
            formula_source,
            read_only=True,
            data_only=False,
            keep_links=False,
        )
        try:
            formula_worksheet = _selected_worksheet(formula_workbook, sheet)
            cached_worksheet = _selected_worksheet(cached_workbook, sheet)
            formula_worksheet.reset_dimensions()

            limit = get_settings().large_table_row_threshold
            sampled_rows = nrows if nrows is not None and nrows <= limit else limit
            max_rows = header_row + 1 + sampled_rows
            missing: list[str] = []
            missing_count = 0
            formula_rows = formula_worksheet.iter_rows()
            cached_rows = cached_worksheet.iter_rows()
            try:
                for row_number, (formula_row, cached_row) in enumerate(
                    zip_longest(formula_rows, cached_rows, fillvalue=()),
                    start=1,
                ):
                    if row_number > max_rows:
                        break
                    for index, formula_cell in enumerate(formula_row):
                        if formula_cell.data_type != "f":
                            continue
                        cached_value = cached_row[index].value if index < len(cached_row) else None
                        if cached_value is None:
                            missing_count += 1
                            if len(missing) < 5:
                                missing.append(formula_cell.coordinate)
            finally:
                formula_rows.close()
                cached_rows.close()
            if missing_count:
                examples = "、".join(missing)
                raise FormulaCacheMissingError(
                    f"工作表 {formula_worksheet.title} 检测到 {missing_count} 个公式单元格"
                    f"缺少可用缓存值（如 {examples}）。请先用 Excel 或 LibreOffice "
                    "重新计算并保存后再上传；系统不会把这些公式静默当作空值"
                )
        finally:
            formula_workbook.close()


def _selected_worksheet(workbook: Any, sheet: str | int) -> Any:
    names = workbook.sheetnames
    if isinstance(sheet, int) and 0 <= sheet < len(names):
        sheet = names[sheet]
    if sheet not in names:
        raise ValueError(f"工作表不存在: {sheet}")
    return workbook[sheet]


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
    sample_values = [_scalar_to_str(v) for v in series.dropna().unique()[:_SAMPLE_VALUES]]

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
