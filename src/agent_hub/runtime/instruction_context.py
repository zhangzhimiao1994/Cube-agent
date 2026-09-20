"""Bounded project guidance loaded by the service, never reconstructed from model output."""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_hub.capabilities.scoped_read import (
    ScopedReadError,
    ScopedReadEvidence,
    authorized_workspace_scope,
    read_scoped_file,
)
from agent_hub.models.types import ModelRequest

MAX_SOURCE_BYTES = 8192
_PATHS: tuple[Literal["AGENTS.md", "SKILL.md"], ...] = ("AGENTS.md", "SKILL.md")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _request_json(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _request_json(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {key: _request_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_request_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def model_request_sha256(request: ModelRequest) -> str:
    """Hash the full submitted request, separately from runtime-specific replay digests."""
    return _sha(json.dumps(_request_json(request), ensure_ascii=False, allow_nan=False,
                           sort_keys=True, separators=(",", ":")).encode("utf-8"))


class InstructionSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              hide_input_in_errors=True, revalidate_instances="always")
    path: Literal["AGENTS.md", "SKILL.md"]
    kind: Literal["project_guidance"] = "project_guidance"
    status: Literal["loaded", "unavailable", "invalid_utf8"]
    project_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    session_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    read_bytes: int = Field(default=0, ge=0, le=MAX_SOURCE_BYTES + 1)
    read_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content_bytes: int = Field(default=0, ge=0, le=MAX_SOURCE_BYTES)
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    truncated: bool = False
    text: str | None = Field(default=None, repr=False, exclude=True)

    @model_validator(mode="after")
    def valid_evidence(self) -> Self:
        if self.status == "loaded":
            if self.text is None or self.project_id is None or self.session_id is None:
                raise ValueError("loaded instruction lacks scoped content")
            encoded = self.text.encode("utf-8")
            if len(encoded) != self.content_bytes or _sha(encoded) != self.content_sha256:
                raise ValueError("instruction content evidence mismatch")
            if self.read_sha256 is None or self.content_bytes > self.read_bytes:
                raise ValueError("instruction read evidence mismatch")
            if self.truncated:
                if self.file_sha256 is not None or self.read_bytes != MAX_SOURCE_BYTES + 1:
                    raise ValueError("truncated read cannot claim a full-file digest")
            elif self.file_sha256 != self.read_sha256 or self.read_sha256 != self.content_sha256:
                raise ValueError("complete read digest mismatch")
        elif self.text is not None or self.read_bytes or self.content_bytes or any((
            self.read_sha256, self.file_sha256, self.content_sha256, self.truncated,
        )):
            raise ValueError("unavailable instruction cannot claim successful reading")
        return self


class InstructionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              hide_input_in_errors=True, revalidate_instances="always")
    load_id: UUID
    tenant_id: UUID
    run_id: UUID
    sources: tuple[InstructionSource, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def unique_scope(self) -> Self:
        if len({source.path for source in self.sources}) != len(self.sources):
            raise ValueError("instruction paths must be unique")
        scopes = {(source.project_id, source.session_id) for source in self.sources
                  if source.status == "loaded"}
        if len(scopes) > 1:
            raise ValueError("instruction session changed during loading")
        return self

    def metadata(self) -> dict[str, object]:
        return {"schema_version": 1, "load_id": str(self.load_id),
                "stage": "session_load", "actor": "context_loader",
                "tenant_id": str(self.tenant_id), "run_id": str(self.run_id),
                "source": "session_root", "sources": [item.model_dump() for item in self.sources]}

    def authorized_for(self, record: object) -> bool:
        try:
            scope = authorized_workspace_scope(record, tenant_id=self.tenant_id, run_id=self.run_id)
        except ScopedReadError:
            return False
        return all((item.project_id, item.session_id) == scope
                   for item in self.sources if item.status == "loaded")

    def render(self) -> str:
        sources = [{"path": item.path, "kind": item.kind, "text": item.text,
                    "truncated": item.truncated}
                   for item in self.sources if item.status == "loaded" and item.text]
        if not sources:
            return ""
        encoded = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "<PROJECT_GUIDANCE_JSON>" + encoded.replace("<", "\\u003c").replace(
            ">", "\\u003e"
        ) + "</PROJECT_GUIDANCE_JSON>"

    def injection_metadata(
        self, *, logical_model: str, request_sha256: str,
        stage: Literal["direct", "dispatch_step", "dispatch_review"] = "direct",
        actor: str = "main_agent",
    ) -> dict[str, object]:
        return {"schema_version": 1, "load_id": str(self.load_id),
                "stage": stage, "actor": actor,
                "tenant_id": str(self.tenant_id), "run_id": str(self.run_id),
                "boundary": "model_gateway", "logical_model": logical_model,
                "request_sha256": request_sha256,
                "sources": [{"path": item.path, "injected_bytes": item.content_bytes,
                             "injected_sha256": item.content_sha256, "truncated": item.truncated}
                            for item in self.sources if item.status == "loaded" and item.text]}


class InstructionContextLoader:
    def __init__(self, *, repository: object, project_root: Path) -> None:
        self._repository = repository
        self._project_root = project_root

    async def load(self, *, tenant_id: UUID, run_id: UUID) -> InstructionContext:
        sources: list[InstructionSource] = []
        for path in _PATHS:
            try:
                result = await read_scoped_file(
                    repository=self._repository, tenant_id=tenant_id, run_id=run_id,
                    project_root=self._project_root, attachment_root=None, path=path,
                    include_metadata=True, max_bytes=MAX_SOURCE_BYTES,
                )
                assert isinstance(result, ScopedReadEvidence)
                # Only an incomplete final UTF-8 code point may be dropped from a bounded prefix.
                decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
                text = decoder.decode(result.data[:MAX_SOURCE_BYTES], final=not result.truncated)
                source = InstructionSource(
                    path=path, status="loaded", project_id=result.project_id,
                    session_id=result.session_id, text=text, read_bytes=len(result.data),
                    read_sha256=_sha(result.data),
                    file_sha256=None if result.truncated else _sha(result.data),
                    content_bytes=len(text.encode("utf-8")), content_sha256=_sha(text.encode("utf-8")),
                    truncated=result.truncated,
                )
            except ScopedReadError:
                source = InstructionSource(path=path, status="unavailable")
            except UnicodeDecodeError:
                source = InstructionSource(path=path, status="invalid_utf8")
            sources.append(source)
        if len({(item.project_id, item.session_id) for item in sources if item.status == "loaded"}) > 1:
            sources = [InstructionSource(path=path, status="unavailable") for path in _PATHS]
        return InstructionContext(load_id=uuid4(), tenant_id=tenant_id, run_id=run_id,
                                  sources=tuple(sources))
