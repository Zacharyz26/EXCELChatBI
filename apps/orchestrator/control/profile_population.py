"""画像声明的总体约束：Host 血缘证明与文本范围独立于数值相等。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from packages.orchestration.analysis_requirements import explicit_analysis_requirements
from packages.session.models import JsonObject

from apps.orchestrator.control.operation_intent import (
    OperationMention,
    operation_mentions,
)

if TYPE_CHECKING:
    from packages.session.store import SessionStore

_SCOPE = re.compile(r"筛选(?:条件)?|过滤(?:条件)?|仅限|只看|限定(?:于)?")
_WHOLE = re.compile(r"(?:恢复|回到|改为|使用)?(?:原始全表|未筛选全表|全表)")


def profile_population_arguments(
    store: SessionStore, arguments: JsonObject, version: str | None = None
) -> JsonObject:
    """只从当前 Host 登记血缘生成快照；不读取模型声称的过滤证明。"""
    args = {k: v for k, v in arguments.items() if k != "profile_population"}
    ref = str(args.get("dataset_ref", ""))
    current = store.get_dataset(ref)
    if current is None:
        return args
    project = current.project_id
    steps: list[JsonObject] = []
    seen: set[str] = set()
    while current.parent_ref is not None:
        if current.ref in seen or len(seen) >= 64:
            return args
        seen.add(current.ref)
        parents = store.dataset_parent_refs(current.ref)
        if parents != (current.parent_ref,) or not current.transform:
            return args
        parent = store.get_dataset(current.parent_ref)
        if parent is None or parent.project_id != project:
            return args
        operations = current.transform
        if not set(operations) <= {
            "filters",
            "drop_nulls",
            "drop_duplicates",
            "sort",
            "exclude_row_indices",
        }:
            return args
        steps.append(
            {"dataset_ref": current.ref, "parent_ref": parent.ref, "transform": operations}
        )
        current = parent
    if version is not None:
        args["data_version_hash"] = version
    args["profile_population"] = {
        "schema": "host-profile-population-v1",
        "dataset_ref": ref,
        "dataset_version": args.get("data_version_hash", ref),
        "root_ref": current.ref,
        "steps": list(reversed(steps)),
    }
    return args


def population_filters(binding: JsonObject) -> list[JsonObject] | None:
    """验证快照的版本和连续边，派生范围不从裸 filters 参数推断。"""
    proof = binding.get("population")
    if proof is None:
        return [] if not binding.get("filters") else None
    if (
        not isinstance(proof, dict)
        or proof.get("schema") != "host-profile-population-v1"
        or proof.get("dataset_ref") != binding.get("dataset_ref")
        or proof.get("dataset_version") != binding.get("dataset_version")
        or not isinstance(proof.get("steps"), list)
    ):
        return None
    current = proof.get("root_ref")
    filters: list[JsonObject] = []
    if not isinstance(current, str) or not current:
        return None
    seen = {current}
    for step in proof["steps"]:
        if not isinstance(step, dict) or step.get("parent_ref") != current:
            return None
        current = step.get("dataset_ref")
        if not isinstance(current, str) or current in seen:
            return None
        seen.add(current)
        operations = step.get("transform")
        if not isinstance(operations, dict) or not set(operations) <= {
            "filters",
            "drop_nulls",
            "drop_duplicates",
            "sort",
            "exclude_row_indices",
        }:
            return None
        raw_filters = operations.get("filters", [])
        if not isinstance(raw_filters, list):
            return None
        for item in raw_filters:
            if not isinstance(item, dict) or set(item) != {"column", "op", "value"}:
                return None
            if item not in filters:
                filters.append(item)
    return filters if current == binding.get("dataset_ref") else None


@dataclass(frozen=True)
class PopulationRequest:
    """一段明确范围声明及其有界原文位置；无法解释仍保留拒绝义务。"""

    filters: tuple[JsonObject, ...]
    valid: bool
    span: tuple[int, int]


def _explicit_population_request(text: str, fields: set[str]) -> PopulationRequest | None:
    """识别已有 AND 比较式或字段紧邻值的筛选说明，不猜 OR/未知条件。"""
    names = "|".join(re.escape(f) for f in sorted(fields, key=len, reverse=True))
    markers = list(_SCOPE.finditer(text))
    if names:
        markers.extend(re.finditer(r"仅(?=\s*(?:" + names + r"))", text))
    markers.sort(key=lambda m: m.start())
    whole = list(_WHOLE.finditer(text))
    if not markers:
        return PopulationRequest((), True, whole[-1].span()) if whole else None
    marker = markers[-1]
    start = marker.start()
    position = marker.end()
    opening = re.match(r"\s*(?:为|是|[:：])?\s*", text[position:])
    assert opening is not None
    position += opening.end()
    # Consume complete conditions. A comma/semicolon/newline may join another
    # condition, so it cannot itself terminate the population. Unknown fields
    # or connectors retain an invalid obligation rather than shrinking scope.
    condition = re.compile(
        r"[\"'`“]?(?P<column>" + (names or r"(?!)") + r")[\"'`”]?\s*"
        r"(?P<operator>==|>=|<=|!=|=|>|<|为|是)?\s*"
        r"(?P<value>\"[^\"]*\"|'[^']*'|“[^”]*”|[^\s，,、；;：:。!?！？=<>!]+?)"
        r"(?=后(?:的)?|并且|而且|且|\bAND\b|&&|[\s，,、；;：:。!?！？]|$)",
        re.I,
    )
    filters: list[JsonObject] = []
    valid = bool(names)
    end = position
    while valid:
        match = condition.match(text, position)
        if match is None:
            valid = False
            break
        raw = match["value"]
        if re.search(r"\bOR\b|或者|或|\|\|", raw, re.I):
            valid = False
            break
        operator = match["operator"] or "="
        if operator in {"为", "是"}:
            operator = "="
        # Reuse the request grammar's literal conversion and operator spelling;
        # a neutral key avoids imposing its bare-field spelling on real labels.
        parsed = explicit_analysis_requirements("筛选 scopefield" + operator + raw)
        if parsed.filter_error or len(parsed.filters) != 1:
            valid = False
            break
        item = {**parsed.filters[0], "column": match["column"]}
        if item not in filters:
            filters.append(item)
        position = end = match.end()
        tail = text[position:]
        after = re.match(r"\s*后(?:的)?", tail)
        if after:
            end += after.end()
            break
        separator = re.match(r"\s*(?:[，,、；;：:]\s*)?(?:(?:并且|而且|且|AND|&&)\s*)?", tail, re.I)
        assert separator is not None
        next_position = position + separator.end()
        following = text[next_position:]
        # An explicit connector always demands another full condition.
        connector = bool(re.search(r"并且|而且|且|\bAND\b|&&", separator[0], re.I))
        next_condition = condition.match(text, next_position)
        has_condition = next_condition is not None and next_condition["operator"] is not None
        unknown_condition = re.match(r"[^，,；;。!?！？\n]+?[=<>!]", following)
        if connector or has_condition or unknown_condition:
            if next_position == position:
                valid = False
                break
            position = next_position
            continue
        if re.match(r"\s*(?:OR\b|或|\|\|)", tail, re.I):
            valid = False
        break
    return PopulationRequest(tuple(filters), valid and bool(filters), (start, end))


def _last_filter_mention(text: str) -> OperationMention | None:
    mentions = operation_mentions(text, actions=("筛选", "过滤"))
    return mentions[-1] if mentions else None


def _whole_population_wins(whole: re.Match[str] | None, mention: OperationMention | None) -> bool:
    """后置或完整包住动作词的全表声明优先；较早全表不覆盖后续筛选。"""
    if whole is None or mention is None:
        return whole is not None
    return whole.start() > mention.span[0] or (
        whole.start() <= mention.span[0] and mention.span[1] <= whole.end()
    )


def requested_population(text: str, fields: set[str]) -> PopulationRequest | None:
    """解析用户本轮要求的范围；禁止/暂缓筛选不覆盖当前 Host 范围。

    明确的肯定筛选仍沿用严格语法并失败关闭。显式全表要求仍要求空过滤
    血缘；只有“本轮不新增筛选”返回 ``None``，让当前 Evidence 自证范围。
    """

    parsed = _explicit_population_request(text, fields)
    mention = _last_filter_mention(text)
    whole = list(_WHOLE.finditer(text))
    latest_whole = whole[-1] if whole else None
    if _whole_population_wins(latest_whole, mention):
        assert latest_whole is not None
        return PopulationRequest((), True, latest_whole.span())
    if mention is None:
        return parsed
    if mention.disposition != "current":
        return None
    return parsed


def stated_population(text: str, fields: set[str]) -> PopulationRequest | None:
    """解析回答对事实范围的声明，并与请求意图保持分离。

    “未筛选”是可核验的空过滤事实；“不要筛选”只是命令，不作为回答
    的范围证明。肯定筛选与全表声明继续使用相同严格语法。
    """

    parsed = _explicit_population_request(text, fields)
    mention = _last_filter_mention(text)
    whole = list(_WHOLE.finditer(text))
    latest_whole = whole[-1] if whole else None
    if _whole_population_wins(latest_whole, mention):
        assert latest_whole is not None
        return PopulationRequest((), True, latest_whole.span())
    if mention is None:
        return parsed
    if mention.disposition == "absent":
        return PopulationRequest((), True, mention.context_span)
    if mention.disposition in {"prohibited", "deferred", "described"}:
        return None
    return parsed


def population_request(text: str, fields: set[str]) -> PopulationRequest | None:
    """向后兼容：回答文本范围解析使用事实声明语义。"""

    return stated_population(text, fields)


def population_matches(binding: JsonObject, request: PopulationRequest | None) -> bool:
    """所有画像统计共用范围门禁；数值相同不放宽条件或版本。"""
    actual = population_filters(binding)
    if actual is None:
        return False
    if request is None:
        return True
    return (
        request.valid
        and len(actual) == len(request.filters)
        and all(item in actual for item in request.filters)
    )


def mask_population(text: str, request: PopulationRequest | None) -> str:
    """只在匹配证据范围后使用的等长语义视图，原 Claim 文本不改写。"""
    if request is None:
        return text
    left, right = request.span
    return text[:left] + " " * (right - left) + text[right:]
