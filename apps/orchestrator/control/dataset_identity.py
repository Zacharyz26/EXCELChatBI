"""One typed vocabulary for explicit dataset/version identity declarations."""

from __future__ import annotations

import re

from packages.session.models import JsonObject

IDENTITY_FIELDS = {
    "dataset_ref": "dataset_ref",
    "数据集": "dataset_ref",
    "dataset_version": "dataset_version",
    "版本": "dataset_version",
}
IDENTITY_DECLARATION = re.compile(
    r"(?P<label>(?<![A-Za-z0-9_])(?:dataset_version|dataset_ref)|数据集|版本)"
    r"\s*[:：=]?\s*(?P<value>(?:(?!数据集|版本)[^\s，,；;。！？!?：:=])+)"
)


def identity_declarations(text: str, *, headers_only: bool = False) -> list[re.Match[str]]:
    if not headers_only:
        return list(IDENTITY_DECLARATION.finditer(text))
    found: list[re.Match[str]] = []
    for clause in re.finditer(r"(?:^|[，,；;])\s*", text):
        position = clause.end()
        while match := IDENTITY_DECLARATION.match(text, position):
            found.append(match)
            position = match.end()
            while position < len(text) and text[position].isspace():
                position += 1
    return found


def identity_literal(value: str) -> bool:
    return re.fullmatch(r"(?:[a-f0-9]{32}|[a-f0-9]{64})", value) is not None


def identity_matches_binding(match: re.Match[str], binding: JsonObject) -> bool:
    """The key chooses the identity dimension; equal strings never swap roles."""
    return identity_literal(match["value"]) and (
        match["value"] == binding.get(IDENTITY_FIELDS[match["label"]])
    )
