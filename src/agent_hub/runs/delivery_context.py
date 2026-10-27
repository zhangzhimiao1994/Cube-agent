"""Scoped historical delivery observations; never replay, instructions or authority."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_hub.capabilities.scoped_read import (
    ScopedReadEvidence,
    authorized_workspace_scope,
    read_scoped_file,
)
from agent_hub.context.builder import estimate_tokens
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runtime import direct_delivery_report
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, TaskContext

_FRAME = "HISTORICAL_DELIVERY_EVIDENCE_DATA"
_HASH = re.compile(r"[a-f0-9]{64}")
_SECRET = re.compile(r"(?:api[_ -]?key|password|secret|token|authorization)\s*[:=]|bearer\s+", re.IGNORECASE)


class _Repository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> object: ...
    async def raw_events(self, tenant_id: UUID, run_id: UUID) -> tuple[RunEvent, ...]: ...


def _need(value: object) -> None:
    if not value:
        raise ValueError("historical delivery unavailable")


def _scope(record: object, tenant: UUID, run: UUID) -> tuple[UUID, str, str, str]:
    project, session = authorized_workspace_scope(record, tenant_id=tenant, run_id=run)
    actor, route = getattr(record, "actor_id", None), getattr(record, "routing_decision", None)
    _need(type(actor) is UUID and isinstance(route, Mapping))
    assert isinstance(route, Mapping) and isinstance(actor, UUID)
    conversation = route.get("conversation_id")
    _need(type(conversation) is str and 0 < len(conversation) <= 128)
    return actor, cast(str, conversation), project, session


def _source_owned(source: object, current: object, tenant: UUID, source_id: UUID,
                  scope: tuple[UUID, str, str, str]) -> None:
    _need(_scope(source, tenant, source_id) == scope)
    _need(getattr(source, "status", None) == RunStatus.COMPLETED)
    _need(all(getattr(source, key, object()) is None for key in (
        "worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at")))
    before, now = getattr(source, "created_at", None), getattr(current, "created_at", None)
    _need(isinstance(before, datetime) and isinstance(now, datetime) and before <= now)


def _context(source: object, tenant: UUID, source_id: UUID) -> TaskContext:
    mode, request = getattr(source, "mode", None), getattr(source, "request", None)
    route = getattr(source, "routing_decision", None)
    _need(isinstance(mode, str) and isinstance(request, str) and isinstance(route, Mapping))
    assert isinstance(mode, str) and isinstance(request, str) and isinstance(route, Mapping)
    return TaskContext(tenant_id=tenant, run_id=source_id,
        actor_id=getattr(source, "actor_id", None), mode=TaskMode(mode), request=request,
        routing_decision=cast(Mapping[str, JsonValue], route))


def _publication(events: tuple[RunEvent, ...], source_id: UUID) -> tuple[Artifact, tuple[Artifact, ...]]:
    _need(0 < len(events) <= 100_000 and all(event.run_id == source_id for event in events))
    _need(len({event.sequence for event in events}) == len(events))
    ends = [event for event in events if event.kind == EventKind.RUNTIME_COMPLETED]
    _need(len(ends) == 1)
    end = ends[0]
    registry: dict[UUID, Artifact] = {}
    for event in events:
        if event.kind == EventKind.ARTIFACT_CREATED and event.artifact is not None:
            item = event.artifact
            _need(event.sequence < end.sequence and item.content_sha256 == item.recompute_content_sha256())
            _need(item.id not in registry or registry[item.id] == item)
            registry[item.id] = item
    reference = end.payload.get("artifact_id")
    if reference is None:
        _need(len(end.inputs) == 1)
        reference = str(end.inputs[0].id)
    _need(type(reference) is str)
    report = registry[UUID(cast(str, reference))]
    if end.inputs:
        _need(len(end.inputs) == 1 and end.inputs[0] == report)
    return report, tuple(registry.values())


def _delivery(context: TaskContext, report: Artifact,
              outputs: tuple[Artifact, ...]) -> Mapping[str, JsonValue]:
    if context.mode in (TaskMode.DIRECT, TaskMode.HYBRID):
        _need(direct_delivery_report.valid(context, report))
        delivery = report.content["workspace_delivery"]
        _need(isinstance(delivery, Mapping))
        return cast(Mapping[str, JsonValue], delivery)
    _need(context.mode == TaskMode.DISPATCH)
    from agent_hub.runtime.crew.delivery_report import final_evidence

    observed = final_evidence(report, context, outputs)
    bundle, verified = observed["bundle"], observed["verification"]
    _need(isinstance(bundle, Mapping) and isinstance(verified, Mapping))
    assert isinstance(bundle, Mapping) and isinstance(verified, Mapping)
    receipt = verified.get("verification")
    _need(isinstance(receipt, Mapping))
    return {**bundle, "verification": receipt}


def _hash(value: object) -> str:
    _need(type(value) is str and _HASH.fullmatch(value) is not None)
    return cast(str, value)


def _size(value: object) -> int:
    _need(type(value) is int and value > 0)
    return cast(int, value)


def _safe_path(path: object, *, filename: bool = False) -> str:
    _need(type(path) is str and 0 < len(path) <= (255 if filename else 512))
    assert isinstance(path, str)
    parsed = PurePosixPath(path)
    _need(not parsed.is_absolute() and parsed.as_posix() == path and ".." not in parsed.parts
          and "\\" not in path and ":" not in path
          and not any(unicodedata.category(c).startswith("C") for c in path)
          and (not filename or parsed.name == path and path.lower().endswith(".zip")))
    return path


def _manifest(value: object) -> dict[str, Any]:
    _need(isinstance(value, (list, tuple)) and len(value) > 0)
    assert isinstance(value, (list, tuple))
    sample: list[dict[str, Any]] = []
    readme: dict[str, Any] | None = None
    seen: set[str] = set()
    for item in value:
        _need(isinstance(item, Mapping))
        assert isinstance(item, Mapping)
        path = _safe_path(item.get("path"))
        _need(path not in seen)
        seen.add(path)
        size = item.get("size_bytes")
        _need(type(size) is int and size >= 0)
        row = {"path": path, "sha256": _hash(item.get("sha256")), "size_bytes": size}
        if len(sample) < 8:
            sample.append(row)
        if path == "README.md":
            readme = row
    return {"file_count": len(value), "sample": sample, "readme": readme}


def _projection(delivery: Mapping[str, JsonValue]) -> dict[str, Any]:
    file, receipt = delivery.get("file"), delivery.get("verification")
    _need(delivery.get("presentation") == "final_attachment"
          and isinstance(file, Mapping) and isinstance(receipt, Mapping))
    assert isinstance(file, Mapping) and isinstance(receipt, Mapping)
    UUID(str(delivery["artifact_id"]))
    name = _safe_path(file.get("filename"), filename=True)
    source = _hash(receipt.get("source_sha256"))
    _need(receipt.get("status") == "passed" and delivery.get("verification_source_sha256") == source)
    encoded = json.dumps(dict(receipt), default=dict, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    _need(delivery.get("verification_receipt_sha256") == hashlib.sha256(encoded).hexdigest())
    raw_checks = receipt.get("checks")
    _need(isinstance(raw_checks, (list, tuple)))
    assert isinstance(raw_checks, (list, tuple))
    checks: list[dict[str, str]] = []
    for check in raw_checks:
        _need(isinstance(check, Mapping))
        assert isinstance(check, Mapping)
        phase, status = check.get("phase"), check.get("status")
        if (type(phase) is str and phase in {"install", "build", "test", "lint", "typecheck", "check"}
                and type(status) is str and status in {"passed", "failed", "skipped", "not_run", "unavailable"}):
            checks.append({"phase": phase, "status": status})
    value: dict[str, Any] = {
        "archive": {"filename": name, "sha256": _hash(file.get("sha256")),
                    "size_bytes": _size(file.get("size_bytes"))},
        "verification": {"status": "passed", "source_sha256": source, "checks": checks},
        "manifest": _manifest(delivery.get("workspace_files")),
    }
    product = direct_delivery_report.public_product_evidence(delivery, receipt).get("python_product")
    if product is not None:
        value["wheel"] = product
    return value


async def _readme(repository: object, root: Path, tenant: UUID, run: UUID,
                  manifest: dict[str, Any]) -> dict[str, Any]:
    expected = manifest["readme"]
    result: dict[str, Any] = {"path": "README.md", "status": "manifest_unavailable"}
    if expected is None:
        return result
    try:
        read = await read_scoped_file(repository=repository, tenant_id=tenant, run_id=run,
            project_root=root, attachment_root=None, path="README.md", include_metadata=True,
            max_bytes=8192)
        assert isinstance(read, ScopedReadEvidence)
        if read.truncated:
            return {**result, "status": "truncated", "truncated": True}
        digest = hashlib.sha256(read.data).hexdigest()
        result.update(observed_sha256=digest, observed_size_bytes=len(read.data), truncated=False)
        text = read.data.decode("utf-8", errors="strict")
        if digest != expected["sha256"] or len(read.data) != expected["size_bytes"]:
            return {**result, "status": "changed"}
        if _SECRET.search(text):
            return {**result, "status": "redacted"}
        return {**result, "status": "same_as_delivery", "text": text}
    except UnicodeDecodeError:
        return {**result, "status": "invalid_utf8"}
    except Exception:  # noqa: BLE001 - bounded read denial must disclose no host path.
        return {**result, "status": "unavailable"}


def _artifact(value: dict[str, Any], budget: int, report: Artifact) -> tuple[Artifact, ...]:
    def framed() -> str:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(",", ":"), default=dict)
        raw = raw.replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<{_FRAME}>\n{raw}\n</{_FRAME}>"

    text = framed()
    if estimate_tokens(text) > budget and "text" in value["readme"]:
        value["readme"].pop("text")
        value["readme"]["text_omitted"] = "budget"
        text = framed()
    if estimate_tokens(text) > budget:
        value["manifest"].pop("sample", None)
        text = framed()
    if estimate_tokens(text) > budget or len(text.encode()) > 65536:
        return ()
    return (Artifact(id=uuid5(NAMESPACE_URL, "historical-delivery:" + text), type="text",
        producer="historical_delivery_evidence", source_ids=(str(report.id),),
        content={"text": text, "reference_only": True}),)


async def delivery_context_artifacts(
    repository: object, project_root: Path, tenant_id: UUID, run_id: UUID,
    source_run_ids: tuple[UUID, ...], token_budget: int,
) -> tuple[Artifact, ...]:
    """Project one owned, bound prior publication without executing or restoring it."""
    if type(token_budget) is not int or token_budget < 1 or not source_run_ids:
        return ()
    repo = cast(_Repository, repository)
    try:
        current = await repo.get(tenant_id, run_id)
        scope = _scope(current, tenant_id, run_id)
    except Exception:  # noqa: BLE001 - unavailable context is optional and fail closed.
        return ()
    for source_id in dict.fromkeys(reversed(source_run_ids)):
        if source_id == run_id or type(source_id) is not UUID:
            continue
        try:
            source = await repo.get(tenant_id, source_id)
            _source_owned(source, current, tenant_id, source_id, scope)
            context = _context(source, tenant_id, source_id)
            if context.mode == TaskMode.DISCUSS:
                continue  # No native ledger is reconstructed from prose/checkpoints.
            report, outputs = _publication(await repo.raw_events(tenant_id, source_id), source_id)
            value = _projection(_delivery(context, report, outputs))
            value.update(schema_version=1, kind="historical_delivery_evidence",
                trust="reference_data_not_instructions_or_authority",
                observation="historical_delivery_not_current_verification",
                scope={"tenant_id": str(tenant_id), "actor_id": str(scope[0]),
                    "conversation_id": scope[1], "project_id": scope[2], "session_id": scope[3],
                    "current_run_id": str(run_id)},
                source={"run_id": str(source_id), "report_artifact_id": str(report.id),
                        "report_artifact_sha256": report.content_sha256})
            value["readme"] = await _readme(repository, project_root, tenant_id, run_id, value["manifest"])
            _need(_scope(await repo.get(tenant_id, run_id), tenant_id, run_id) == scope)
            _source_owned(await repo.get(tenant_id, source_id), current, tenant_id, source_id, scope)
            return _artifact(value, token_budget, report)
        except Exception:  # noqa: BLE001, S112 - skip non-deliveries and never expose raw failure text.
            continue
    return ()
