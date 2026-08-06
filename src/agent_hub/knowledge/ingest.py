from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    id: UUID
    tenant_id: UUID
    acl_users: frozenset[UUID]
    source_uri: str
    version: str
    document_sha256: str
    heading: str
    text: str
    embedding: tuple[float, ...]
    embedding_logical_model: str
    prompt_injection_risk: bool


class EmbeddingProvider(Protocol):
    logical_model: str

    async def embed(self, text: str) -> tuple[float, ...]: ...


class DeterministicEmbeddingProvider:
    logical_model = "deterministic-test-embedding"

    async def embed(self, text: str) -> tuple[float, ...]:
        return deterministic_embedding(text)


class InMemoryKnowledgeStore:
    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self._chunks: list[KnowledgeChunk] = []
        self._embedding_provider = embedding_provider or DeterministicEmbeddingProvider()
        self._embedding_dimension: int | None = None

    async def add_document(
        self,
        *,
        tenant_id: UUID,
        acl_users: frozenset[UUID],
        source_uri: str,
        version: str,
        text: str,
    ) -> tuple[KnowledgeChunk, ...]:
        document_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunks: list[KnowledgeChunk] = []
        for heading, body in _chunk_document(text):
            embedding = self._prepare_embedding(await self._embedding_provider.embed(body))
            chunks.append(
                KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    acl_users=acl_users,
                    source_uri=source_uri,
                    version=version,
                    document_sha256=document_sha256,
                    heading=heading,
                    text=body,
                    embedding=embedding,
                    embedding_logical_model=self._embedding_provider.logical_model,
                    prompt_injection_risk=_prompt_injection_risk(body),
                )
            )
        self._chunks.extend(chunks)
        return tuple(chunks)

    def chunks(self) -> tuple[KnowledgeChunk, ...]:
        return tuple(self._chunks)

    async def embed_query(self, query: str) -> tuple[float, ...]:
        return self._prepare_embedding(await self._embedding_provider.embed(query))

    def _prepare_embedding(self, embedding: tuple[float, ...]) -> tuple[float, ...]:
        if not embedding:
            raise ValueError("embedding must not be empty")
        if not all(math.isfinite(value) for value in embedding):
            raise ValueError("embedding values must be finite")
        if self._embedding_dimension is None:
            self._embedding_dimension = len(embedding)
        if len(embedding) != self._embedding_dimension:
            raise ValueError("embedding dimension mismatch")
        norm = sum(value * value for value in embedding) ** 0.5
        if norm == 0:
            return embedding
        return tuple(value / norm for value in embedding)


def deterministic_embedding(text: str) -> tuple[float, ...]:
    buckets = [0.0] * 16
    for term in terms(text):
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        buckets[int.from_bytes(digest[:2], "big") % len(buckets)] += 1.0
    norm = sum(value * value for value in buckets) ** 0.5
    if norm == 0:
        return tuple(buckets)
    return tuple(value / norm for value in buckets)


def terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.casefold()))


def _chunk_document(text: str) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    heading = "document"
    buffer: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if buffer:
                chunks.append((heading, " ".join(buffer)))
                buffer = []
            heading = line.lstrip("#").strip() or "section"
            continue
        buffer.append(line)
        if sum(len(part) for part in buffer) > 700:
            chunks.append((heading, " ".join(buffer)))
            buffer = []
    if buffer:
        chunks.append((heading, " ".join(buffer)))
    return chunks or [("document", text.strip())]


def _prompt_injection_risk(text: str) -> bool:
    return (
        re.search(
            r"\b(ignore previous|system prompt|developer message|reveal secrets)\b",
            text,
            re.IGNORECASE,
        )
        is not None
    )
