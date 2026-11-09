"""Real deterministic keep policy and the public tool output schema.

The input records distinguish first from last for a subset-key operation.
The oracle comes from input order, independently of the emitted policy field.
"""

from copy import deepcopy

import pandas as pd
import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp_servers.dataset_ops.server import build_server
from packages.common.dataset_store import load_dataframe, save_dataframe

from tests.test_r2_operation_completion import no_network

__all__ = ["no_network"]


@pytest.fixture
def policy_output():
    source = save_dataframe(pd.DataFrame({"频道": ["A", "A", "B"], "时长": [10, 20, 30]}))
    tool = build_server()._tools["transform_dataset"]
    result = tool.invoke({"dataset_ref": source, "drop_duplicates": ["频道"]})
    validator = Draft202012Validator(tool.output_schema)
    validator.validate(result)
    return result, validator


def test_retention_policy_is_actual_first_and_valid_public_output(policy_output):
    result, _ = policy_output
    assert result["rows_before"] == 3 and result["rows_after"] == 2
    assert load_dataframe(result["dataset_ref"]).to_dict("records") == [
        {"频道": "A", "时长": 10},
        {"频道": "B", "时长": 30},
    ]
    assert result["row_changes"]["steps"][0]["retention_policy"] == "first"
    assert result["row_changes"]["steps"][0]["columns"] == ["频道"]


@pytest.mark.parametrize("value", ["last", "2", 0, None, False])
def test_output_schema_rejects_other_retention_policies(policy_output, value):
    result, validator = policy_output
    malformed = deepcopy(result)
    malformed["row_changes"]["steps"][0]["retention_policy"] = value
    with pytest.raises(ValidationError):
        validator.validate(malformed)


def test_legacy_output_schema_remains_compatible_without_policy(policy_output):
    result, validator = policy_output
    legacy = deepcopy(result)
    legacy["row_changes"]["steps"][0].pop("retention_policy")
    validator.validate(legacy)
