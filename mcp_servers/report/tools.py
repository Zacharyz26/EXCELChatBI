"""报告工具实现（Markdown · WeasyPrint）——**纯组装，零 LLM**。

铁律（防重蹈 R1）：本模块三个工具内**绝不调用任何 LLM/gateway**。报告中的中文
解读一律由编排层调用 `stats_interpreter.interpret_stats`（已接入数据安全策略的唯一
出口）在报告生成前产出，作为入参传入；这里只做把画像/图片/统计结果/解读文字拼装成
Markdown 与 PDF 的纯格式化工作。报告里所有数字来自工具真实结果（红线2）。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

from packages.common.config import get_settings

# ── PDF 用 HTML 模板：CJK 字体 + 基础排版（图片已 base64 内嵌，无需 base_url）──
_HTML_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 1.6cm; }}
body {{ font-family: "WenQuanYi Zen Hei","Noto Sans CJK SC","Noto Sans CJK",sans-serif;
       font-size: 12px; color: #222; line-height: 1.6; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #4a7; padding-bottom: 6px; }}
h2 {{ font-size: 17px; margin-top: 18px; border-bottom: 1px solid #ddd; }}
h3 {{ font-size: 14px; margin-top: 12px; }}
table {{ border-collapse: collapse; font-size: 11px; margin: 6px 0; }}
th,td {{ border: 1px solid #bbb; padding: 3px 8px; text-align: left; }}
th {{ background: #f2f6f2; }}
img {{ max-width: 100%; }}
blockquote {{ margin: 6px 0; padding: 6px 12px; background: #f4f8ff;
             border-left: 3px solid #4a7; }}
</style></head><body>{body}</body></html>"""


def _reports_dir() -> Path:
    d = Path(get_settings().report_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def gen_report_md(args: dict[str, Any]) -> dict[str, Any]:
    """把画像 + 图表(图片) + 统计结果 + 解读文字组装成 Markdown，落盘。

    Args:
        args: {title, profile, charts?[{caption?, image_path}],
               stats?[{kind, caption?, result, interpretation?}], insights?}。

    Returns:
        {report_id, md_path, markdown}。
    """
    title: str = args["title"]
    profile: dict[str, Any] = args["profile"]
    charts: list[dict[str, Any]] = args.get("charts", [])
    stats: list[dict[str, Any]] = args.get("stats", [])
    insights: str | None = args.get("insights")

    lines: list[str] = [f"# {title}", ""]
    lines += _profile_md(profile)
    if insights:
        lines += ["## 要点速览", "", insights, ""]
    if charts:
        lines += ["## 图表", ""]
        for c in charts:
            if c.get("caption"):
                lines += [f"### {c['caption']}", ""]
            lines += [f"![chart]({_img_data_uri(c['image_path'])})", ""]
    if stats:
        lines += ["## 统计分析", ""]
        for s in stats:
            lines += _stat_md(s)

    markdown = "\n".join(lines)
    report_id = uuid.uuid4().hex
    md_path = _reports_dir() / f"{report_id}.md"
    _atomic_write_text(md_path, markdown)
    return {"report_id": report_id, "md_path": str(md_path), "markdown": markdown}


def insight_summary(args: dict[str, Any]) -> dict[str, Any]:
    """把已生成的各段解读拼成"要点速览"Markdown（纯拼接，**不调 LLM**）。

    Args:
        args: {items: [{label, text}]}。text 由 stats_interpreter 在编排层产出。

    Returns:
        {summary_md}。
    """
    items: list[dict[str, Any]] = args["items"]
    parts = [
        f"- **{it.get('label', '')}**：{it['text']}"
        for it in items
        if it.get("text")
    ]
    return {"summary_md": "\n".join(parts)}


def export_pdf(args: dict[str, Any]) -> dict[str, Any]:
    """Markdown → HTML → PDF（WeasyPrint）。图片已 base64 内嵌，PDF 自包含。

    Args:
        args: {report_id}。

    Returns:
        {report_id, pdf_path, bytes}。

    Raises:
        FileNotFoundError: report_id 对应的 .md 不存在。
    """
    import markdown as md_lib  # type: ignore[import-untyped]
    import weasyprint

    report_id: str = args["report_id"]
    md_path = _reports_dir() / f"{report_id}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"报告不存在: {report_id}")

    html_body = md_lib.markdown(
        md_path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
    )
    html = _HTML_TEMPLATE.format(body=html_body)
    pdf_bytes = weasyprint.HTML(string=html).write_pdf()
    pdf_path = _reports_dir() / f"{report_id}.pdf"
    _atomic_write_bytes(pdf_path, pdf_bytes)
    return {"report_id": report_id, "pdf_path": str(pdf_path), "bytes": len(pdf_bytes)}


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Publish a complete report file with a same-directory atomic rename."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


# ── 内部：Markdown 片段（纯格式化，数字来自入参的工具结果）──

def _img_data_uri(image_path: str) -> str:
    """PNG 文件 → base64 data URI（内嵌，报告自包含）。"""
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _fmt(v: Any) -> str:
    """数值 → 展示字符串。"""
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _profile_md(profile: dict[str, Any]) -> list[str]:
    """数据画像段。"""
    cols = profile.get("columns", [])
    out = [
        "## 数据画像",
        "",
        f"共 {profile.get('row_count', '—')} 行 · {profile.get('column_count', '—')} 列",
        "",
        "| 列名 | 类型 | 空值率 | distinct | 最小 | 最大 | 均值 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in cols:
        nr = c.get("null_ratio")
        nr_s = f"{nr * 100:.1f}%" if isinstance(nr, int | float) else "—"
        out.append(
            f"| {c.get('name', '')} | {c.get('dtype', '')} | {nr_s} | "
            f"{_fmt(c.get('distinct_count'))} | {_fmt(c.get('min'))} | "
            f"{_fmt(c.get('max'))} | {_fmt(c.get('mean'))} |"
        )
    out.append("")
    return out


_KIND_LABEL = {"trend": "趋势分析", "anomaly": "异常检测", "regression": "回归分析"}


def _stat_md(section: dict[str, Any]) -> list[str]:
    """单个统计段：标题 + 结果表（数字来自工具）+ 解读文字（来自 stats_interpreter）。"""
    kind = section.get("kind", "")
    result = section.get("result", {})
    caption = section.get("caption") or _KIND_LABEL.get(kind, kind)
    out = [f"### {caption}", ""]

    if kind == "trend":
        fc = result.get("forecast") or []
        out += [
            f"- 方向：**{result.get('direction', '—')}** · 斜率 {_fmt(result.get('slope'))} · "
            f"季节强度 {_fmt(result.get('seasonality_strength'))} · 样本 {_fmt(result.get('n'))}",
            f"- 预测（未来 {len(fc)} 期）：{'、'.join(_fmt(v) for v in fc) or '—'}",
            "",
        ]
    elif kind == "anomaly":
        out += [
            f"- 方法 {result.get('method', '—')} · 样本 {_fmt(result.get('n_total'))}"
            f" · 异常 **{_fmt(result.get('n_anomalies'))}**",
            "",
        ]
        anomalies = result.get("anomalies") or []
        if anomalies:
            out += ["| 序号 | 值 | 异常分 | 时间 |", "|---|---|---|---|"]
            out += [
                f"| {_fmt(a.get('index'))} | {_fmt(a.get('value'))} | "
                f"{_fmt(a.get('score'))} | {a.get('time', '—')} |"
                for a in anomalies
            ]
            out.append("")
    elif kind == "regression":
        out += [
            f"- {str(result.get('kind', '')).upper()} · R² {_fmt(result.get('r_squared'))}"
            f" · 调整 R² {_fmt(result.get('adj_r_squared'))} · 样本 {_fmt(result.get('n_obs'))}",
            "",
        ]
        coefs = result.get("coefficients") or []
        if coefs:
            out += ["| 变量 | 系数 | 标准误 | p 值 | 显著 |", "|---|---|---|---|---|"]
            out += [
                f"| {c.get('name', '')} | {_fmt(c.get('coef'))} | {_fmt(c.get('std_err'))} | "
                f"{_fmt(c.get('p_value'))} | {'✓' if c.get('significant') else ''} |"
                for c in coefs
            ]
            out.append("")
    elif kind == "correlation":
        cols = result.get("columns") or []
        matrix = result.get("matrix") or []
        out += [f"- 方法 {result.get('method', '—')} · 样本 {_fmt(result.get('n_obs'))}", ""]
        if cols and matrix:
            out += ["| | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
            out += [
                f"| {cols[i]} | " + " | ".join(_fmt(v) for v in row) + " |"
                for i, row in enumerate(matrix)
            ]
            out.append("")

    interp = section.get("interpretation")
    if interp:
        out += [f"> {interp}", ""]
    return out
