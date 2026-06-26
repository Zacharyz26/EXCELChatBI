"""指代消解：把"这个图""刚才那张表"等映射到具体实体（设计文档 5.2.2）。

双策略并用：
1. 维护 entity_map，把别名映射到 chart_id / dataset_ref；
2. 对模糊指代用轻量模型做 query 改写，替换为明确实体后再进主流程。
"""

from __future__ import annotations

from packages.session.state import SessionState


def resolve_reference(query: str, state: SessionState) -> str:
    """对含指代的 query 做消解，返回实体明确化后的 query。

    Args:
        query: 用户原始输入（可能含"这个""上面那个"等指代）。
        state: 会话状态，提供 entity_map / chart_registry。
    """
    raise NotImplementedError("TODO: 先查 entity_map 命中；未命中再用轻量模型改写")
