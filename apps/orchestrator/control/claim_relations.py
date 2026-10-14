"""从陈述解析完整关系端点；不先用 Evidence 词表删除未知端点。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.orchestrator.control.statistic_roles import label_pattern, role_mentions


@dataclass(frozen=True)
class SubjectRelation:
    """显式关系保留成员多重集及方向；单成员省略单独表示。"""

    members: tuple[str, ...]
    explicit: bool
    directed: bool = False


_CONNECTOR = re.compile(
    r"\s*(->|<-|→|←|与|和|及|对比|相比|、|/|／|&|＆|—|–|，|,|\band\b|\bversus\b|\bvs\.?\b)\s*", re.I
)


def parse_subject_relation(
    text: str,
    role: str,
    qualifiers: tuple[str, ...],
    metric: str | None = None,
    *,
    subject_only: bool = False,
) -> SubjectRelation | None:
    """解析明确的二元关系或完整单成员；不认识的残余文字不会被丢弃。"""
    if subject_only:
        subject = text.strip()
    else:
        roles = [start for start, _, found in role_mentions(text, {role}) if found == role]
        if not roles:
            return None
        subject = text[: min(roles)].strip()
        subject = re.sub(r"^报告内容[：:]\s*", "", subject)
        subject = re.sub(
            r"^(?:(?:dataset_ref|数据集|版本)\s*[:：=]?\s*[a-f0-9]{32,64}\s*)+", "", subject
        )
        # Only explicit leading context is removable, never an unknown endpoint.
        previous = None
        while previous != subject:
            previous = subject
            for label in sorted(qualifiers, key=len, reverse=True):
                subject = re.sub(
                    r"^" + label_pattern(label) + r"\s*(?:的)?\s*", "", subject, flags=re.I
                )
        subject = re.sub(r"(?:之间|的)+$", "", subject).strip()
        if metric:
            subject = re.sub(re.escape(metric) + r"$", "", subject).strip()
            subject = re.sub(r"(?:之间|的)+$", "", subject).strip()
    parts = _CONNECTOR.split(subject)
    if len(parts) == 1:
        return SubjectRelation((subject,) if subject else (), False)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        return None
    left, connector, right = parts
    members = (right, left) if connector in {"<-", "←"} else (left, right)
    return SubjectRelation(members, True, connector in {"->", "<-", "→", "←"})
