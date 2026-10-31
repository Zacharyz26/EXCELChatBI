"""Compatibility exports for the single shared knowledge-intent compiler."""

from packages.orchestration.knowledge_intent import (
    KnowledgeIntent,
    KnowledgeQuery,
    knowledge_query_allowed,
    knowledge_query_matches,
    knowledge_query_requirements,
    knowledge_query_scope,
    knowledge_query_step_id,
    knowledge_scope_covers,
    knowledge_search_requested,
)

__all__ = [
    "KnowledgeIntent",
    "KnowledgeQuery",
    "knowledge_query_allowed",
    "knowledge_query_matches",
    "knowledge_query_scope",
    "knowledge_query_step_id",
    "knowledge_query_requirements",
    "knowledge_scope_covers",
    "knowledge_search_requested",
]
