"""NOT_RUN draft: open the actual PDF in existing Chromium's native viewer.

No HTML reconstruction, dependency installation, model request or external URL.
Captures are evidence awaiting root's independent page-by-page visual review;
the script never writes visual PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def inspect(pdf, destination, expected_sha256):
    destination.mkdir(parents=True, exist_ok=True)
    metadata = {
        "status": "BLOCKED",
        "visual_gate": "NOT_REVIEWED",
        "pdf": str(pdf),
        "pdf_sha256": None,
        "expected_sha256": expected_sha256,
        "pages": [],
        "requests": [],
        "blocked_requests": [],
        "closed": False,
        "layer": "native Chromium actual-PDF viewer; not frontend app testing",
    }
    browser = context = None
    try:
        metadata["pdf_sha256"] = digest(pdf)
        if metadata["pdf_sha256"] != expected_sha256:
            raise RuntimeError("Actual PDF bytes no longer match the verified download")
        # Use only existing installed browsers. No playwright install/download.
        candidates = sorted(
            Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome")
        )
        if not candidates:
            candidates = sorted(
                Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux/chrome")
            )
        if not candidates:
            raise RuntimeError("Existing Chromium is unavailable")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=str(candidates[-1]),
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--allow-file-access-from-files"],
            )
            metadata["chromium"] = str(candidates[-1])
            context = browser.new_context(viewport={"width": 1400, "height": 1900})

            def guard(route):
                url = route.request.url
                metadata["requests"].append(url)
                if url.startswith(("file:", "chrome:", "chrome-extension:", "data:", "blob:")):
                    route.continue_()
                else:
                    metadata["blocked_requests"].append(url)
                    route.abort("blockedbyclient")

            context.route("**/*", guard)
            page = context.new_page()
            page.set_default_timeout(20_000)
            try:
                # The PDF fragment controls its native viewer, not HTML content.
                page.goto(pdf.as_uri() + "#page=1&zoom=80", wait_until="load", timeout=30_000)
                page.wait_for_timeout(3500)
                metadata["url"] = page.url
                metadata["frames"] = [frame.url for frame in page.frames]
                native = [
                    frame for frame in page.frames if frame.url.startswith("chrome-extension:")
                ]
                if len(native) != 1:
                    raise RuntimeError("A unique native Chromium PDF viewer frame was not found")
                viewer = native[0]
                viewer.locator("pdf-viewer").wait_for(state="attached")
                # Read the existing viewer's own metadata, never guess page
                # count from source Markdown or scan the PDF bytes with regex.
                details = viewer.evaluate("""() => {
                    const found = [];
                    const walk = root => {
                        for (const el of root.querySelectorAll('*')) {
                            if (['viewer-toolbar','viewer-page-selector'].includes(el.localName)) {
                                found.push({tag:el.localName, id:el.id,
                                    docLength:el.docLength, pageNo:el.pageNo});
                            }
                            if (el.shadowRoot) walk(el.shadowRoot);
                        }
                    };
                    walk(document); return found;
                }""")
                metadata["native_page_controls"] = details
                counts = {
                    item["docLength"]
                    for item in details
                    if isinstance(item.get("docLength"), int)
                    and not isinstance(item.get("docLength"), bool)
                    and item["docLength"] > 0
                }
                if len(counts) != 1:
                    raise RuntimeError(
                        "Native PDF page count is unavailable/ambiguous; page coverage is unknown"
                    )
                total = counts.pop()
                metadata["page_count"] = total
                page_controls = viewer.locator("viewer-page-selector input")
                if page_controls.count() != 1:
                    raise RuntimeError(
                        "Native page selector is unavailable/ambiguous; "
                        "cannot claim all-page capture"
                    )
                control = page_controls.first
                for number in range(1, total + 1):
                    control.fill(str(number))
                    control.press("Enter")
                    page.wait_for_timeout(1200)
                    if control.input_value() != str(number):
                        raise RuntimeError(f"Native viewer did not select page {number}")
                    screenshot = destination / f"page-{number:03d}.png"
                    page.screenshot(path=str(screenshot), full_page=False)
                    metadata["pages"].append(
                        {
                            "number": number,
                            "selected_page": control.input_value(),
                            "screenshot": str(screenshot),
                            "sha256": digest(screenshot),
                            "visual_status": "NOT_REVIEWED",
                        }
                    )
                if metadata["blocked_requests"]:
                    raise RuntimeError("Unexpected external request was blocked")
                if digest(pdf) != expected_sha256:
                    raise RuntimeError("PDF changed during capture")
                metadata["status"] = "CAPTURED_PENDING_VISUAL_REVIEW"
            finally:
                context.close()
                context = None
                browser.close()
                browser = None
                metadata["closed"] = True
    except Exception as error:
        metadata["error"] = f"{type(error).__name__}: {error}"
    finally:
        cleanup_errors = []
        if context is not None:
            try:
                context.close()
                context = None
            except Exception as error:
                cleanup_errors.append(f"context: {error}")
        if browser is not None:
            try:
                browser.close()
                browser = None
            except Exception as error:
                cleanup_errors.append(f"browser: {error}")
        metadata["closed"] = context is None and browser is None
        if cleanup_errors:
            metadata["cleanup_errors"] = cleanup_errors
            metadata["status"] = "BLOCKED"
        # Preserve a diagnostic even when launch/selector discovery fails.
        write(destination / "pdf-native-capture.json", metadata)
        write(
            destination / "visual-review-required.json",
            {
                "status": "NOT_REVIEWED",
                "reviewer": None,
                "pdf_sha256": metadata["pdf_sha256"],
                "capture_status": metadata["status"],
                "pages": [
                    {
                        "number": row["number"],
                        "screenshot_sha256": row["sha256"],
                        "status": "NOT_REVIEWED",
                        "findings": [],
                    }
                    for row in metadata["pages"]
                ],
                "required_checks": [
                    "Open every actual captured PDF page; verify Chinese glyphs and units",
                    "Read monthly row/value pairs and total against independent CSV oracle",
                    "Inspect actual chart, complete pagination and clipping/truncation",
                    "Record evidence-backed PASS/FAIL per page; screenshot existence is not PASS",
                ],
                "automatic_test_pass_does_not_close_r4": True,
            },
        )
    return metadata["status"] == "CAPTURED_PENDING_VISUAL_REVIEW"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    ok = inspect(args.pdf.resolve(), args.output.resolve(), args.sha256)
    raise SystemExit(0 if ok else 2)
