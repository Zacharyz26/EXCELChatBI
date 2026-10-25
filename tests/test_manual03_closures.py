"""New heading/query scope must respect matching lexical delimiters."""

import pytest

from tests.test_manual03_contract import (
    HEADING,
    evaluate,
)
from tests.test_manual03_contract import (
    test_knowledge_request_and_negation_scope as check_request,
)


@pytest.mark.parametrize(
    "text",
    [
        HEADING + "单位即列名中的“分钟』。该列最大值999。",
        "````markdown\n```\n" + HEADING + "该列最大值999。\n````",
    ],
)
def test_heading_requires_matching_quote_and_fence_closure(text, tmp_path):
    evaluate(text, False, tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "````text\n```\n请查询公司规定。\n````",
        "~~~text\n~~~still-code\n请查询公司规定。\n~~~",
    ],
)
def test_query_inside_unclosed_code_remains_data(text, tmp_path):
    check_request(text, False, tmp_path)


def test_closed_code_releases_own_heading_scope(tmp_path):
    text = "````text\n```\nquoted sample\n````\n\n" + HEADING + "该列最大值999。"
    evaluate(text, True, tmp_path)


def test_closed_code_releases_genuine_query(tmp_path):
    check_request("````text\n```\n不要查询公司规定\n````\n\n请查询公司规定。", True, tmp_path)
