from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agent_hub.knowledge.ingest import InMemoryKnowledgeStore, KnowledgeChunk, terms


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    chunk_id: UUID
    tenant_id: UUID
    acl_users: frozenset[UUID]
    source_uri: str
    version: str
    heading: str
    text: str
    score: float
    prompt_injection_risk: bool


class HybridRetriever:
    def __init__(self, store: InMemoryKnowledgeStore) -> None:
        self._store = store

    async def search(
        self,
        query: str,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int = 5,
    ) -> tuple[KnowledgeHit, ...]:
        if limit < 1 or limit > 50:
            raise ValueError("retrieval limit must be between 1 and 50")
        query_terms = terms(query)
        query_embedding = await self._store.embed_query(query)
        scored: list[KnowledgeHit] = []
        for chunk in self._store.chunks():
            if chunk.tenant_id != tenant_id or user_id not in chunk.acl_users:
                continue
            keyword_score = _keyword_score(query_terms, chunk)
            vector_score = _cosine(query_embedding, chunk.embedding)
            score = 0.65 * keyword_score + 0.35 * vector_score
            if score <= 0:
                continue
            scored.append(
                KnowledgeHit(
                    chunk_id=chunk.id,
                    tenant_id=chunk.tenant_id,
                    acl_users=chunk.acl_users,
                    source_uri=chunk.source_uri,
                    version=chunk.version,
                    heading=chunk.heading,
                    text=chunk.text,
                    score=score,
                    prompt_injection_risk=chunk.prompt_injection_risk,
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.source_uri, hit.version, hit.heading))
        return tuple(scored[:limit])


def _keyword_score(query_terms: set[str], chunk: KnowledgeChunk) -> float:
    if not query_terms:
        return 0.0
    overlap = len(query_terms & terms(chunk.text))
    return overlap / len(query_terms)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))
