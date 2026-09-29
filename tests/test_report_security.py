"""Untrusted Markdown must not turn PDF export into a file/network client."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest
from mcp_servers.report import tools as report_tools
from packages.common.report_safety import (
    MAX_PNG_BYTES,
    PNG_DATA_PREFIX,
    report_url_fetcher,
    sanitize_report_html,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
_URI = PNG_DATA_PREFIX + base64.b64encode(_PNG).decode()
_REPORT_ID = "a" * 32


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/private",
        "https://example.com/image.png",
        "ftp://example.com/a",
        "//example.com/a",
        "../secret",
        "data:text/html;base64,PHAvPg==",
        "data:image/svg+xml,<svg/>",
        "data:image/png;base64,!!!",
    ],
)
def test_pdf_fetcher_rejects_every_non_png_resource(url: str) -> None:
    with pytest.raises(ValueError):
        report_url_fetcher(url)


def test_pdf_fetcher_accepts_png_and_bounds_bytes_and_pixels() -> None:
    assert report_url_fetcher(_URI) == {"string": _PNG, "mime_type": "image/png"}
    huge_pixels = _PNG[:16] + struct.pack(">II", 100_000, 100_000) + _PNG[24:]
    for data in (b"not a PNG", huge_pixels, _PNG[:-5], _PNG + b"0" * MAX_PNG_BYTES):
        with pytest.raises(ValueError):
            report_url_fetcher(PNG_DATA_PREFIX + base64.b64encode(data).decode())


def test_html_whitelist_removes_css_attachments_svg_and_unsafe_attributes() -> None:
    body = sanitize_report_html(
        '<link rel="attachment" href="file:///private">'
        '<style>@import "https://example.com";</style><svg><image href="file:///x"/></svg>'
        '<p style="background:url(file:///x)" onclick="alert(1)">正文</p>'
        '<a rel="attachment" href="file:///private">附件</a>'
        '<img src="https://example.com/p.png">'
        f'<img src="{_URI}"><table><tr><td>42</td></tr></table>'
    )
    for denied in ("file:", "https:", "style", "attachment", "onclick", "svg"):
        assert denied not in body
    assert "<p>正文</p>" in body
    assert _URI in body and "<td>42</td>" in body


def test_generated_markdown_escapes_raw_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_tools, "_reports_dir", lambda: tmp_path)
    result = report_tools.gen_report_md(
        {
            "title": '<link rel="attachment" href="file:///private">',
            "profile": {},
            "insights": "<script>alert('x')</script> **正常 Markdown**",
        }
    )
    assert "<script>" not in result["markdown"]
    assert "<link" not in result["markdown"]
    assert "**正常 Markdown**" in result["markdown"]


def test_real_pdf_never_fetches_local_or_remote_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weasyprint = pytest.importorskip("weasyprint")
    monkeypatch.setattr(report_tools, "_reports_dir", lambda: tmp_path)
    # Test legacy/on-disk Markdown too: escaping new narratives is not the boundary.
    marker = tmp_path / "marker.txt"
    marker.write_text("SYNTHETIC_ATTACHMENT_MUST_NOT_LEAK", encoding="utf-8")
    (tmp_path / f"{_REPORT_ID}.md").write_text(
        f'<link rel="attachment" href="{marker.as_uri()}">\n\n'
        f'<a rel="attachment" href="{marker.as_uri()}">secret</a>\n\n'
        '<style>@import url("http://127.0.0.1:9/private");</style>\n\n'
        "![remote](https://example.com/private.png)\n\n"
        f"![safe]({_URI})\n\n| a | b |\n|---|---|\n| 1 | 2 |",
        encoding="utf-8",
    )
    attempts: list[str] = []

    def forbidden_fetch(*args: object, **kwargs: object) -> None:
        attempts.append(str(args))
        raise AssertionError("Default resource fetcher must not be used")

    monkeypatch.setattr(weasyprint.urls, "urlopen", forbidden_fetch)
    fetched: list[str] = []

    def tracked_fetch(url: str, *args: object, **kwargs: object) -> dict[str, object]:
        fetched.append(url)
        return report_url_fetcher(url)

    monkeypatch.setattr(report_tools, "report_url_fetcher", tracked_fetch)
    result = report_tools.export_pdf({"report_id": _REPORT_ID})
    pdf = Path(result["pdf_path"]).read_bytes()
    assert pdf.startswith(b"%PDF-")
    assert b"/EmbeddedFiles" not in pdf and b"/Filespec" not in pdf
    assert b"/Subtype /Image" in pdf  # real inline PNG still renders
    assert attempts == [] and fetched == [_URI]


@pytest.mark.parametrize("report_id", ["../secret", "/tmp/secret", "a/../../secret"])
def test_export_rejects_path_traversal(report_id: str) -> None:
    pytest.importorskip("weasyprint")
    with pytest.raises(ValueError, match="标识符"):
        report_tools.export_pdf({"report_id": report_id})
