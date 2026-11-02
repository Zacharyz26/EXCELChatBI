"""Independent visible-data chart control for R4-CHART-POLICY-01.

No policy override. Five copies of synthetic rows establish enough valid
measurements for the unchanged min-size-five policy. Only the model is scripted.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict
from decimal import Decimal

import pytest
from packages.common.dataset_store import load_dataframe

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_independent_default_consumers import default_chart, successful, upload


def repeat_rows(raw, copies):
    lines = raw.decode().splitlines(keepends=True)
    return (lines[0] + "".join(lines[1:] * copies)).encode()


@pytest.mark.asyncio
async def test_sufficient_synthetic_groups_render_default_current_child_values(tmp_path):
    body = repeat_rows(support.CSV, 5)
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    selected = [r for r in rows if r["频道"] == "A" and r["状态"] == "已播出"]
    sums = {}
    valid = {}
    for row in selected:
        key = row["月份"]
        sums.setdefault(key, Decimal(0))
        valid.setdefault(key, 0)
        if row[support.METRIC].strip():
            sums[key] += Decimal(row[support.METRIC])
            valid[key] += 1
    sums = dict(sorted(sums.items()))
    assert len(rows) == 30 and len(selected) == 20
    assert valid == {"2026-01": 10, "2026-02": 5}
    assert list(sums.values()) == [Decimal(150), Decimal(250)]

    store, project, conversation = support.workspace(tmp_path)
    parent = await upload(store, project, conversation, body=body, name="sufficient-source.csv")
    filtered = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                ("transform_dataset", {"dataset_ref": parent, "filters": support.FILTERS}),
            ],
            "筛选前30行，筛选后20行。",
        ),
        f"dataset_ref={parent}；筛选条件：频道=A，状态=已播出。说明筛选前后的行数。",
    )
    support.write_witness(
        tmp_path,
        "sufficient-filter",
        result=filtered,
        independent_rows=len(selected),
        independent_months=sums,
        valid_counts=valid,
    )
    assert filtered["run"]["status"] == "completed", filtered
    records = [e for e in filtered["evidence"] if e["source"].get("tool") == "transform_dataset"]
    assert len(records) == len(successful(filtered, "transform_dataset")) == 1
    child = records[0]["summary"]["fact_projection"]["output_ref"]
    assert store.get_dataset(child).parent_ref == parent and child != parent
    frame = load_dataframe(child)
    assert len(frame) == len(selected) and Decimal(str(frame[support.METRIC].sum())) == sum(
        sums.values()
    )
    other = store.create_conversation(project.id)
    latest = await upload(
        store, project, other, body=repeat_rows(support.OTHER, 5), name="latest-unrelated.csv"
    )
    result = await default_chart(store, project, conversation)
    calls = successful(result, "gen_chart")
    artifact = next(
        (
            a
            for a in store.list_artifacts(conversation.id)
            if calls and a.id == calls[0]["artifact_id"]
        ),
        None,
    )
    support.write_witness(
        tmp_path,
        "sufficient-default-chart",
        result=result,
        artifact=asdict(artifact) if artifact else None,
        parent=parent,
        child=child,
        project_latest=latest,
        independent_months=sums,
        valid_counts=valid,
        policy_override=None,
    )
    assert result["run"]["status"] == "completed", result
    assert len(calls) == 1 and calls[0]["args"]["dataset_ref"] == child
    assert artifact is not None and artifact.dataset_ref == child
    option = artifact.payload["option"]
    assert option["xAxis"]["type"] == "category" and option["yAxis"]["type"] == "value"
    assert option["xAxis"]["data"] == list(sums)
    assert [Decimal(str(v)) for v in option["series"][0]["data"]] == list(sums.values())
    assert support.METRIC in option["series"][0]["name"]
