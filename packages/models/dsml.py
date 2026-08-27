"""DeepSeek DSML compatibility at the model boundary.

Some OpenAI-compatible endpoints occasionally serialize function calls into
the assistant ``content`` field instead of returning structured
``tool_calls``.  The serialized form must never leak to the user as ordinary
assistant text.  This module accepts only the small DSML subset observed for
function calling and keeps the downstream JSON-schema validation authoritative.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

from packages.models.types import ModelResponse, ToolCall

DSML_START_MARKERS = (
    "<｜｜DSML｜｜tool_calls>",
    "<||DSML||tool_calls>",
    "<|DSML|tool_calls>",
    "<｜DSML｜tool_calls>",
)

_PREFIX = r"(?:｜｜DSML｜｜|\|\|DSML\|\||\|DSML\||｜DSML｜)"
_BLOCK_PATTERN = re.compile(
    rf"<{_PREFIX}tool_calls\s*>(?P<body>.*?)</{_PREFIX}tool_calls\s*>",
    re.DOTALL,
)
_INVOKE_PATTERN = re.compile(
    rf"<{_PREFIX}invoke\s+name=(?P<quote>['\"])(?P<name>[^'\"]+)"
    rf"(?P=quote)\s*>(?P<body>.*?)</{_PREFIX}invoke\s*>",
    re.DOTALL,
)
_PARAMETER_PATTERN = re.compile(
    rf"<{_PREFIX}parameter\s+name=(?P<name_quote>['\"])(?P<name>[^'\"]+)"
    rf"(?P=name_quote)\s+string=(?P<string_quote>['\"])(?P<string>true|false)"
    rf"(?P=string_quote)\s*>(?P<value>.*?)</{_PREFIX}parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)


def contains_dsml_tool_calls(content: str) -> bool:
    """Return whether content contains a supported DSML tool-call opener."""
    return any(marker in content for marker in DSML_START_MARKERS)


def dsml_marker_suffix_length(content: str) -> int:
    """Length of a trailing fragment that might become a DSML opener.

    Streaming callers retain only this suffix.  Normal text therefore remains
    genuinely streaming while a marker split across chunks cannot leak.
    """
    maximum = 0
    for marker in DSML_START_MARKERS:
        upper = min(len(content), len(marker) - 1)
        for length in range(upper, 0, -1):
            if content.endswith(marker[:length]):
                maximum = max(maximum, length)
                break
    return maximum


def normalize_dsml_tool_response(
    response: ModelResponse,
    tools: list[dict[str, Any]],
) -> ModelResponse:
    """Convert a valid DSML content block into structured tool calls.

    Only tool names offered in the current request are accepted.  Argument
    types are decoded, but argument schema validation remains in the
    orchestrator.  Malformed or unoffered calls fail closed with ``ValueError``
    so raw protocol text is never presented as an assistant answer.
    """
    content = response.content or ""
    if not contains_dsml_tool_calls(content):
        return response

    allowed_names = _offered_tool_names(tools)
    matches = list(_BLOCK_PATTERN.finditer(content))
    if not matches:
        raise ValueError("模型返回了不完整的 DSML 工具调用")

    parsed_calls: list[ToolCall] = []
    for block in matches:
        parsed_calls.extend(_parse_block(block.group("body"), allowed_names))
    if not parsed_calls:
        raise ValueError("模型返回的 DSML 工具调用为空")

    cleaned = _clean_content(_BLOCK_PATTERN.sub("", content))
    response.content = cleaned
    # A provider may redundantly send both structured calls and DSML content.
    # In that case structured OpenAI calls remain the source of truth; DSML is
    # only removed from visible text.
    if not response.tool_calls:
        response.tool_calls = parsed_calls
    return response


def _parse_block(body: str, allowed_names: frozenset[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    position = 0
    for index, invocation in enumerate(_INVOKE_PATTERN.finditer(body)):
        if body[position : invocation.start()].strip():
            raise ValueError("DSML tool_calls 包含无法识别的内容")
        position = invocation.end()
        name = html.unescape(invocation.group("name")).strip()
        if not name or name not in allowed_names:
            raise ValueError(f"DSML 请求了未提供的工具: {name or '?'}")
        arguments = _parse_parameters(invocation.group("body"))
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(f"{index}\0{name}\0{encoded}".encode()).hexdigest()
        calls.append(
            ToolCall(
                id=f"dsml_{digest[:24]}",
                name=name,
                arguments=encoded,
            )
        )
    if body[position:].strip():
        raise ValueError("DSML tool_calls 包含无法识别的尾部内容")
    return calls


def _parse_parameters(body: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    position = 0
    for parameter in _PARAMETER_PATTERN.finditer(body):
        if body[position : parameter.start()].strip():
            raise ValueError("DSML invoke 包含无法识别的参数内容")
        position = parameter.end()
        name = html.unescape(parameter.group("name")).strip()
        if not name or name in arguments:
            raise ValueError(f"DSML 参数名无效或重复: {name or '?'}")
        raw_value = html.unescape(parameter.group("value")).strip()
        if parameter.group("string").lower() == "true":
            arguments[name] = raw_value
            continue
        try:
            arguments[name] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DSML 参数 {name} 不是有效 JSON") from exc
    if body[position:].strip():
        raise ValueError("DSML invoke 包含无法识别的尾部内容")
    return arguments


def _offered_tool_names(tools: list[dict[str, Any]]) -> frozenset[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return frozenset(names)


def _clean_content(content: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", content).strip()
