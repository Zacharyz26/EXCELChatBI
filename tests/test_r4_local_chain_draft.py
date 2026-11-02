"""NOT_RUN: independent R4 same-conversation local chain, pending root review.

Only the gateway choices are scripted. Upload, Host, tools, Evidence, Verifier,
SQLite, chart renderer, PDF producer and download must be actual product paths.
This is not a real model or a frontend/browser end-to-end acceptance.

Run alongside test_r4_context_contract.py in this directory after R3 checkpoint
and R4 authorization. Root owns execution and environment approval. No fixture
in this file fabricates a successful invocation, Evidence, artifact or status.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

# Deliberate sibling import: this draft stays outside the repository until
# reviewed. This helper uses exact matching run_id in both Host and registry.
from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
METRIC = "播出时长（分钟）"
MONTH = "月份"


def independent_oracle():
    raw = (REPO / "examples/handoff/broadcast.csv").read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    columns = tuple(reader.fieldnames or ())
    original = list(reader)
    channel = [r for r in original if r["频道"] == "A"]
    both = [r for r in channel if r["状态"] == "已播出"]
    nonnull = [r for r in both if r[METRIC].strip()]
    seen = set()
    unique = []
    for row in nonnull:
        key = tuple(row[column] for column in columns)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    monthly = {}
    for row in unique:
        monthly[row[MONTH]] = monthly.get(row[MONTH], Decimal(0)) + Decimal(row[METRIC])
    reference = json.loads((REPO / "examples/handoff/oracle.json").read_text())["broadcast"]
    calculated = {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "rows": [len(original), len(channel), len(both), len(nonnull), len(unique)],
        "monthly": dict(sorted(monthly.items())),
        "total": sum(monthly.values(), Decimal(0)),
    }
    assert calculated["input_sha256"] == reference["sha256"]
    assert calculated["rows"] == [75, 38, 38, 37, 36]
    assert list(calculated["monthly"].values()) == [Decimal(x) for x in reference["monthly"]]
    assert calculated["total"] == Decimal(reference["total"])
    return raw, columns, [original, channel, both, nonnull, unique], calculated


def plain(value):
    return (
        format(Decimal(str(value)), "f").rstrip("0").rstrip(".")
        if "." in str(value)
        else str(value)
    )


async def api_request(store, method, path, **kwargs):
    # ASGITransport invokes the actual API/dependency handlers but deliberately
    # does not enter lifespan. Starting/stopping TestClient during a live Host
    # would call recover_stale_runs(0) at shutdown and corrupt this scenario.
    transport = httpx.ASGITransport(app=support.application(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://local.test") as client:
        return await client.request(method, path, **kwargs)


async def conversation_detail(store, conversation):
    response = await api_request(store, "GET", f"/conversations/{conversation.id}")
    assert response.status_code == 200, response.text
    return response.json()


async def current_projection(store, conversation):
    detail = await conversation_detail(store, conversation)
    projection = support.execution_projection(detail)
    assert projection.get("availability") == "available", projection
    assert isinstance(projection.get("dataset_ref"), str), projection
    return projection


def artifact_id(artifact):
    return (
        (artifact.params or {}).get("analysis_id")
        or (artifact.payload or {}).get("analysis_id")
        or artifact.id
    )


class ContextDrivenModel:
    """No expected row count, child ref or independent oracle enters this model.

    The only dataset argument is read from the product's common R4 projection,
    at the time the Host asks for a choice. A broken coref/Host contract cannot
    be concealed: the test separately checks contract, args, bindings and lineage.
    Final numbers are copied from that turn's actual tool message, not oracle.
    """

    def __init__(self, store, conversation, tool, parameters, answer_kind):
        self.store = store
        self.conversation = conversation
        self.tool = tool
        self.parameters = parameters
        self.answer_kind = answer_kind
        self.calls = 0
        self.messages = []
        self.projection = None
        self.arguments = None
        self.tool_result = None
        self.answer = None
        self.diagnostic = None

    async def stream_turn(self, _scenario, messages, **_kwargs):
        self.calls += 1
        self.messages.append([asdict(item) for item in messages])
        if self.calls == 1:
            # Actual no-lifespan API read, no expected child or fixture dict.
            self.projection = await current_projection(self.store, self.conversation)
            args = dict(self.parameters)
            if self.tool == "generate_report":
                permitted = set(self.projection.get("analysis_ids", []))
                selected = [
                    artifact_id(a)
                    for a in self.store.list_artifacts(self.conversation.id)
                    if a.source_tool in {"aggregate_preview", "gen_chart"}
                    and a.dataset_ref == self.projection["dataset_ref"]
                    and artifact_id(a) in permitted
                ]
                assert len(selected) == 2, {"projection": self.projection, "selected": selected}
                args["analysis_ids"] = selected
            else:
                args["dataset_ref"] = self.projection["dataset_ref"]
            self.arguments = args
            yield ModelResponse(
                content="",
                model="offline-r4-local-chain",
                tool_calls=[
                    ToolCall("r4-one-real-tool", self.tool, json.dumps(args, ensure_ascii=False))
                ],
            )
            return
        if self.calls != 2:
            raise ModelBudgetExceeded("Local deterministic scenario stopped; no provider/retry")
        message = next(m for m in reversed(messages) if m.role == "tool")
        try:
            self.tool_result = json.loads(message.content)
        except json.JSONDecodeError as error:
            self.diagnostic = {
                "kind": "actual_tool_message_truncated_or_invalid_json",
                "raw_tool_message": message.content,
                "error": str(error),
                "explicit_truncation_notice": "结果已截断" in message.content,
                "policy": "stop; no oracle answer or persisted-result substitution",
            }
            raise ModelBudgetExceeded(
                "Offline fixture cannot read actual tool message; stopped without retry"
            ) from error
        result = self.tool_result
        if self.answer_kind == "profile":
            profile = result["profile"]
            column = next(c for c in profile["columns"] if c["name"] == METRIC)
            self.answer = (
                f"当前数据共有{profile['row_count']}行。{METRIC}最大值为{plain(column['max'])}。"
            )
        elif self.answer_kind in {"filter", "drop_nulls", "drop_duplicates"}:
            name = {"filter": "筛选", "drop_nulls": "清洗", "drop_duplicates": "去重"}[
                self.answer_kind
            ]
            self.answer = f"{name}前{result['rows_before']}行，{name}后{result['rows_after']}行。"
            if self.answer_kind == "drop_duplicates":
                self.answer += f"去重删除了{result['row_changes']['removed_count']}行多余记录。"
        elif self.answer_kind == "aggregate":
            monthly = "；".join(f"{row['group']}为{plain(row['value'])}" for row in result["rows"])
            self.answer = (
                f"{METRIC}各月合计：{monthly}。{METRIC}总计为{plain(result['total_value'])}。"
            )
        elif self.answer_kind == "chart":
            self.answer = "已生成当前数据的月度播出时长折线图。"
        else:
            self.answer = "已生成所选月度汇总表和折线图的 PDF 报告。"
        yield ModelResponse(content=self.answer, model="offline-r4-local-chain")


def assert_frame(ref, expected_rows, columns):
    frame = load_dataframe(ref)
    assert tuple(frame.columns) == columns
    assert len(frame) == len(expected_rows)
    # Numeric input is independently interpreted from CSV; no product reducer
    # or binding helper provides the expected value.
    actual = []
    for row in frame.to_dict("records"):
        actual.append(
            tuple(
                None
                if column == METRIC and row[column] != row[column]
                else Decimal(str(row[column]))
                if column == METRIC
                else str(row[column])
                for column in columns
            )
        )
    expected = [
        tuple(
            Decimal(row[column])
            if column == METRIC and row[column].strip()
            else None
            if column == METRIC
            else row[column]
            for column in columns
        )
        for row in expected_rows
    ]
    assert actual == expected


def save_turn(tmp_path, label, result, model):
    support.write_witness(
        tmp_path,
        label,
        result=result,
        projection_seen=model.projection,
        emitted_arguments=model.arguments,
        actual_tool_result=model.tool_result,
        final_answer=model.answer,
        model_messages=model.messages,
        model_diagnostic=model.diagnostic,
        layer="real local Host/tools/SQLite; deterministic gateway; NOT real model/browser",
    )


async def turn(tmp_path, label, store, project, conversation, tool, parameters, kind, goal):
    model = ContextDrivenModel(store, conversation, tool, parameters, kind)
    try:
        result = await support.execute(store, project, conversation, model, goal)
    except Exception as error:
        latest = TaskStore(store.db_path).get_latest_run_for_conversation(conversation.id)
        support.write_witness(
            tmp_path,
            label + "-execution-exception",
            goal=goal,
            error=f"{type(error).__name__}: {error}",
            latest_actual_run=asdict(latest) if latest else None,
            emitted_arguments=model.arguments,
            model_messages=model.messages,
            model_diagnostic=model.diagnostic,
        )
        raise
    save_turn(tmp_path, label, result, model)  # before every assertion/failure
    assert result["run"]["status"] == "completed", result
    actual = [item for item in result["invocations"] if item["status"] == "succeeded"]
    assert len(actual) == 1 and actual[0]["tool_name"] == tool, result
    assert actual[0]["args"] == model.arguments, result
    assert (
        result["contract"]["analysis_requirements"]["dataset_ref"]
        == model.projection["dataset_ref"]
    ), result
    return result, model


@pytest.mark.asyncio
async def test_r4_same_conversation_actual_local_chain(tmp_path):
    raw, columns, expected_frames, oracle = independent_oracle()
    support.write_witness(tmp_path, "00-independent-oracle", oracle=oracle)
    store = SessionStore(str(tmp_path / "full-chain.sqlite3"))
    project = store.create_project("R4 local synthetic chain")
    conversation = store.create_conversation(project.id)
    uploaded = await api_request(
        store,
        "POST",
        "/upload/excel",
        data={"project_id": project.id, "conversation_id": conversation.id},
        files={"file": ("broadcast.csv", raw, "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    original = uploaded.json()["dataset_ref"]
    assert_frame(original, expected_frames[0], columns)
    refs = [original]
    frozen_run_hashes = {}

    profile, _ = await turn(
        tmp_path,
        "01-profile",
        store,
        project,
        conversation,
        "get_data_profile",
        {},
        "profile",
        "请先为当前数据生成画像，说明记录规模和播出时长最大值；暂不筛选或清洗。",
    )
    frozen_run_hashes[profile["run"]["run_id"]] = profile["data_version_hash"]
    transforms = [
        (
            "02-channel",
            {"filters": [{"column": "频道", "op": "==", "value": "A"}]},
            "filter",
            "现在筛选频道=A，说明筛选前后行数。",
        ),
        (
            "03-status",
            {"filters": [{"column": "状态", "op": "==", "value": "已播出"}]},
            "filter",
            "在当前结果中继续筛选状态=已播出，说明筛选前后行数。",
        ),
        (
            "04-null",
            {"drop_nulls": [METRIC]},
            "drop_nulls",
            "现在删除播出时长（分钟）为空的行，说明清洗前后行数。",
        ),
        (
            "05-deduplicate",
            {"drop_duplicates": []},
            "drop_duplicates",
            "现在按整行口径去重，说明去重前后行数及删除的多余记录数。",
        ),
    ]
    for frame_index, (label, parameters, kind, goal) in enumerate(transforms, 1):
        result, model = await turn(
            tmp_path,
            label,
            store,
            project,
            conversation,
            "transform_dataset",
            parameters,
            kind,
            goal,
        )
        tool_result = model.tool_result
        assert tool_result["parent_ref"] == refs[-1]
        assert tool_result["rows_before"] == len(expected_frames[frame_index - 1])
        assert tool_result["rows_after"] == len(expected_frames[frame_index])
        assert tool_result["transform"] == parameters
        child = tool_result["dataset_ref"]
        assert child != refs[-1] and store.get_dataset(child).parent_ref == refs[-1]
        evidence = [e for e in result["evidence"] if e["source"].get("tool") == "transform_dataset"]
        assert len(evidence) == 1
        proof = evidence[0]["summary"]["fact_projection"]
        assert proof["input_ref"] == refs[-1] and proof["output_ref"] == child
        assert proof["arguments"] == model.arguments
        assert_frame(child, expected_frames[frame_index], columns)
        refs.append(child)
        frozen_run_hashes[result["run"]["run_id"]] = result["data_version_hash"]
    final_ref = refs[-1]

    aggregate, aggregate_model = await turn(
        tmp_path,
        "06-aggregate",
        store,
        project,
        conversation,
        "aggregate_preview",
        {"group_col": MONTH, "value_col": METRIC, "agg": "sum", "sort": "group"},
        "aggregate",
        "按月份汇总当前结果的播出时长（分钟），给出各月合计和总计。",
    )
    actual_monthly = {
        r["group"]: Decimal(str(r["value"])) for r in aggregate_model.tool_result["rows"]
    }
    assert actual_monthly == oracle["monthly"]
    assert Decimal(str(aggregate_model.tool_result["total_value"])) == oracle["total"]
    assert plain(oracle["total"]) in aggregate_model.answer and "总计" in aggregate_model.answer
    assert aggregate_model.tool_result["group_total"] == len(oracle["monthly"])
    assert aggregate_model.tool_result["truncated"] is False
    independent_month_counts = {
        month: sum(row[MONTH] == month for row in expected_frames[-1])
        for month in oracle["monthly"]
    }
    for row in aggregate_model.tool_result["rows"]:
        assert (
            row["row_count"] == row["valid_value_count"] == independent_month_counts[row["group"]]
        )
    assert aggregate_model.arguments["dataset_ref"] == final_ref
    assert any(
        any(v.get("path") == "$.total_value" for v in e["summary"].get("value_index", []))
        for e in aggregate["evidence"]
    )

    chart, chart_model = await turn(
        tmp_path,
        "07-chart",
        store,
        project,
        conversation,
        "gen_chart",
        {"chart_type": "line", "encoding": {"x": MONTH, "y": METRIC, "agg": "sum"}},
        "chart",
        "请为当前结果生成按月播出时长折线图。",
    )
    option = chart_model.tool_result["option"]
    assert chart_model.arguments["dataset_ref"] == final_ref
    assert option["xAxis"]["type"] == "category"
    assert option["xAxis"]["data"] == list(oracle["monthly"])
    assert option["yAxis"]["type"] == "value"
    assert len(option["series"]) == 1 and option["series"][0]["type"] == "line"
    assert [Decimal(str(v)) for v in option["series"][0]["data"]] == list(
        oracle["monthly"].values()
    )
    assert option["series"][0]["name"] == METRIC and METRIC in option["title"]["text"]
    support.write_witness(
        tmp_path,
        "07-axis-display-limitation",
        y_axis_name=option["yAxis"].get("name"),
        current_unit_sources=["series.name", "title.text"],
        frontend_visual_review="NOT_RUN",
    )

    prior_artifacts = {a.id for a in store.list_artifacts(conversation.id)}
    report, report_model = await turn(
        tmp_path,
        "08-report",
        store,
        project,
        conversation,
        "generate_report",
        {"title": "播出时长月度分析", "include_pdf": True},
        "report",
        "用当前已生成的月度汇总表和折线图生成新的 PDF 报告，保留月度数据、总计及单位。",
    )
    payload = report_model.tool_result
    assert payload["skipped_charts"] == 0
    assert payload["analysis_ids"] == report_model.arguments["analysis_ids"]
    artifact = next(
        a
        for a in store.list_artifacts(conversation.id)
        if a.id not in prior_artifacts and a.source_tool == "generate_report"
    )
    assert artifact.payload["report_id"] == payload["report_id"]
    root = Path(get_settings().report_dir)
    pdf = root / f"{payload['report_id']}.pdf"
    md = root / f"{payload['report_id']}.md"
    text = md.read_text()
    assert "完整数据总计：" + plain(oracle["total"]) in text and METRIC in text
    selected_table = {}
    for line in text.splitlines():
        cells = [value.strip() for value in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] in oracle["monthly"]:
            assert cells[0] not in selected_table, "A repeated or stale monthly table was included"
            selected_table[cells[0]] = Decimal(cells[1])
            assert int(cells[2]) == int(cells[3]) == independent_month_counts[cells[0]]
    assert selected_table == oracle["monthly"]
    for month, value in oracle["monthly"].items():
        assert month in text and plain(value) in text
    assert "![" in text, "The selected real chart must be embedded, not silently skipped"
    assert "仅展示部分分组" not in text
    assert (
        artifact_id(
            next(
                a
                for a in store.list_artifacts(conversation.id)
                if a.source_tool == "aggregate_preview" and a.dataset_ref == final_ref
            )
        )
        in text
    )
    downloaded = await api_request(store, "GET", f"/analyze/report/{payload['report_id']}.pdf")
    assert downloaded.status_code == 200, downloaded.text
    assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(pdf.read_bytes()).digest()
    (tmp_path / "downloaded-report.pdf").write_bytes(downloaded.content)
    (tmp_path / "new-report.md").write_text(text)

    # Open the exact downloaded PDF using already-installed native Chromium.
    # No HTML substitute or new dependency. Capture and visual verdict differ.
    inspector = Path(__file__).with_name("inspect_actual_pdf_chromium.py")
    capture_dir = tmp_path / "actual-pdf-pages"
    try:
        capture = subprocess.run(
            [
                sys.executable,
                str(inspector),
                "--pdf",
                str(tmp_path / "downloaded-report.pdf"),
                "--output",
                str(capture_dir),
                "--sha256",
                hashlib.sha256(downloaded.content).hexdigest(),
            ],
            text=True,
            capture_output=True,
            timeout=180,
        )
        support.write_witness(
            tmp_path,
            "09-pdf-capture-process",
            returncode=capture.returncode,
            stdout=capture.stdout,
            stderr=capture.stderr,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        support.write_witness(
            tmp_path,
            "09-pdf-capture-process",
            status="BLOCKED",
            error=str(error),
            cleanup="root must verify only this capture process/resources; not claimed closed",
        )
    capture_record = capture_dir / "pdf-native-capture.json"
    visual_capture = (
        json.loads(capture_record.read_text())
        if capture_record.exists()
        else {"status": "BLOCKED", "reason": "native viewer produced no capture record"}
    )

    artifact_snapshot = {a.id: a.payload for a in store.list_artifacts(conversation.id)}
    before = await conversation_detail(store, conversation)
    reopened = SessionStore(store.db_path)
    after = await conversation_detail(reopened, conversation)
    first_context = support.execution_projection(before)
    next_context = support.execution_projection(after)
    assert first_context == next_context
    assert next_context["dataset_ref"] == final_ref and next_context["run_status"] == "completed"
    lineage = next_context["lineage"]
    assert [(edge["input_ref"], edge["output_ref"]) for edge in lineage] == list(
        zip(refs, refs[1:], strict=False)
    )
    for edge, (_, parameters, _, _) in zip(lineage, transforms, strict=True):
        assert {
            key: value for key, value in edge["arguments"].items() if key != "dataset_ref"
        } == parameters
    assert TaskStore(reopened.db_path).get_run(report["run"]["run_id"]).status == "completed"
    for run_id, digest in frozen_run_hashes.items():
        assert TaskStore(reopened.db_path).data_version_hash(run_id) == digest
    assert {a.id: a.payload for a in reopened.list_artifacts(conversation.id)} == artifact_snapshot
    support.write_witness(
        tmp_path,
        "10-reopen",
        before=before,
        after=after,
        lineage_refs=refs,
        report_sha256=hashlib.sha256(downloaded.content).hexdigest(),
        local_tool_and_persistence_chain="PASS",
        pdf_capture=visual_capture,
        real_model="NOT_RUN",
        frontend_browser="NOT_RUN",
        manual_pdf_visual_review="NOT_REVIEWED",
        overall_r4_gate="PENDING_INDEPENDENT_VISUAL_REVIEW",
    )
