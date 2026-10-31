"""Independent original-goal ownership tests; real local tools/SQLite, offline model."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from apps.orchestrator.agent_loop import _dataset_role_bindings
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import load_dataframe, save_dataframe
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.dataset_scope import operation_progress
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_r1_core_real_paths import real_host
from tests.test_r2_operation_completion import no_network  # noqa: F401
from tests.test_r2_scope_holdout import IndependentTrace

CAPTURE = json.loads((Path(__file__).parent / "fixtures/manual03_profile.json").read_text())
SPLIT_BODY = (
    "先筛选设备=A且状态=运行，再去掉停机分钟为空的行，"
    "然后去重，按月份汇总停机分钟并生成折线图。回答清洗后行数和停机分钟总计。"
)
FILTER = {
    "filters": [
        {"column": "设备", "op": "==", "value": "A"},
        {"column": "状态", "op": "==", "value": "运行"},
    ]
}


class DeviceTrace(IndependentTrace):
    def __init__(self, tmp_path, *, explicit_dedup=False):
        rows = [
            {"月份": f"2026-{m:02d}", "设备": device, "状态": "运行", "停机分钟": m * factor}
            for m in range(1, 7)
            for device, factor in [("A", 10), ("B", 20)]
        ]
        rows.extend(
            [dict(rows[0]), {"月份": "2026-02", "设备": "A", "状态": "运行", "停机分钟": None}]
        )
        self.source = self.current = save_dataframe(pd.DataFrame(rows))
        self.session = SessionStore(str(tmp_path / "independent-device.sqlite3"))
        self.project = self.session.create_project("independent split obligation")
        self.session.register_dataset(
            ref=self.source,
            project_id=self.project.id,
            filename="synthetic-device.csv",
            profile=infer_schema({"dataset_ref": self.source}).to_dict(),
        )
        self.conversation = self.session.create_conversation(self.project.id)
        body = SPLIT_BODY.replace("然后去重", "然后按整行去重") if explicit_dedup else SPLIT_BODY
        self.goal = f"数据集 {self.source}：" + body
        _, message = self.session.start_user_turn(
            conversation_id=self.conversation.id,
            content=self.goal,
            suggested_title="split obligation",
        )
        self.tasks = TaskStore(self.session.db_path)
        dataset = self.session.get_dataset(self.source)
        self.contract = build_minimal_contract(
            run_id="independent-device-obligation",
            user_text=self.goal,
            chart_required=True,
            report_required=False,
            pdf_required=False,
            columns=tuple(dataset.profile["columns"][i]["name"] for i in range(4)),
            dataset_refs=(self.source,),
            column_roles=_dataset_role_bindings(dataset),
        )
        self.run, _ = self.tasks.create_run(
            project_id=self.project.id,
            conversation_id=self.conversation.id,
            user_message_id=message.id,
            contract=self.contract,
            budget={"max_tool_calls": 8},
        )
        self.run, _ = self.tasks.transition(
            self.run.run_id,
            expected_version=self.run.state_version,
            status="running",
            event_type="run.started",
            payload={},
        )
        self.assistant = self.session.append_message(
            conversation_id=self.conversation.id, role="assistant", content="独立合成工具核验"
        )
        self.count, self.directory = 0, tmp_path


@pytest.mark.asyncio
async def test_original_manual03_description_reaches_profile_without_mutation_plan(tmp_path):
    result = await real_host(
        tmp_path,
        tool="get_data_profile",
        operation={},
        goal=CAPTURE["goal"],
        answer=CAPTURE["answer"],
    )
    assert result["witness"]["goal"] == CAPTURE["goal"]
    required = result["witness"]["contract"]["analysis_requirements"]
    assert not [op for op in required["operations"] if op["disposition"] == "current"]
    assert not required["clarifications"]
    assert all(i["tool_name"] != "transform_dataset" for i in result["witness"]["invocations"])
    profile = next(a for a in result["artifacts"] if a.source_tool == "get_data_profile")
    assert profile.payload["profile"]["row_count"] == 75
    # Exact captured answer still includes an unsupported duplicate-group claim.
    assert result["run"].status != "completed"


def test_explicit_dedup_is_not_erased_by_request_to_describe_its_removed_count(tmp_path):
    trace = IndependentTrace(tmp_path, "请按整行去重，说明删除的记录数。")
    required = trace.contract.analysis_requirements
    assert not required.needs_clarification(), required.to_dict()
    assert [op.kind for op in required.active_operations()] == ["drop_duplicates"]
    assert trace.admit({"drop_duplicates": []}) is None
    trace.execute({"drop_duplicates": []})
    assert len(load_dataframe(trace.current)) == 3
    calls, evidence, bindings = trace.state()
    assert operation_progress(required, trace.run.run_id, calls, evidence, bindings).complete


def test_original_split_request_preserves_the_null_cleanup_obligation(tmp_path):
    trace = DeviceTrace(tmp_path)
    required = trace.contract.analysis_requirements
    active = required.active_operations()
    assert [op.kind for op in active] == ["filter", "drop_nulls", "drop_duplicates", "aggregate"]
    assert active[0].parameters.filters[0].column == "设备"
    assert active[0].parameters.filters[1].column == "状态"
    assert active[1].parameters.columns == ("停机分钟",)
    assert required.needs_clarification()  # Original dedup scope is not stated.
    assert all(
        q.slot == "columns" and q.operation_id == active[2].operation_id
        for q in required.clarifications
    )


def test_post_cleanup_row_delivery_does_not_replace_original_monthly_sum(tmp_path):
    trace = DeviceTrace(tmp_path)
    required = trace.contract.analysis_requirements
    assert (required.group_col, required.value_col, required.aggregation) == (
        "月份",
        "停机分钟",
        "sum",
    )
    assert required.total and required.total_column == "停机分钟"
    assert not any(
        q.slot in {"aggregation", "group_col", "value_col"} for q in required.clarifications
    )


@pytest.mark.parametrize("omit_null_cleanup", [False, True])
def test_explicit_dedup_control_uses_real_lineage_and_cannot_skip_null_cleanup(
    tmp_path, omit_null_cleanup
):
    trace = DeviceTrace(tmp_path, explicit_dedup=True)
    required = trace.contract.analysis_requirements
    assert not required.needs_clarification(), required.to_dict()
    assert trace.admit(FILTER) is None
    trace.execute(FILTER)
    assert len(load_dataframe(trace.current)) == 8
    if not omit_null_cleanup:
        assert trace.admit({"drop_nulls": ["停机分钟"]}) is None
        trace.execute({"drop_nulls": ["停机分钟"]})
        assert len(load_dataframe(trace.current)) == 7
        assert trace.admit({"drop_duplicates": []}) is None
    else:
        assert trace.admit({"drop_duplicates": []}) is not None
    # A deliberate isolated bypass below tests proof reconstruction, not Host success.
    trace.execute({"drop_duplicates": []})
    assert len(load_dataframe(trace.current)) == (7 if omit_null_cleanup else 6)
    aggregate = trace.execute(
        {"group_col": "月份", "value_col": "停机分钟", "agg": "sum"}, tool="aggregate_preview"
    )
    assert aggregate["total_value"] == 210  # Same total cannot hide the missing cleanup.
    assert [row["value"] for row in sorted(aggregate["rows"], key=lambda row: row["group"])] == [
        10,
        20,
        30,
        40,
        50,
        60,
    ]
    calls, evidence, bindings = trace.state()
    progress = operation_progress(required, trace.run.run_id, calls, evidence, bindings)
    assert progress.complete is (not omit_null_cleanup), progress
    result = trace.verify("停机分钟总计210。")
    trace.witness("split-obligation-proof", result)
    if omit_null_cleanup:
        assert any(i.code == "required_operation_not_satisfied" for i in result.issues)
    # The original goal still requires a chart; this local tool proof does not
    # pretend the absent chart or full original Host task has been delivered.
    assert not result.passed
