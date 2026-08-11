"""Deterministic data-role preconditions for governed analysis tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from mcp_servers.excel_parser.advisor import DataRole, infer_data_roles_from_mapping
from packages.session.models import Dataset, JsonObject
from packages.session.task_models import DataRoleConfirmation

RoleRequirement = tuple[str, str, frozenset[DataRole]]
GuardCode = Literal[
    "data_role_preconditions_satisfied",
    "data_role_dataset_required",
    "data_role_profile_unavailable",
    "data_role_column_missing",
    "data_role_confirmation_required",
    "data_role_mismatch",
]


@dataclass(frozen=True, slots=True)
class DataRoleGuardResult:
    """A stable, auditable decision made before a tool Invocation is reserved."""

    allowed: bool
    code: GuardCode
    message: str
    checks: tuple[JsonObject, ...] = ()

    def evidence(self) -> JsonObject:
        return {
            "schema": "chatbi-data-role-preconditions-v1",
            "allowed": self.allowed,
            "code": self.code,
            "checks": [dict(item) for item in self.checks],
        }


def tool_role_requirements(
    tool_name: str,
    arguments: JsonObject,
) -> tuple[RoleRequirement, ...]:
    """Map current stats/aggregate arguments to roles; reusable by future Join."""
    requirements: list[RoleRequirement] = []

    def scalar(argument: str, *roles: DataRole) -> None:
        value = arguments.get(argument)
        if isinstance(value, str) and value.strip():
            requirements.append((argument, value.strip(), frozenset(roles)))

    def sequence(argument: str, *roles: DataRole) -> None:
        value = arguments.get(argument)
        if not isinstance(value, list):
            return
        for item in value:
            if isinstance(item, str) and item.strip():
                requirements.append((argument, item.strip(), frozenset(roles)))

    if tool_name == "trend_analysis":
        scalar("time_col", "time")
        scalar("value_col", "metric")
    elif tool_name == "anomaly_detect":
        scalar("value_col", "metric")
        scalar("time_col", "time")
    elif tool_name == "regression":
        scalar("target", "metric")
        sequence("features", "metric")
    elif tool_name == "correlation":
        sequence("columns", "metric")
    elif tool_name == "aggregate_preview":
        scalar("group_col", "dimension", "time")
        if arguments.get("agg") != "count":
            scalar("value_col", "metric")
    return tuple(requirements)


def validate_data_role_preconditions(
    *,
    tool_name: str,
    arguments: JsonObject,
    dataset: Dataset | None,
    confirmations: tuple[DataRoleConfirmation, ...],
    data_version_hash: str,
) -> DataRoleGuardResult | None:
    """Validate supplied role-bearing columns without reading raw dataset rows."""
    requirements = tool_role_requirements(tool_name, arguments)
    if not requirements:
        return None
    return validate_role_requirements(
        requirements=requirements,
        dataset=dataset,
        confirmations=confirmations,
        data_version_hash=data_version_hash,
    )


def validate_role_requirements(
    *,
    requirements: tuple[RoleRequirement, ...],
    dataset: Dataset | None,
    confirmations: tuple[DataRoleConfirmation, ...],
    data_version_hash: str,
) -> DataRoleGuardResult:
    """Validate an explicit role contract, including future Join key contracts."""
    if dataset is None:
        return DataRoleGuardResult(
            allowed=False,
            code="data_role_dataset_required",
            message="数据角色前置检查无法找到当前 TaskRun 中的数据集。",
        )
    try:
        roles = infer_data_roles_from_mapping(dataset.profile, dataset_ref=dataset.ref)
    except ValueError as exc:
        return DataRoleGuardResult(
            allowed=False,
            code="data_role_profile_unavailable",
            message=f"数据画像不足以执行角色前置检查：{exc}",
        )
    raw_items = roles.get("columns")
    items = cast(list[JsonObject], raw_items) if isinstance(raw_items, list) else []
    by_column = {
        str(item.get("column")): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("column"), str)
    }
    confirmed = {
        (item.dataset_ref, item.column): item
        for item in confirmations
        if item.data_version_hash == data_version_hash
    }
    checks: list[JsonObject] = []
    for argument, column, allowed_roles in requirements:
        role_item = by_column.get(column)
        if role_item is None:
            return DataRoleGuardResult(
                allowed=False,
                code="data_role_column_missing",
                message=f"字段“{column}”不在数据集画像中，无法执行角色前置检查。",
                checks=tuple(checks),
            )
        confirmation = confirmed.get((dataset.ref, column))
        effective_role: str
        if confirmation is not None:
            effective_role = confirmation.role
            source = "user_confirmation"
            confirmation_id: str | None = confirmation.confirmation_id
        else:
            effective_role = str(role_item.get("primary_role", "unknown"))
            source = "deterministic_profile"
            confirmation_id = None
        check: JsonObject = {
            "argument": argument,
            "column": column,
            "allowed_roles": sorted(allowed_roles),
            "effective_role": effective_role,
            "source": source,
            "ambiguous": bool(role_item.get("ambiguous")),
        }
        if confirmation_id is not None:
            check["confirmation_id"] = confirmation_id
        checks.append(check)
        if confirmation is None and bool(role_item.get("ambiguous")):
            return DataRoleGuardResult(
                allowed=False,
                code="data_role_confirmation_required",
                message=(
                    f"字段“{column}”的数据角色存在歧义；请先完成结构化角色确认，"
                    "再执行分析。"
                ),
                checks=tuple(checks),
            )
        if effective_role not in allowed_roles:
            expected = "、".join(sorted(allowed_roles))
            return DataRoleGuardResult(
                allowed=False,
                code="data_role_mismatch",
                message=(
                    f"字段“{column}”的确认/推断角色为 {effective_role}，"
                    f"但参数 {argument} 需要 {expected}。"
                ),
                checks=tuple(checks),
            )
    return DataRoleGuardResult(
        allowed=True,
        code="data_role_preconditions_satisfied",
        message="数据角色前置检查通过。",
        checks=tuple(checks),
    )
