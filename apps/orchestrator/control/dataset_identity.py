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
    # A descriptive outer label can contain another explicit typed label as
    # its apparent value (数据集 dataset_ref: ...). Enumerate real, nonempty
    # matches at every label start, so the outer candidate cannot hide it.
    candidates: list[re.Match[str]] = []
    position = 0
    while match := IDENTITY_DECLARATION.search(text, position):
        candidates.append(match)
        position = match.start() + 1
    if not headers_only:
        return candidates
    found: list[re.Match[str]] = []
    for clause in re.finditer(r"(?:^|[，,；;])\s*", text):
        position = clause.end()
        while match := IDENTITY_DECLARATION.match(text, position):
            nested = [item for item in candidates if match.start() <= item.start() < match.end()]
            for item in nested:
                if not any(existing.span() == item.span() for existing in found):
                    found.append(item)
            position = max(item.end() for item in nested)
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
