"""Independent scope-span regression from the immutable captured profile."""

from apps.orchestrator.control.profile_population import stated_population

from tests.test_stage2_profile_intent_regression import _load, _refs


def test_absent_operation_scope_does_not_consume_declared_population_quantity():
    text = "播出时长（分钟）描述统计（口径：基于全部 75 行、未做任何筛选或清洗）"
    request = stated_population(text, {"播出时长（分钟）"})
    assert request is not None and request.valid and not request.filters
    position = text.index("75")
    assert not (request.span[0] <= position < request.span[1])


def test_captured_six_statistics_keep_their_original_evidence_binding():
    fixture, evidence = _load()
    refs = _refs(fixture["candidate"], fixture["goal"], evidence)
    expected = {
        "10.0": "$.profile.columns[3].min",
        "999.0": "$.profile.columns[3].max",
        "50.121622": "$.profile.columns[3].mean",
        "37.5": "$.profile.columns[3].median",
        "113.152819": "$.profile.columns[3].std",
        "37": "$.profile.columns[3].distinct_count",
    }
    for token, path in expected.items():
        actual = [ref for ref in refs if ref.get("token") == token]
        assert len(actual) == 1
        assert actual[0].get("supported") is True
        assert actual[0].get("path") == path
    # Ambiguous original duplicate claims must remain rejected independently.
    assert any(ref.get("supported") is False for ref in refs)
