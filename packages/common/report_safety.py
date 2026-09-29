"""Closed resource boundary for untrusted report HTML (no network or filesystem)."""

from __future__ import annotations

import base64
import binascii
import html
import struct
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

MAX_PNG_BYTES = 12 * 1024 * 1024
MAX_PNG_PIXELS = 16_000_000
PNG_DATA_PREFIX = "data:image/png;base64,"
_TAGS = frozenset(
    "p br hr h1 h2 h3 h4 h5 h6 strong em b i u s del blockquote pre code "
    "ul ol li table thead tbody tfoot tr th td a img".split()
)
_VOID = frozenset({"br", "hr", "img"})
_DROP_CONTENT = frozenset({"script", "style", "svg", "math", "iframe", "object"})


def escape_report_markup(value: Any) -> Any:
    """Keep Markdown formatting, but render embedded HTML as literal text."""
    if isinstance(value, str):
        return html.escape(value, quote=False)
    if isinstance(value, dict):
        return {key: escape_report_markup(item) for key, item in value.items()}
    if isinstance(value, list):
        return [escape_report_markup(item) for item in value]
    return value


def validate_png(data: bytes) -> None:
    """Bound compressed bytes and decoded dimensions before the PDF image loader."""
    if (
        len(data) > MAX_PNG_BYTES
        or len(data) < 33
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[8:16] != b"\x00\x00\x00\rIHDR"
    ):
        raise ValueError("报告图片必须是大小受限的 PNG")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height or width * height > MAX_PNG_PIXELS:
        raise ValueError("报告 PNG 像素数超过处理上限")
    # Do not pass truncated chunks or malformed CRCs to the PDF image decoder.
    offset = 8
    has_image_data = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + 12 + length
        if end > len(data):
            break
        chunk = data[offset + 4 : end - 4]
        checksum = int.from_bytes(data[end - 4 : end], "big")
        if binascii.crc32(chunk) != checksum:
            break
        kind = chunk[:4]
        has_image_data = has_image_data or kind == b"IDAT"
        if kind == b"IEND":
            if length == 0 and end == len(data) and has_image_data:
                return
            break
        offset = end
    raise ValueError("报告 PNG 文件损坏")


def report_url_fetcher(url: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Never delegate to WeasyPrint's default fetcher, including redirects/SVG."""
    if not url.startswith(PNG_DATA_PREFIX):
        raise ValueError("报告仅允许内嵌 PNG，禁止访问外部或本地资源")
    encoded = url[len(PNG_DATA_PREFIX) :]
    if len(encoded) > ((MAX_PNG_BYTES + 2) // 3) * 4:
        raise ValueError("报告图片超过处理上限")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("报告图片编码非法") from exc
    validate_png(data)
    return {"string": data, "mime_type": "image/png"}


class _ReportHTML(HTMLParser):
    """Rebuild a small HTML subset; never copy arbitrary attributes or CSS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self.blocked.append(tag)
        if self.blocked or tag not in _TAGS:
            return
        source = dict(attrs)
        safe: dict[str, str] = {}
        if tag == "img":
            src = source.get("src") or ""
            try:
                report_url_fetcher(src)
            except ValueError:
                return
            safe = {"src": src, "alt": source.get("alt") or "chart"}
        elif tag == "a":
            href = source.get("href") or ""
            try:
                scheme = urlsplit(href).scheme.lower()
            except ValueError:
                scheme = "invalid"
            if scheme in {"http", "https", "mailto"} or href.startswith("#"):
                safe["href"] = href
        encoded = "".join(
            f' {key}="{html.escape(value, quote=True)}"' for key, value in safe.items()
        )
        self.parts.append(f"<{tag}{encoded}>")

    def handle_endtag(self, tag: str) -> None:
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        if tag in _TAGS and tag not in _VOID:
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.parts.append(html.escape(data))


def sanitize_report_html(body: str) -> str:
    parser = _ReportHTML()
    parser.feed(body)
    parser.close()
    return "".join(parser.parts)
