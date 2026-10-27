"""画像数字的封闭语义：列统计不能借用样本值、其他列或整行质量计数。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from packages.session.models import JsonObject
from packages.session.task_models import ClaimDraft, EvidenceRecord

from apps.orchestrator.control.dataset_identity import (
    identity_declarations,
    identity_literal,
    identity_matches_binding,
)
from apps.orchestrator.control.numeric_display import (
    decimal_from_token,
    display_quantum,
    matches_display_value,
    parse_numeric_display,
)
from apps.orchestrator.control.profile_population import mask_population, stated_population

_COLUMN_STATS = {
    "min",
    "max",
    "mean",
    "median",
    "std",
    "distinct_count",
    "null_ratio",
    "null_count",
}
_COUNTS = {
    "row_count",
    "column_count",
    "distinct_count",
    "null_count",
    "non_null_count",
    "duplicate_rows",
    "duplicate_values_at_least",
    "duplicate_excess_count",
}
_PREFIX = {
    "min": r"最小值?",
    "max": r"最大值?",
    "mean": r"均值|平均值|平均",
    "median": r"中位数",
    "std": r"标准差",
    "null_ratio": r"空值率|缺失率|缺失比例|null_ratio",
    "distinct_count": r"不同取值数|不同值数量|唯一值数|distinct(?:_count)?",
    "null_count": r"精确空值数|空值数|空值|缺失值数",
    "non_null_count": r"非空值数|非空记录数|非空数|非空",
    "row_count": r"总行数|行数|记录数",
    "column_count": r"列数|字段数",
    "duplicate_rows": r"整行重复多余记录数",
    "duplicate_excess_count": r"重复多余记录数",
    "duplicate_ratio": r"重复率",
}


def profile_statistic_labels(text: str) -> set[str]:
    """从现有画像语法提取原文统计标签，不纳入标签之后的数值。"""
    return {
        match.group()
        for pattern in _PREFIX.values()
        for match in re.finditer(pattern, text, re.IGNORECASE)
    }


def annotate_profile_values(values: list[JsonObject], result: Any) -> None:
    """按完整列名和精确计数证明绑定画像事实；历史舍入比例不能补造计数。"""
    if not isinstance(result, dict) or not isinstance(result.get("profile"), dict):
        return
    fields = {c.get("name") for c in result["profile"].get("columns", []) if isinstance(c, dict)}
    # A role profile is usable only when its named column has an exact count.
    # Legacy rounded ratios never create non-null facts; order is irrelevant.
    columns = result["profile"].get("columns", [])
    row_count = result["profile"].get("row_count")
    exact: dict[str, int] = {}
    if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0:
        for column in columns:
            nulls = column.get("null_count")
            field = column.get("name")
            ratio = column.get("null_ratio")
            if (
                isinstance(field, str)
                and sum(c.get("name") == field for c in columns) == 1
                and isinstance(nulls, int)
                and not isinstance(nulls, bool)
                and 0 <= nulls <= row_count
                and isinstance(ratio, int | float)
                and not isinstance(ratio, bool)
                and 0 <= ratio <= 1
                and round(ratio, 4) == round(nulls / row_count if row_count else 0.0, 4)
            ):
                exact[field] = row_count - nulls
    for index, role in enumerate((result.get("roles") or {}).get("columns", [])):
        field = role.get("column")
        count = (role.get("profile_evidence") or {}).get("non_null_count")
        if field in exact and type(count) is int and count == exact[field]:
            for item in values:
                if item.get("path") == f"$.roles.columns[{index}].profile_evidence.non_null_count":
                    item["profile_column"] = field
                    item["profile_count_basis"] = "non_null_rows"
    quality = result.get("quality") or {}
    for index, issue in enumerate(quality.get("issues", [])):
        columns = issue.get("columns", [])
        if issue.get("code") == "duplicate_rows" and not columns:
            for item in values:
                if item.get("path") == f"$.quality.issues[{index}].evidence.duplicate_ratio":
                    item["profile_dataset_stat"] = "duplicate_ratio"
        if len(columns) != 1 or columns[0] not in fields:
            continue
        allowed = (
            {"duplicate_values_at_least", "distinct_count", "non_null_count"}
            if issue.get("code") == "non_unique_identifier"
            else set()
        )
        for item in values:
            if item.get("path") in {f"$.quality.issues[{index}].evidence.{s}" for s in allowed}:
                if item["path"].endswith(".non_null_count") and (
                    columns[0] not in exact
                    or issue["evidence"].get("non_null_count") != exact[columns[0]]
                ):
                    continue
                item["profile_column"] = columns[0]
                if item["path"].endswith(".non_null_count"):
                    item["profile_count_basis"] = "non_null_rows"
                if item["path"].endswith(".duplicate_values_at_least"):
                    proof = issue["evidence"]
                    column = next(
                        c for c in result["profile"]["columns"] if c.get("name") == columns[0]
                    )
                    distinct = column.get("distinct_count")
                    if (
                        columns[0] in exact
                        and type(distinct) is int
                        and 0 <= distinct <= exact[columns[0]]
                        and type(proof.get("non_null_count")) is int
                        and type(proof.get("distinct_count")) is int
                        and proof.get("non_null_count") == exact[columns[0]]
                        and proof.get("distinct_count") == distinct
                        and type(proof.get("duplicate_values_at_least")) is int
                        and proof["duplicate_values_at_least"] == exact[columns[0]] - distinct
                    ):
                        item["profile_count_basis"] = "excess_non_null_occurrences"


def profile_fact(item: JsonObject) -> JsonObject | None:
    """识别可直接证明画像结论的路径；不将 sample_rows、置信度或建议计为事实。"""
    path = str(item.get("path", ""))
    role = path.rsplit(".", 1)[-1]
    column: str | None = None
    if re.fullmatch(r"\$\.profile\.columns\[\d+\]\.\w+", path):
        if role not in _COLUMN_STATS or not isinstance(item.get("metric"), str):
            return None
        column = item["metric"]
    elif path in {"$.profile.row_count", "$.profile.column_count", "$.quality.duplicate_rows"}:
        pass
    elif item.get("profile_dataset_stat") == "duplicate_ratio" and role == "duplicate_ratio":
        pass
    elif isinstance(item.get("profile_column"), str):
        if role not in {"duplicate_values_at_least", "distinct_count", "non_null_count"}:
            return None
        column = item["profile_column"]
    else:
        return None
    count_basis: str | None = None
    if role == "duplicate_values_at_least":
        if item.get("profile_count_basis") != "excess_non_null_occurrences":
            return None
        role = "duplicate_excess_count"
        count_basis = "excess_non_null_occurrences"
    elif role == "duplicate_rows":
        count_basis = "excess_rows_keep_one"
    elif role == "non_null_count":
        # A historical profile_column annotation alone could come from a rounded
        # ratio. Only the current exact-count attestation certifies this fact.
        if item.get("profile_count_basis") != "non_null_rows":
            return None
        count_basis = "non_null_rows"
    return {
        **({"count_basis": count_basis} if count_basis else {}),
        "metric": column or role,
        "statistic": role,
        "statistic_scope": f"profile.column:{column}" if column else "profile.dataset",
        "object": {"kind": "profile_column", "column": column}
        if column
        else {"kind": "profile_dataset"},
        "unit": "count"
        if role in _COUNTS
        else "ratio"
        if role in {"null_ratio", "duplicate_ratio"}
        else "raw",
        "profile_fact": True,
    }


def has_column_profile(summary: JsonObject) -> bool:
    """旧 Evidence 也能使用原有明确列路径；不重写或臆造旧来源。"""
    return any(
        re.fullmatch(r"\$\.profile\.columns\[\d+\]\.\w+", str(i.get("path", "")))
        and isinstance(i.get("metric"), str)
        for i in summary.get("value_index", [])
    )


def _normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


_TRUSTED_FIELD_UNITS = {
    "%",
    "cny",
    "g",
    "h",
    "kg",
    "km",
    "m",
    "min",
    "mm",
    "rmb",
    "s",
    "usd",
    "个",
    "万元",
    "亿元",
    "人民币",
    "件",
    "克",
    "元",
    "公里",
    "公斤",
    "分",
    "分钟",
    "厘米",
    "千克",
    "千米",
    "吨",
    "周",
    "天",
    "小时",
    "年",
    "日",
    "月",
    "条",
    "次",
    "毫克",
    "毫米",
    "百分比",
    "百分点",
    "秒",
    "米",
    "美元",
}


def _field_unit_parts(field: str) -> tuple[str, str, str] | None:
    """Return a base-field alias only for a conservative, known unit suffix."""
    label = _normal(field)
    match = re.fullmatch(r"(.+)\(([^()]+)\)", label)
    if match is None:
        return None
    unit = match[2].strip()
    if unit.casefold() not in _TRUSTED_FIELD_UNITS:
        return None
    return label, match[1].rstrip(), unit


def _field_unit(field: str) -> str | None:
    parts = _field_unit_parts(field)
    return parts[2] if parts is not None else None


def _field_spellings(field: str, context: str) -> list[str]:
    """Return the full identity and its unit-qualified base spelling."""
    label = _normal(field)
    parts = _field_unit_parts(label)
    return [label, parts[1]] if parts is not None and parts[2] in _normal(context) else [label]


def _exact_unit_declaration(clause: str, field: str) -> bool:
    """Accept only bounded declarations of the unit encoded by the field name."""
    unit = _field_unit(field)
    if unit is None:
        return False
    text = _normal(clause).strip()
    literals = [unit, f"“{unit}”", f"「{unit}」", f"『{unit}』", f'"{unit}"', f"'{unit}'"]
    literal = "(?:" + "|".join(re.escape(value) for value in literals) + ")"
    return any(
        re.fullmatch(pattern + literal, text) is not None
        for pattern in (
            r"(?:字段名|列名)(?:本身)?(?:已)?(?:标注|注明)(?:为)?\s*",
            r"单位(?:为|即)\s*",
            r"单位即列名(?:中(?:的)?|(?:已)?(?:标注|注明)(?:的|为)?)\s*",
        )
    )


def _fields_in(text: str, fields: set[str], *, context: str | None = None) -> set[str]:
    """Match an entire named field, never a suffix inside another identifier."""
    normalized = _normal(text)
    found: set[str] = set()
    for field in fields:
        label = _normal(field)
        for spelling in _field_spellings(field, context or text):
            for match in re.finditer(r"(?<![\w])" + re.escape(spelling), normalized):
                tail = normalized[match.end() :]
                if spelling != label and re.match(r"\s*\(", tail):
                    continue  # A differently stated unit is a different field.
                # Chinese has no word separators. Require a field predicate,
                # statistic label or punctuation after the complete identity.
                if tail and not re.match(
                    r"(?:[^\w]|(?:字段|列)(?=\s*[:：])|的|为|是|有|存在|"
                    r"描述统计|统计|最小|最大|均值|平均|中位数|标准差|"
                    r"空值率|缺失率|缺失比例|null_ratio|非空|重复多余记录数|精确空值数|空值数|缺失值数|"
                    r"不同取值数|不同值数量|唯一值数|distinct)",
                    tail,
                    re.I,
                ):
                    continue
                found.add(field)
    return {
        f
        for f in found
        if not any(
            f != other and _normal(f) != _normal(other) and _normal(f) in _normal(other)
            for other in found
        )
    }


_STAT_LABEL = "(?:" + "|".join(_PREFIX.values()) + ")"
_STAT_END = re.compile(_STAT_LABEL + r"\s*(?:约|为|是|=)*\s*$", re.I)
_NUMBER = r"[-+]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"


@dataclass(frozen=True)
class RatioRestatement:
    """一个完整的原比例及百分比重述，两者必须共同核验。"""

    raw_token: str
    percent_token: str
    raw_span: tuple[int, int]
    percent_number_span: tuple[int, int]
    percent_display_span: tuple[int, int]
    label_span: tuple[int, int]
    span: tuple[int, int]
    connector: Literal["implicit", "approximate", "equivalent"]

    def owns(self, start: int, end: int) -> bool:
        """判断一个抽取 token 是否属于该重述的原值或百分比值。"""
        return (start, end) == self.raw_span or (
            start == self.percent_number_span[0]
            and end in {self.percent_number_span[1], self.percent_display_span[1]}
        )


def parse_ratio_restatement(text: str, left: int, right: int) -> RatioRestatement | None:
    """解析一个有界缺失比例重述；不接受分母、范围或额外内容。"""
    clause = text[left:right]
    match = re.fullmatch(
        r"(?P<subject>.*?)"
        r"(?P<label>" + _PREFIX["null_ratio"] + r")\s*(?:约|为|是|[:：=])*\s*"
        r"(?P<raw>" + _NUMBER + r")\s*"
        r"(?P<open>[(（])\s*(?P<connector>约|即)?\s*"
        r"(?P<percent>" + _NUMBER + r")(?P<percent_suffix>\s*[%％])\s*"
        r"(?P<close>[)）])\s*[。.!！?？;；]?\s*",
        clause,
        re.I,
    )
    if match is None or (match["open"], match["close"]) not in {("(", ")"), ("（", "）")}:
        return None

    def absolute(span: tuple[int, int]) -> tuple[int, int]:
        return left + span[0], left + span[1]

    connector: Literal["implicit", "approximate", "equivalent"]
    if match["connector"] == "约":
        connector = "approximate"
    elif match["connector"] == "即":
        connector = "equivalent"
    else:
        connector = "implicit"
    return RatioRestatement(
        raw_token=match["raw"],
        percent_token=match["percent"],
        raw_span=absolute(match.span("raw")),
        percent_number_span=absolute(match.span("percent")),
        percent_display_span=absolute((match.start("percent"), match.end("percent_suffix"))),
        label_span=absolute(match.span("label")),
        span=absolute(match.span()),
        connector=connector,
    )


def _ratio_restatement_matches_evidence(
    restatement: RatioRestatement, evidence_value: Decimal
) -> bool:
    """用共享显示舍入规则分别核验原比例和百分比。"""
    raw = decimal_from_token(restatement.raw_token)
    percent = decimal_from_token(restatement.percent_token)
    raw_quantum = display_quantum(restatement.raw_token)
    percent_quantum = display_quantum(restatement.percent_token)
    if None in {raw, percent, raw_quantum, percent_quantum}:
        return False
    assert raw is not None and percent is not None
    assert raw_quantum is not None and percent_quantum is not None
    return matches_display_value(
        evidence_value,
        raw,
        display_factor=Decimal(1),
        quantum=raw_quantum,
    ) and matches_display_value(
        evidence_value,
        percent,
        display_factor=Decimal("0.01"),
        quantum=percent_quantum,
    )


@dataclass(frozen=True)
class ProfileStatHeader:
    """一个已验证的画像统计头；span 不包含末尾分隔冒号。"""

    field: str
    span: tuple[int, int]
    separator_end: int
    kind: str = "statistics"


_COLUMN_LABEL_CONFIRMATION = re.compile(
    r"(?P<subject>字段名|列名)(?P<self>本身)?"
    r"(?P<state>尚未|仍未|并未|没有|已经|已|未|是否|可能|也许|似乎|据说)?"
    r"(?P<action>标注|注明)(?P<completion>过|了)?\s*",
    re.I,
)
_COLUMN_LABEL_NEGATIVE_STATES = frozenset({"未", "尚未", "仍未", "没有", "并未"})
_COLUMN_LABEL_UNCERTAIN_STATES = frozenset({"是否", "可能", "也许", "似乎", "据说"})
_COLUMN_LABEL_PROVENANCE_FORMS = (
    re.compile(
        r"(?P<state>并非|不是|不|可能|是否|似乎|据说)?"
        r"(?:来自|来源于)(?P<source>列名|字段名)\s*",
        re.I,
    ),
    re.compile(
        r"来源(?:(?P<state>并非|不是|可能|是否|似乎|据说)(?:为|是)?|(?:为|是))"
        r"(?P<source>列名|字段名)\s*",
        re.I,
    ),
)
_COLUMN_LABEL_PROVENANCE_NEGATIVE = frozenset({"并非", "不是", "不"})
_COLUMN_LABEL_PROVENANCE_UNCERTAIN = frozenset({"可能", "是否", "似乎", "据说"})


@dataclass(frozen=True)
class ProfileHeaderMetadataAtom:
    """一个有类型且带极性的统计头 metadata 原子。"""

    kind: Literal["unit_declaration", "column_label_confirmation", "column_label_provenance"]
    polarity: Literal["affirmative", "negative", "uncertain"]
    value: str


def _column_label_confirmation_polarity(
    atom: str,
) -> Literal["affirmative", "negative", "uncertain", "invalid"]:
    """分类有界列名确认；只把明确肯定状态交给统计头解析器。"""
    match = _COLUMN_LABEL_CONFIRMATION.fullmatch(_normal(atom).strip())
    if match is None:
        return "invalid"
    state = match["state"] or ""
    if state in _COLUMN_LABEL_NEGATIVE_STATES:
        return "negative"
    if state in _COLUMN_LABEL_UNCERTAIN_STATES:
        return "uncertain"
    return "affirmative"


def _column_label_provenance(
    atom: str,
) -> tuple[Literal["affirmative", "negative", "uncertain"], str] | None:
    """解析来源于列名/字段名的有界陈述，并保留其极性。"""
    text = _normal(atom).strip()
    match = next(
        (
            result
            for candidate in _COLUMN_LABEL_PROVENANCE_FORMS
            if (result := candidate.fullmatch(text)) is not None
        ),
        None,
    )
    if match is None:
        return None
    state = match["state"] or ""
    if state in _COLUMN_LABEL_PROVENANCE_NEGATIVE:
        polarity: Literal["affirmative", "negative", "uncertain"] = "negative"
    elif state in _COLUMN_LABEL_PROVENANCE_UNCERTAIN:
        polarity = "uncertain"
    else:
        polarity = "affirmative"
    return polarity, match["source"]


def _parse_profile_header_metadata_atom(atom: str, field: str) -> ProfileHeaderMetadataAtom | None:
    """将单位、列名确认和列名来源解析为同一个受限类型。"""
    if _exact_unit_declaration(atom, field):
        return ProfileHeaderMetadataAtom(
            kind="unit_declaration",
            polarity="affirmative",
            value=_field_unit(field) or "",
        )
    confirmation = _column_label_confirmation_polarity(atom)
    if confirmation != "invalid":
        return ProfileHeaderMetadataAtom(
            kind="column_label_confirmation",
            polarity=confirmation,
            value="column_label",
        )
    provenance = _column_label_provenance(atom)
    if provenance is None:
        return None
    return ProfileHeaderMetadataAtom(
        kind="column_label_provenance",
        polarity=provenance[0],
        value=provenance[1],
    )


def _profile_header_metadata_valid(metadata: str, field: str) -> bool:
    """只接受唯一且明确肯定的有类型 metadata 原子。"""
    if not metadata.strip() or any(char in metadata for char in "()"):
        return False
    atoms = [atom.strip() for atom in re.split(r"[,、]", metadata)]
    if any(not atom for atom in atoms):
        return False
    parsed = [_parse_profile_header_metadata_atom(atom, field) for atom in atoms]
    if any(atom is None or atom.polarity != "affirmative" for atom in parsed):
        return False
    kinds = [atom.kind for atom in parsed if atom is not None]
    return len(kinds) == len(set(kinds))


def _profile_scope_metadata_valid(metadata: str, fields: set[str]) -> bool:
    """接受带严格 population 证明的统计口径头，不吞掉任意括号说明。"""
    request = stated_population(metadata, fields)
    if request is None or not request.valid:
        return False
    prefix = metadata[: request.span[0]].strip()
    suffix = metadata[request.span[1] :].strip()
    if suffix:
        return False
    return bool(
        re.fullmatch(
            r"(?:口径\s*[:：]\s*)?(?:基于\s*)?(?:全部|当前)?\s*" r"(?:\d+\s*行)?\s*(?:[、,，]\s*)?",
            prefix,
        )
    )


def parse_profile_stat_header(
    text: str, fields: set[str], *, context: str | None = None
) -> ProfileStatHeader | None:
    """解析唯一完整字段的有界统计头，并返回统一的已消费 span。"""
    normalized = _normal(text)
    start_match = re.match(r"\s*", normalized)
    assert start_match is not None
    start = start_match.end()
    candidates: list[ProfileStatHeader] = []
    for field in fields:
        for spelling in _field_spellings(field, context or normalized):
            if not normalized.startswith(spelling, start):
                continue
            cursor = start + len(spelling)
            kind = re.match(r"\s*(?:的)?(?:描述)?统计", normalized[cursor:])
            if kind is None:
                continue
            cursor += kind.end()
            space = re.match(r"\s*", normalized[cursor:])
            assert space is not None
            cursor += space.end()
            if cursor < len(normalized) and normalized[cursor] == "(":
                closing = normalized.find(")", cursor + 1)
                if closing < 0:
                    continue
                metadata = normalized[cursor + 1 : closing]
                if not (
                    _profile_header_metadata_valid(metadata, field)
                    or _profile_scope_metadata_valid(metadata, fields)
                ):
                    continue
                cursor = closing + 1
            header_end = cursor
            separator = re.match(r"\s*:?[\s]*", normalized[cursor:])
            assert separator is not None
            separator_end = cursor + separator.end()
            trailing = normalized[separator_end:]
            if trailing and re.match(_STAT_LABEL, trailing, re.I) is None:
                continue
            candidates.append(
                ProfileStatHeader(
                    field=field,
                    span=(start, header_end),
                    separator_end=separator_end,
                )
            )
    unique = {
        (candidate.field, candidate.span, candidate.separator_end): candidate
        for candidate in candidates
    }
    return next(iter(unique.values())) if len(unique) == 1 else None


def _clause_boundaries(text: str, fields: set[str]) -> list[int]:
    # Protect exact field identities and only metadata accepted by the shared
    # header parser. Unknown parentheses remain visible to ordinary boundaries.
    protected = [(m.start(), m.end()) for f in fields for m in re.finditer(re.escape(f), text)]
    if header := parse_profile_stat_header(text, fields, context=text):
        protected.append(header.span)
    protected.extend(
        m.span()
        for m in identity_declarations(text, headers_only=True)
        if identity_literal(m["value"])
    )
    boundaries = []
    for i, char in enumerate(text):
        if char not in "，,、：:；;":
            continue
        if any(a <= i < b for a, b in protected):
            continue
        if (
            char == ","
            and i
            and i + 1 < len(text)
            and text[i - 1].isdigit()
            and text[i + 1].isdigit()
        ):
            continue
        if char in "：:" and _STAT_END.search(text[:i]):
            continue  # This colon belongs to the statistic, not a new subject.
        boundaries.append(i)
    return boundaries


def _clause_span(text: str, start: int, end: int, fields: set[str]) -> tuple[int, int]:
    boundaries = _clause_boundaries(text, fields)
    return (
        max((i + 1 for i in boundaries if i < start), default=0),
        min((i for i in boundaries if i >= end), default=len(text)),
    )


def _metadata_continuation(clause: str, field: str) -> bool:
    """Only explicit unit/type declarations about the same column keep scope.

    Free prose is not a scope declaration. This allows a named-column heading
    followed by its unit and type without carrying a subject through arbitrary
    narrative, unknown fields or transitions.
    """
    text = _normal(clause).strip()
    if _exact_unit_declaration(text, field):
        return True
    return (
        re.fullmatch(
            r"(?:画像(?:结果)?(?:也)?(?:印证|表明|显示|确认)(?:了)?(?:这一点)?[—:\s]*)?"
            r"该列(?:是|为)\s*(?:浮点型|整数型|浮点|整数|数值型|数值|float|integer)"
            r"(?:数值)?\s*(?:\(\s*metric\s*(?:角色)?\s*\))?",
            text,
            re.I,
        )
        is not None
    )


def profile_statement_context(statement: str, fields: set[str]) -> str:
    """Return one bounded field context from a complete field-description sentence."""
    text = _normal(statement).strip()
    text = re.sub(r"[。.!！?？]\s*$", "", text)
    header = parse_profile_stat_header(text, fields, context=text)
    header_text = text[slice(*header.span)] if header is not None else ""
    opening = header_text.rfind("(")
    population_header = bool(
        opening >= 0
        and header_text.endswith(")")
        and _profile_scope_metadata_valid(header_text[opening + 1 : -1], fields)
    )
    if (
        header is not None
        and population_header
        and re.search(
            _STAT_LABEL,
            text[header.separator_end :],
            re.I,
        )
    ):
        return header.field + "："
    label = re.fullmatch(r"(?P<subject>.+?)\s*(?P<tag>字段|列)\s*[:：]\s*(?P<metadata>.+)", text)
    if label is None:
        return ""
    mentioned = _subject_fields(label["subject"] + label["tag"], fields, context=text)
    if len(mentioned) != 1:
        return ""
    field = next(iter(mentioned))
    return field + "：" if _exact_unit_declaration(label["metadata"], field) else ""


def _complete_clause_subject(clause: str, fields: set[str], context: str) -> set[str] | None:
    """Recognize a complete statistic clause without discarding intervening prose.

    None means a boundary; an empty set is a complete subjectless statistic.
    Numeric truth and units are still checked independently for every token.
    """
    text = _normal(clause).strip()
    label = re.search(
        r"(?P<label>" + _STAT_LABEL + r")\s*(?:约|为|是|[:：=])*\s*(?=[-+\d.])", text, re.I
    )
    ending = _NUMBER + r"\s*(?:%|[千万亿]?元|小时|分钟|秒|天|条|个)?[)）]?"
    if label and re.fullmatch(_PREFIX["null_ratio"], label["label"], re.I):
        ending += r"(?:\s*\(约?\s*" + _NUMBER + r"\s*%\))?"
    if label and re.fullmatch(
        _STAT_LABEL + r"\s*(?:约|为|是|[:：=])*\s*" + ending,
        text[label.start() :],
        re.I,
    ):
        prefix = text[: label.start()].strip()
        if prefix in {"", "该列", "该列的"}:
            return set()
        return _subject_fields(prefix, fields, context) or None
    # Exact missing counts may introduce a same-column ratio aside.
    postfix = re.fullmatch(
        r"(?P<subject>.*?)\s*" + _NUMBER + r"\s*个?(?:缺失值|空值)"
        r"(?:\(\s*(?:空值率|缺失率|null_ratio)\s*(?:约|为|是|[:：=])*\s*"
        + _NUMBER
        + r"\s*%?\s*\))?",
        text,
        re.I,
    )
    if postfix:
        return _subject_fields(postfix["subject"], fields, context) or None
    return None


def _inherited_field(text: str, left: int, fields: set[str]) -> set[str]:
    """Carry one field only through uninterrupted complete statistic clauses."""
    population = stated_population(text, fields)
    subject_view = mask_population(text, population) if population and population.valid else text
    if len(_fields_in(subject_view, fields)) != 1:
        return set()
    boundaries = [-1, *[i for i in _clause_boundaries(text, fields) if i < left]]
    current: set[str] = set()
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        clause = text[a + 1 : b].strip()
        subject = _complete_clause_subject(clause, fields, text)
        if subject:
            current = subject if len(subject) == 1 else set()
        elif subject == set() and current:
            continue
        elif current and _metadata_continuation(clause, next(iter(current))):
            continue
        else:
            # Preserve the established explicit field/metadata heading contract.
            current = _subject_fields(clause, fields, text) if not current else set()
    return current


def _subject_fields(subject: str, fields: set[str], context: str) -> set[str]:
    """Resolve a complete subject phrase, including bounded profile asides.

    Unknown words before/after a known name are not discarded. The grammar
    admits the existing column role and quality asides, not arbitrary prose
    between a field mention and a statistic.
    """
    result: set[str] = set()
    text = _normal(subject).strip()
    header = parse_profile_stat_header(text, fields, context=context)
    if header is not None and re.fullmatch(r"\s*:?[\s]*", text[header.span[1] :]):
        return {header.field}

    tail = (
        r"(?:字段|列|的|(?:的)?(?:单位|画像|统计)|(?:为|是)(?:时间|维度|指标|标识|数值)列|"
        r"(?:存在|有)|(?:存在|有)\s*"
        + _NUMBER
        + r"\s*(?:个?(?:缺失值|空值)|条重复)\s*\(\s*(?:"
        + _NUMBER
        + r"\s*行中\s*)?)?"
    )
    for field in fields:
        for spelling in _field_spellings(field, context):
            pattern = (
                r"(?:另外|此外)?[\s(「『\"'`]*"
                + re.escape(spelling)
                + r"[」』\"'`]*\s*"
                + tail
                + r"\s*[(]?\s*"
            )
            if re.fullmatch(pattern, text):
                result.add(field)
    return result


def _dataset_subject(text: str, binding: JsonObject) -> bool:
    subject = _normal(text).strip().removesuffix("的").rstrip()
    if subject in {
        "",
        "全表",
        "整表",
        "全数据集",
        "整个数据集",
        "当前数据集",
        "本数据集",
        "该数据集",
    }:
        return True
    # A complete header may contain both typed fields, in either order.
    raw = text.strip().removesuffix("的").rstrip()
    declarations = identity_declarations(raw, headers_only=True)
    remaining = raw
    for declaration in reversed(declarations):
        remaining = remaining[: declaration.start()] + remaining[declaration.end() :]
    return bool(
        declarations
        and not remaining.strip()
        and all(identity_matches_binding(m, binding) for m in declarations)
    )


def _duplicate_scope_matches(
    text: str,
    left: int,
    subject: str,
    binding: JsonObject,
    fields: set[str],
    role: str,
) -> bool:
    # get_data_profile profiles one immutable dataset and accepts no filters.
    # A filtered population must have its own derived dataset identity; never
    # silently ignore a filter attached to a historical/foreign profile fact.
    if binding.get("filters"):
        return False
    # Every earlier comma clause must be a complete statistic or an explicit
    # dataset heading. Unknown scope prose is not equivalent to empty scope.
    names = "(?:" + "|".join(re.escape(f) for f in sorted(fields, key=len, reverse=True)) + ")"
    neutral_spans = (
        [
            match.span()
            for match in re.finditer(
                r"数据质量提示\s*[:：]\s*" + names + r"(?:\s*、\s*" + names + r")*\s*为常量列",
                text,
            )
        ]
        if fields
        else []
    )
    boundaries = [-1, *[i for i in _clause_boundaries(text, fields) if i < left]]
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        clause = text[a + 1 : b].strip()
        if any(start <= a + 1 and b <= end for start, end in neutral_spans):
            continue
        if re.fullmatch(r"[\s)\]）】」』]+", clause):
            continue  # Closing a prior grouped statement adds no population.
        if _dataset_subject(clause, binding):
            continue
        # Bounded informational headings from the established quality narrative
        # do not restrict a population. Every other preceding clause is still
        # checked, so a subset qualifier cannot hide behind this heading.
        if re.fullmatch(
            r"(?:顺带)?提示(?:[一二三四五六七八九十两]+个)?数据质量点|供后续分步处理时参考",
            clause,
        ):
            continue
        if _complete_clause_subject(clause, fields, text) is None:
            return False
    if role == "duplicate_excess_count":
        return bool(_subject_fields(subject, fields, text))
    if _dataset_subject(subject, binding):
        return not binding.get("filters")
    if role == "duplicate_ratio":
        aside = re.fullmatch(
            r"(.*?)整行重复多余记录数\s*(?:约|为|是|[:：=])*\s*"
            + _NUMBER
            + r"\s*(?:条|行|个)?\s*\(\s*",
            _normal(subject),
        )
        return bool(aside and _dataset_subject(aside[1], binding) and not binding.get("filters"))
    return False


def profile_assignment_role(prefix: str) -> str | None:
    """以完整赋值前缀确认画像事实类型，不能借嵌套行数改变外层类型。"""
    matches = [
        (role, match.start())
        for role, pattern in _PREFIX.items()
        if (match := re.search(r"(?:" + pattern + r")\s*(?:约|为|是|[:：=])*\s*$", prefix, re.I))
    ]
    if not matches:
        return None
    left = min(start for _, start in matches)
    roles = {role for role, start in matches if start == left}
    return next(iter(roles)) if len(roles) == 1 else None


def _role_aside_distinct_matches(
    binding: JsonObject,
    text: str,
    start: int,
    end: int,
    fields: set[str],
) -> bool:
    """绑定“字段（角色，N 个不同值）”中的受限 distinct 声明。"""
    if binding.get("statistic") != "distinct_count":
        return False
    normalized = _normal(text)
    for aside in re.finditer(r"\((?P<body>[^()]*)\)", normalized):
        body_start, body_end = aside.span("body")
        if not (body_start <= start and end <= body_end):
            continue
        prefix = normalized[body_start:start].strip()
        suffix = normalized[end:body_end].strip()
        if re.fullmatch(r"(?:时间|维度|指标|标识|数值型)(?:列|角色)?\s*[,，、]\s*", prefix) is None:
            continue
        if re.fullmatch(r"个不同值(?:\s*[:：][^()]*)?", suffix) is None:
            continue
        before = normalized[: aside.start()].rstrip()
        candidates = {
            field
            for field in fields
            for spelling in _field_spellings(field, text)
            if before.endswith(_normal(spelling))
            and (
                not (leading := before[: -len(_normal(spelling))]).strip()
                or re.search(
                    r"(?:(?:字段|列)(?:包括|含有|为)|[、,，;；:：])\s*$",
                    leading,
                )
            )
        }
        candidates = {
            field
            for field in candidates
            if not any(field != other and _normal(field) in _normal(other) for other in candidates)
        }
        return len(candidates) == 1 and binding.get("object", {}).get("column") in candidates
    return False


def profile_candidate_matches(
    binding: JsonObject,
    universe: list[JsonObject],
    text: str,
    start: int,
    end: int,
    evidence_value: Decimal,
) -> bool:
    """先定位字段和统计角色，再由通用数值匹配核对显示精度；不按同值选来源。"""
    if not binding.get("profile_fact"):
        return False
    fields = {
        str(b["object"]["column"])
        for b in universe
        if b.get("profile_fact") and b.get("object", {}).get("kind") == "profile_column"
    }
    left, right = _clause_span(text, start, end, fields)
    prefix, suffix = text[left:start], text[end:right]
    display = parse_numeric_display(text[start:end], suffix)
    displayed_unit = re.match(r"\s*(%|[千万亿]?元|小时|分钟|秒|天|人)", suffix)
    if binding["unit"] == "count" and (display.percent or displayed_unit):
        return False
    if binding["unit"] == "raw" and displayed_unit:
        unit = re.fullmatch(r".+\(([^()]+)\)", _normal(str(binding["metric"])))
        if unit is None or displayed_unit[1].lstrip("千万亿") != unit[1]:
            return False
    if _role_aside_distinct_matches(binding, text, start, end, fields):
        return True
    restatement = parse_ratio_restatement(text, left, right)
    restatement_owns_token = bool(restatement and restatement.owns(start, end))
    restatement_matches = bool(
        restatement
        and restatement_owns_token
        and binding["statistic"] == "null_ratio"
        and _ratio_restatement_matches_evidence(restatement, evidence_value)
    )
    # A recognized restatement is atomic: both displayed values must match this
    # exact Evidence value before either token may bind to it.
    if binding["unit"] == "ratio":
        if restatement_owns_token and not restatement_matches:
            return False
        if not restatement_owns_token and not display.ratio_complete:
            return False
    # A count of participating rows/groups requires its own tool fact. Neither
    # an excess count nor a same-valued distinct count is evidence for that role.
    if re.search(r"参与重复|重复组数|重复组|重复值种类", prefix + suffix):
        return False
    role: str | None = None
    role_start: int | None = None
    matches = [
        (candidate, match)
        for candidate, pattern in _PREFIX.items()
        if (match := re.search(r"(?:" + pattern + r")\s*(?:约|为|是|[:：=])*\s*$", prefix, re.I))
    ]
    if matches:
        earliest = min(match.start() for _, match in matches)
        longest = [(candidate, match) for candidate, match in matches if match.start() == earliest]
        if len(longest) != 1:
            return False
        role, selected = longest[0]
        role_start = selected.start()
    if role is None:
        if (
            restatement_matches
            and restatement is not None
            and start == restatement.percent_number_span[0]
            and end
            in {
                restatement.percent_number_span[1],
                restatement.percent_display_span[1],
            }
        ):
            role = "null_ratio"
            role_start = restatement.label_span[0] - left
    if role is None:
        postfix = [
            (r"\s*个?(?:不同取值|不同值|唯一值)", "distinct_count"),
            (r"\s*个?(?:缺失值|空值)", "null_count"),
            (r"\s*行", "row_count"),
            (r"\s*列", "column_count"),
        ]
        role = next(
            (candidate for pattern, candidate in postfix if re.match(pattern, suffix)), None
        )
        if role == "row_count" and (
            re.match(r"\s*行(?:疑似)?(?:完全|整行)?重复", suffix)
            or re.search(r"(?:重复记录|重复行|整行重复)\s*(?:有|为)?\s*$", prefix)
        ):
            return False  # Ambiguous duplicate counts are not dataset row counts.
    if role is None or role != binding["statistic"]:
        return False
    if role in {"non_null_count", "duplicate_excess_count", "duplicate_rows"}:
        count_end = r"\s*(?:条|行|个)?\s*"
        if role == "duplicate_rows":
            # A whole-dataset excess count may have its same-basis ratio aside.
            # Every ratio token still gets an independent typed Evidence check.
            count_end += (
                r"(?:[（(]\s*重复率\s*(?:约|为|是|[:：=])*\s*" + _NUMBER + r"\s*[%％]?\s*[)）])?"
            )
        if not re.fullmatch(count_end + r"\s*[)）]?\s*[。.!！?？;；]?\s*", suffix):
            return False  # Groups, physical units or unparsed qualifiers are not rows.
    subject = prefix[:role_start] if role_start is not None else prefix
    if role in {
        "duplicate_rows",
        "duplicate_ratio",
        "duplicate_excess_count",
    } and not _duplicate_scope_matches(text, left, subject, binding, fields, role):
        return False
    mentioned = _subject_fields(subject, fields, text)
    if role in {"row_count", "column_count"}:
        # Explicit 行/列 describe the dataset's dimensions even inside a column aside.
        return bool(binding["object"]["kind"] == "profile_dataset")
    if role in {"duplicate_rows", "duplicate_ratio"}:
        return not mentioned and binding["object"]["kind"] == "profile_dataset"
    if not mentioned and role in {
        "min",
        "max",
        "mean",
        "median",
        "std",
        "null_ratio",
        "non_null_count",
        "null_count",
        "distinct_count",
    }:
        # A shared field is carried only across an explicit statistic list in
        # this one statement. An unknown or competing subject fails closed.
        leading = prefix[:role_start].strip() if role_start is not None else None
        if leading in {"", "该列", "该列的"}:
            mentioned = _inherited_field(text, left, fields)
    return len(mentioned) == 1 and binding["object"].get("column") in mentioned


DUPLICATE_COUNT_INCOMPLETE = (
    "重复计数的口径或数值尚未通过核验，任务未完成。"
    "须依据对应证据修正；非必需附加结论可明确撤回，必交付内容仍须核验。"
    "参与重复的记录数和重复组数需先取得相应工具证据。"
)


def duplicate_count_feedback(
    claims: list[ClaimDraft], evidence: list[EvidenceRecord]
) -> str | None:
    """Explain existing tool bases without inventing counts or rewriting claims."""
    if not any(
        claim.claim_kind == "numeric"
        and "重复" in claim.statement
        and any(ref.get("supported") is not True for ref in claim.value_refs)
        for claim in claims
    ):
        return None
    statistics = {
        fact["statistic"]
        for record in evidence
        if record.kind == "tool_result" and record.source.get("tool") == "get_data_profile"
        for item in record.summary.get("value_index", [])
        if (fact := profile_fact(item)) is not None
    }
    instructions = []
    if "duplicate_rows" in statistics:
        instructions.append(
            "当前画像 quality.duplicate_rows 是整行相同的每组保留一条后多余的记录数；"
            "请明确写‘整行重复多余记录数为〈该工具值〉’。"
        )
    if "duplicate_excess_count" in statistics:
        instructions.append(
            "当前画像 non_unique_identifier 的 duplicate_values_at_least 在精确证据核验后"
            "表示该字段非空值每组保留一条后的多余记录数；"
            "请写‘〈完整字段名〉重复多余记录数为〈对应工具值〉’。"
        )
    if not instructions:
        return None
    return (
        "候选回答的重复计数口径或数值未通过核验。"
        + "".join(instructions)
        + "多余记录数、参与重复的全部记录数、重复值或重复行的组数是不同口径，"
        "即使数值相同也不能互换。"
        "当前这些画像事实不提供参与数和组数，不得据多余数推算它们，"
        "不得把相同数字当作不同口径的证据。"
        "仅引用同一数据集、同一版本、同一字段的实际工具值，不得沿用未经支持的候选数字，"
        "也不得自行相减。其他口径必须先由确定性工具计算；无对应工具时如实说明未计算。"
        "这是你自己的候选表述问题，不要要求用户替你选择口径；"
        "按现有工具已定义的事实明确表达并重新提交完整回答。"
        "只有用户真实业务目标仍缺少必要定义时才澄清，不要删除这项问题后声称核验完成。"
    )


def profile_heading_sections(text: str, fields: set[str]) -> list[tuple[str, str]]:
    """仅将明确字段标题绑定到紧接的完整统计段落，不跨引用、范围或段落。"""
    from apps.orchestrator.control.claim_structure import (
        delimited_spans,
        inline_numeric_text,
        mask_fenced_code,
    )

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    quoted = delimited_spans(mask_fenced_code(text))
    ancestors: list[tuple[int, bool]] = []
    fence: tuple[str, int] | None = None
    blockquote = False
    scope_boundary = False
    previous = ""
    spans: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        raw = line.rstrip("\r\n")
        stripped = raw.strip()
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", raw)
        if marker:
            if not fence:
                fence = (marker[1][0], len(marker[1]))
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and re.fullmatch(r" {0,3}" + re.escape(marker[1]) + r"\s*", raw)
            ):
                fence = None
            previous = stripped
            continue
        if fence:
            continue
        if raw.lstrip().startswith(">"):
            blockquote = True
        if not stripped:
            blockquote = False
            continue
        atx = re.fullmatch(r" {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*", raw)
        bold = re.fullmatch(r" {0,3}\*\*([^\n]+)\*\*\s*", raw)
        title = (atx[2] if atx else bold[1] if bold else "").strip()
        candidates = {f for f in fields if _normal(title) in {_normal(f), _normal(f) + "的统计"}}
        outside = not blockquote and not any(a < offsets[index] < b for a, b in quoted)
        if atx and outside:
            level = len(atx[1])
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            blocked = not candidates and title not in {"数据画像", "画像", "统计", "统计结果"}
            ancestors.append((level, blocked))
        if not title and re.search(
            r"(?:仅限|只分析|筛选后|过滤后|版本\s*[:：= ]|数据集\s+|"
            r"dataset_ref\s*[:=]|data_version_hash\s*[:=])",
            stripped,
            re.I,
        ):
            scope_boundary = True
        if (
            title
            and not candidates
            and re.fullmatch(r"(?:示例|例子|引用|转述|假设)(?:[:：].*)?", title)
        ):
            scope_boundary = True
        example = re.search(r"(?:例如|示例|举例|引用|转述|假设).*[:：]$", previous)
        if (
            len(candidates) != 1
            or not outside
            or scope_boundary
            or example
            or any(blocked for _, blocked in ancestors)
        ):
            previous = stripped
            continue
        field = next(iter(candidates))
        start = index + 1
        while start < len(lines) and not lines[start].strip():
            start += 1
        end = start
        while end < len(lines) and lines[end].strip():
            end += 1
        body = "".join(lines[start:end])
        if body and _heading_statistics_only(inline_numeric_text(body), field, fields):
            spans.append((offsets[start], offsets[end] if end < len(lines) else len(text), field))
        previous = stripped
    result: list[tuple[str, str]] = []
    cursor = 0
    for start, end, field in spans:
        if start < cursor:
            continue
        result.extend([(text[cursor:start], ""), (text[start:end], field + "：")])
        cursor = end
    result.append((text[cursor:], ""))
    return result


def _heading_statistics_only(text: str, field: str, fields: set[str]) -> bool:
    """标题继承只接受完整统计谓词和该字段的精确单位声明。"""
    normalized = _normal(text).strip()
    statements = [s.strip() for s in re.split(r"[。；;\n]", normalized) if s.strip()]
    has_statistic = False
    for statement in statements:
        if _exact_unit_declaration(statement, field):
            continue
        boundaries = [-1, *_clause_boundaries(statement, fields), len(statement)]
        for a, b in zip(boundaries, boundaries[1:], strict=False):
            clause = statement[a + 1 : b].strip()
            subject = _complete_clause_subject(clause, fields, statement)
            if subject is None or subject not in (set(), {field}):
                return False
            has_statistic = True
    return has_statistic
