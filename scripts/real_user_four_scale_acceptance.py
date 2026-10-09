#!/usr/bin/env python3
"""Run the four project scales through the same HTTP path as a logged-in user."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from uuid import UUID, uuid4

from agent_hub.harness.browser_evidence import validate_case_browser_bundle
from agent_hub.harness.project_scale import (
    PROJECT_SCALE_TIERS,
    ProjectScaleRunPlan,
    ProjectScaleRunRequest,
    build_project_scale_run_plan,
)
from agent_hub.harness.project_scale_runner import (
    AcceptanceClient,
    AcceptanceHTTPError,
    ProjectScaleCaseResult,
    UrllibAcceptanceClient,
    _acceptance_credentials_from_env,
    _deliverable_repair_idempotency_key,
    _effective_execute_wait_seconds,
    _idempotency_key,
    _safe_workspace_session_token,
    _safe_zip_member_path,
    execute_project_scale_plan,
)
from agent_hub.harness.submission_journal import SubmissionJournal
from agent_hub.models.failure_receipt import GatewayFailureReceipt
from agent_hub.runtime.contracts import Artifact, GatewayProvenance

_MODE_CAPABILITIES = ("direct", "dispatch", "hybrid", "multi_agent")
_ROUTE_INTENTS = ("auto", *_MODE_CAPABILITIES)
_ACCEPTANCE_CASES = (
    *(("auto_scale", scale, "auto", f"auto-{scale}") for scale in PROJECT_SCALE_TIERS),
    *(
        (
            "mode_capability",
            scale,
            mode,
            f"mode-{scale}-{mode.replace('_', '-')}",
        )
        for scale in PROJECT_SCALE_TIERS
        for mode in _MODE_CAPABILITIES
    ),
)
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_ID_RE = re.compile(r"[^a-z0-9-]+")
_SAFE_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
# Observe the existing service fuse without imposing an operator limit on the request.
_REAL_USER_RUNTIME_OBSERVATION_BUDGET_SECONDS = 3600.0
_ADMIN_RUN_PREFIX = "/api/v1/admin/runs"
_MAX_PREVIEW_ASSETS = 24
_MAX_MODEL_ARTIFACT_BYTES = 2_100_000
_MODEL_SCOPE_PAYLOAD_KEYS = (
    "logical_model", "requested_logical_model", "attempted_logical_models",
    "artifact_id", "deployment", "provider", "upstream_model", "artifact_origin",
    "model_scope_origin",
)
_WEBSITE_DELIVERABLE_REQUIREMENT = (
    " Also include a complete interactive website for the project. Put a self-contained "
    "preview.html or index.html entrypoint in the workspace with usable navigation and at least "
    "one real interaction backed by the generated project. Do not return a placeholder preview."
)


class _PreviewAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {name.casefold(): value for name, value in attrs}
        reference: str | None = None
        if tag.casefold() in {"script", "img", "source", "video", "audio"}:
            reference = values.get("src")
        elif tag.casefold() == "link":
            rel = (values.get("rel") or "").casefold().split()
            if any(value in {"stylesheet", "icon", "preload", "modulepreload"} for value in rel):
                reference = values.get("href")
        if reference:
            self.references.append(reference)


def _preview_asset_paths(html: bytes, preview_url: str) -> tuple[str, ...]:
    try:
        decoded = html.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    parser = _PreviewAssetParser()
    parser.feed(decoded)
    prefix = preview_url if preview_url.endswith("/") else f"{preview_url}/"
    paths: list[str] = []
    for reference in parser.references:
        if reference.startswith(("data:", "blob:", "mailto:", "javascript:", "#", "//")):
            continue
        resolved = urljoin(prefix, reference)
        parsed = urlsplit(resolved)
        if parsed.scheme or parsed.netloc or not resolved.startswith(prefix):
            continue
        if resolved not in paths:
            paths.append(resolved)
        if len(paths) >= _MAX_PREVIEW_ASSETS:
            break
    return tuple(paths)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RealUserAcceptanceClient:
    """Audit HTTP use and make the runner's admin-run fallback impossible."""

    def __init__(self, delegate: AcceptanceClient) -> None:
        self._delegate = delegate
        self.request_log: list[str] = []
        self.blocked_admin_run_requests: list[str] = []
        self.submitted_run_ids: list[str] = []
        self.accepted_repair_run_ids: list[str] = []
        self.submission_journal: SubmissionJournal | None = None
        self._submission_context: dict[str, object] | None = None
        self._persist_submission: Callable[[], None] | None = None
        self._submission_reconciliation_failed = False

    def configure_submission_journal(
        self, journal: SubmissionJournal, persist: Callable[[], None],
    ) -> None:
        self.submission_journal = journal
        self._persist_submission = persist

    def set_submission_context(self, context: dict[str, object]) -> None:
        self._submission_context = copy.deepcopy(context)

    def require_resolved_submissions(self) -> None:
        if self.submission_journal is not None and self.submission_journal.has_unresolved:
            raise ValueError("unresolved submission: automatic paid-request replay is forbidden")
        if self._submission_reconciliation_failed:
            raise ValueError("confirmed submission requires successful read-only reconciliation")

    def _request_run(
        self, path: str, body: dict[str, object] | None, idempotency_key: str | None,
    ) -> dict[str, object] | list[object]:
        journal, persist = self.submission_journal, self._persist_submission
        if journal is None or persist is None or self._submission_context is None:
            raise ValueError("paid submissions require a durable submission journal")
        self.require_resolved_submissions()
        if path == "/api/v1/runs":
            if body is None or any(
                body.get(key) != self._submission_context[key]
                for key in ("project_id", "conversation_id", "workspace_session_id")
            ):
                raise ValueError("submission request does not match the current case scope")
            identity = cast(Mapping[str, object], journal.snapshot()["identity"])
            profile = identity.get("model_profile")
            if isinstance(profile, Mapping) and (
                body.get("direct_model") != profile.get("direct_model")
                or not isinstance(body.get("allowed_models"), (tuple, list))
                or list(cast(Sequence[object], body["allowed_models"])) != profile.get("allowed_models")
            ):
                raise ValueError("submission request does not match the selected model profile")
        index, confirmed = journal.prepare(
            path=path, body=json.loads(json.dumps(body or {}, allow_nan=False)),
            idempotency_key=idempotency_key,
            context=self._submission_context, persist=persist,
        )
        if confirmed is not None:
            return self.read_confirmed_submission(confirmed)
        response = self._delegate.request_json(
            "POST", path, body=body, idempotency_key=idempotency_key,
        )
        if not isinstance(response, dict):
            raise ValueError("unresolved submission: response is not an object")  # noqa: TRY004
        journal.confirm(index, response, persist)
        return response

    def read_confirmed_submission(self, confirmed: Mapping[str, object]) -> dict[str, object]:
        try:
            observed = self.request_json(
                "GET", f"/api/v1/runs/{quote(str(confirmed['id']), safe='')}",
            )
            if (
                not isinstance(observed, dict)
                or observed.get("id") != confirmed["id"]
                or observed.get("tenant_id") != confirmed["tenant_id"]
                or not isinstance(observed.get("status"), str) or not observed["status"]
            ):
                raise ValueError("confirmed submission read-back identity is inconsistent")
            for key in ("project_id", "conversation_id", "workspace_session_id"):
                if key in observed and observed[key] != confirmed.get(key):
                    raise ValueError("confirmed submission read-back scope is inconsistent")
            return {**confirmed, **observed}
        except BaseException:
            self._submission_reconciliation_failed = True
            raise

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        self._record(method, path)
        if _is_admin_run_path(path):
            blocked = f"{method.upper()} {path}"
            self.blocked_admin_run_requests.append(blocked)
            raise RuntimeError(f"{blocked} is forbidden as acceptance evidence")
        is_submission = method.upper() == "POST" and (
            path == "/api/v1/runs" or re.fullmatch(r"/api/v1/runs/[^/]+/accept-repair", path)
        )
        if is_submission:
            try:
                response = self._request_run(path, body, idempotency_key)
            except BaseException:
                self._submission_reconciliation_failed = True
                raise
        else:
            response = self._delegate.request_json(
                method, path, body=body, idempotency_key=idempotency_key,
            )
        if method.upper() == "POST" and isinstance(response, dict):
            run_id = response.get("id")
            if isinstance(run_id, str) and run_id.strip():
                if path == "/api/v1/runs":
                    self.submitted_run_ids.append(run_id)
                elif re.fullmatch(r"/api/v1/runs/[^/]+/accept-repair", path):
                    self.accepted_repair_run_ids.append(run_id)
        return response

    def request_bytes(self, method: str, path: str) -> bytes:
        self._record(method, path)
        if _is_admin_run_path(path):
            blocked = f"{method.upper()} {path}"
            self.blocked_admin_run_requests.append(blocked)
            raise RuntimeError(f"{blocked} is forbidden as acceptance evidence")
        return self._delegate.request_bytes(method, path)

    def _record(self, method: str, path: str) -> None:
        self.request_log.append(f"{method.upper()} {path}")


def _is_admin_run_path(path: str) -> bool:
    normalized = urlsplit(path).path.rstrip("/")
    return normalized == _ADMIN_RUN_PREFIX or normalized.startswith(f"{_ADMIN_RUN_PREFIX}/")


def build_real_user_scale_plan(
    *,
    scale: str,
    project_id: str,
    project_label: str,
    conversation_id: str,
    workspace_session_id: str,
    route_intent: str,
    logical_model: str | None = None,
    defer_preview: bool = False,
) -> ProjectScaleRunPlan:
    """Build one natural AUTO scale case or one explicit mode capability case."""

    if type(defer_preview) is not bool:
        raise TypeError("defer_preview must be a boolean")
    profile = _model_profile(logical_model)
    if route_intent not in _ROUTE_INTENTS:
        raise ValueError(f"unknown real-user route intent: {route_intent}")

    base_flow = "artifact_production" if route_intent == "auto" else route_intent
    base = build_project_scale_run_plan(
        scales=(scale,),
        flows=(base_flow,),
        execute=True,
        benchmark_kind="capability",
    )
    request = base.requests[0]
    body = dict(request.body)
    body.pop("runtime_timeout_seconds", None)
    body.update(
        {
            "mode": "auto" if route_intent == "auto" else body["mode"],
            "project_id": project_id,
            "project_label": project_label,
            "conversation_id": conversation_id,
            "workspace_session_id": workspace_session_id,
            "message": body["message"] if defer_preview else f"{body['message']}{_WEBSITE_DELIVERABLE_REQUIREMENT}",
        }
    )
    if profile is not None:
        body.update(direct_model=logical_model, allowed_models=(logical_model,))
    scoped_request = ProjectScaleRunRequest(
        case_id=f"{scale}:{route_intent}",
        body=body,
        validation_focus=request.validation_focus,
    )
    return replace(base, requests=(scoped_request,))


def _model_profile(logical_model: str | None) -> dict[str, object] | None:
    if logical_model is None:
        return None
    if not isinstance(logical_model, str) or _SAFE_MODEL_RE.fullmatch(logical_model) is None:
        raise ValueError("logical_model must be a safe logical model identifier")
    return {"direct_model": logical_model, "allowed_models": [logical_model]}


def _report_model_profile(report: Mapping[str, object]) -> dict[str, object] | None:
    profile = report.get("model_profile")
    if profile is None:
        return None
    if not isinstance(profile, Mapping) or not isinstance(profile.get("direct_model"), str):
        raise TypeError("model profile must specify a safe logical model")
    try:
        expected = _model_profile(cast(str, profile["direct_model"]))
    except ValueError as error:
        raise ValueError("model profile must specify a safe logical model") from error
    if profile != expected:
        raise ValueError("model profile must contain exactly one matching allowed model")
    return expected


def _validate_report_model_profile(
    report: Mapping[str, object], expected: Mapping[str, object] | None
) -> None:
    if _report_model_profile(report) != expected:
        raise ValueError("model profile does not match this execution")
    identity = report.get("execution_identity")
    if expected is not None and not isinstance(identity, Mapping):
        raise ValueError("model profile requires a scoped execution identity")
    if isinstance(identity, Mapping) and identity.get("model_profile") != expected:
        raise ValueError("execution identity model profile is inconsistent")
    for section in ("cases", "attempt_history"):
        items = report.get(section, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and item.get("model_profile") != expected:
                    raise ValueError(
                        f"{section} model profile is inconsistent; cannot relabel runs"
                    )


def _model_artifact_hashes(raw: Mapping[str, object]) -> tuple[str, str, bool | None]:
    original = raw.get("content_sha256")
    if not isinstance(original, str) or _SHA256_RE.fullmatch(original) is None:
        raise ValueError("invalid model artifact digest")
    if "public_content_sha256" not in raw and "content_redacted" not in raw:
        return original, original, None
    public, redacted = raw.get("public_content_sha256"), raw.get("content_redacted")
    if (
        not isinstance(public, str) or _SHA256_RE.fullmatch(public) is None
        or type(redacted) is not bool or (original != public) is not redacted
    ):
        raise ValueError("invalid model artifact projection metadata")
    return original, public, redacted


def _acceptance_model_scope_content(artifact: Artifact, run_id: str) -> dict[str, object]:
    # The runtime's shared validator requires one model. Acceptance may be unselected,
    # so validate the original fallback identities here, without rewriting the proof.
    content = artifact.to_payload()["content"]
    keys = {
        "schema_version", "source", "run_id", "tenant_id", "requested_logical_model",
        "history_complete", "call_count", "calls", "scope_id", "part_index", "part_count",
        "call_offset",
    }
    if (
        artifact.type != "model_attempt" or artifact.producer != "main_agent"
        or artifact.source_ids or artifact.provenance is None
        or not isinstance(content, dict) or set(content) != keys
    ):
        raise ValueError("model attempt scope envelope is invalid")
    model = content["requested_logical_model"]
    if (
        type(content["schema_version"]) is not int or content["schema_version"] != 1
        or content["source"] != "direct_runtime" or str(UUID(run_id)) != run_id
        or content["run_id"] != run_id or not isinstance(model, str)
        or content["history_complete"] is not True
        or type(content["call_count"]) is not int or content["call_count"] < 1
        or not isinstance(content["calls"], list) or not content["calls"]
        or content["call_count"] < len(content["calls"])
        or type(content["part_index"]) is not int or type(content["part_count"]) is not int
        or not 1 <= content["part_index"] <= content["part_count"] <= content["call_count"]
        or type(content["call_offset"]) is not int or content["call_offset"] < 0
        or content["call_offset"] + len(content["calls"]) > content["call_count"]
    ):
        raise ValueError("model attempt scope content is invalid")
    for key in ("tenant_id", "scope_id"):
        if not isinstance(content[key], str) or str(UUID(content[key])) != content[key]:
            raise ValueError("model attempt scope identity is invalid")
    seen: set[str] = set()
    final_provenance: GatewayProvenance | None = None
    for call in content["calls"]:
        if not isinstance(call, dict) or set(call) != {"outcome", "receipt"}:
            raise ValueError("model attempt call is invalid")
        if call["outcome"] == "failed":
            failure = GatewayFailureReceipt.from_payload(call["receipt"])
            if not failure.history_complete:
                raise ValueError("model attempt failure history is incomplete")
            positions = {model: index for index, model in enumerate(failure.attempted_logical_models)}
            previous_position = -1
            for attempt in failure.attempts:
                position = positions[attempt.logical_model]
                # Skipped candidates and same-model deployment retries are valid.
                if position < previous_position:
                    raise ValueError("model attempt transport history reverses declared model order")
                previous_position = position
            receipt = cast(dict[str, object], failure.to_payload())
            final_provenance = GatewayProvenance.from_payload(
                failure.attempts[-1].to_payload()["provenance"]
            )
        elif call["outcome"] == "received":
            receipt = call["receipt"]
            if (
                not isinstance(receipt, dict) or set(receipt) != {
                    "schema_version", "source", "call_id", "requested_logical_model",
                    "allow_fallback", "attempted_logical_models", "provenance",
                }
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["source"] != "model_gateway"
                or type(receipt["allow_fallback"]) is not bool
            ):
                raise ValueError("model attempt received receipt is invalid")
            final_provenance = GatewayProvenance.from_payload(receipt["provenance"])
        else:
            raise ValueError("model attempt call outcome is unknown")
        attempts = receipt["attempted_logical_models"]
        call_id = receipt["call_id"]
        if (
            receipt["requested_logical_model"] != model
            or not isinstance(attempts, list) or not attempts or attempts[0] != model
            or not receipt["allow_fallback"] and any(item != model for item in attempts)
            or call["outcome"] == "received" and attempts[-1] != final_provenance.logical_model
            or not isinstance(call_id, str) or str(UUID(call_id)) != call_id or call_id in seen
        ):
            raise ValueError("model attempt receipt identities are inconsistent")
        for attempted in attempts:
            GatewayProvenance.from_payload({
                **final_provenance.to_payload(), "logical_model": attempted,
            })
        seen.add(call_id)
    if final_provenance != artifact.provenance:
        raise ValueError("model attempt final provenance is inconsistent")
    return cast(dict[str, object], content)


def _public_model_artifact(event: Mapping[str, object]) -> dict[str, object]:
    try:
        raw = event.get("artifact")
        if not isinstance(raw, Mapping):
            raise TypeError
        original_digest, public_digest, redacted = _model_artifact_hashes(raw)
        encoded = json.dumps(dict(raw), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_MODEL_ARTIFACT_BYTES:
            raise ValueError
        envelope = {
            key: value for key, value in raw.items()
            if key not in {"public_content_sha256", "content_redacted"}
        }
        envelope["content_sha256"] = public_digest
        artifact = Artifact.from_payload(envelope)
        provenance = artifact.provenance
        if provenance is None:
            raise ValueError
        proof: dict[str, object] = {
            "source": "public_event_artifact",
            "id": str(artifact.id),
            "type": artifact.type,
            "producer": artifact.producer,
            "version": artifact.version,
            "source_ids": list(artifact.source_ids),
            "content_sha256": original_digest,
            **({"public_content_sha256": public_digest, "content_redacted": redacted}
               if redacted is not None else {}),
            "hash_verified": True,
            "hash_verification_scope": "original" if redacted is None else "public_projection",
            "provenance": provenance.to_payload(),
        }
        if artifact.type == "model_attempt":
            if redacted is True:
                raise ValueError
            proof["scope_content"] = _acceptance_model_scope_content(
                artifact, _model_scope_source(event, artifact_id=str(artifact.id), digest=original_digest)[0]
            )
        return proof
    except Exception:  # noqa: BLE001 - contract errors can include model output.
        raise ValueError("public model artifact envelope/hash is invalid or unavailable") from None


def _valid_model_artifact_proof(event: Mapping[str, object]) -> bool:
    proof, payload = event.get("model_artifact"), event.get("payload")
    if not isinstance(proof, Mapping) or not isinstance(payload, Mapping):
        return False
    digest, producer = proof.get("content_sha256"), proof.get("producer")
    if (
        proof.get("source") != "public_event_artifact"
        or payload.get("artifact_origin") == "builtin_fixture"
        or proof.get("hash_verified") is not True
        or not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        or not isinstance(producer, str) or not producer.strip()
        or type(proof.get("version")) is not int or cast(int, proof["version"]) < 1
        or not isinstance(proof.get("source_ids"), list)
        or proof.get("id") != payload.get("artifact_id")
        or proof.get("type") not in {"model_response", "text", "tool_result"}
        or (proof.get("type") == "model_response" and event.get("actor") != producer)
    ):
        return False
    try:
        _, _, redacted = _model_artifact_hashes(proof)
        if proof.get("hash_verification_scope") != (
            "original" if redacted is None else "public_projection"
        ):
            return False
        provenance = GatewayProvenance.from_payload(proof.get("provenance"))
        # Validate the retained envelope identities without retaining its content.
        if str(UUID(cast(str, proof.get("id")))) != proof.get("id"):
            return False
        sources = cast(list[object], proof["source_ids"])
        if any(not isinstance(item, str) or str(UUID(item)) != item for item in sources):
            return False
        if len(set(cast(list[str], sources))) != len(sources):
            return False
    except Exception:  # noqa: BLE001 - evidence must fail closed without echoing inputs.
        return False
    attempts = payload.get("attempted_logical_models")
    return (
        payload.get("logical_model") == provenance.logical_model
        and isinstance(attempts, list) and bool(attempts)
        and all(isinstance(item, str) and bool(item) for item in attempts)
        and attempts[-1] == provenance.logical_model
        and (
            "requested_logical_model" not in payload
            or payload["requested_logical_model"] == attempts[0]
        )
        and all(
            key not in payload or payload[key] == value
            for key, value in (
                ("deployment", provenance.deployment_id), ("provider", provenance.provider_id),
                ("upstream_model", provenance.provider_model),
            )
        )
    )


def _has_direct_model_completion(events: list[object]) -> bool:
    starts: dict[str, tuple[int, object]] = {}
    artifacts: dict[tuple[str, str], tuple[int, Mapping[str, object]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        actor, sequence, payload = event.get("actor"), event.get("sequence"), event.get("payload")
        if not isinstance(actor, str) or type(sequence) is not int or not isinstance(payload, Mapping):
            continue
        if sequence < 1:
            continue
        kind = event.get("kind")
        if kind == "model.started":
            starts[actor] = (sequence, payload.get("logical_model"))
            continue
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or payload.get("artifact_origin") == "builtin_fixture":
            continue
        if kind == "artifact.created" and _valid_model_artifact_proof(event):
            start = starts.get(actor)
            if (
                start is not None and start[0] < sequence
                and start[1] == payload.get("requested_logical_model")
                and isinstance(start[1], str) and bool(start[1])
                and all(isinstance(payload.get(key), str) and payload[key]
                        for key in ("deployment", "provider", "upstream_model"))
            ):
                artifacts[actor, artifact_id] = (sequence, payload)
        elif kind == "runtime.completed":
            artifact = artifacts.get((actor, artifact_id))
            if artifact is not None and artifact[0] < sequence and all(
                key in payload and payload[key] == artifact[1].get(key)
                for key in ("logical_model", "requested_logical_model", "attempted_logical_models")
            ):
                return True
    return False


def _crew_model_start_matches(event: Mapping[str, object], events: list[object]) -> bool:
    starts = [
        item for item in events if isinstance(item, Mapping)
        and item.get("kind") == "model.started" and item.get("actor") == event.get("actor")
    ]
    if not starts:
        return True
    if any(
        type(start.get("sequence")) is not int or cast(int, start["sequence"]) < 1
        or not isinstance(start.get("payload"), Mapping) for start in starts
    ):
        return False
    preceding = [start for start in starts if start["sequence"] < event["sequence"]]
    if not preceding:
        return False
    latest = max(preceding, key=lambda start: cast(int, start["sequence"]))
    attempts = cast(Mapping[str, object], event["payload"])["attempted_logical_models"]
    return cast(Mapping[str, object], latest["payload"]).get("logical_model") == (
        cast(list[str], attempts)[0]
    )


def _model_scope_source(
    event: Mapping[str, object], *, artifact_id: str | None = None, digest: str | None = None,
) -> tuple[str, int]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError
    origin = payload.get("model_scope_origin")
    run_id, sequence = event.get("run_id"), event.get("sequence")
    if origin is not None:
        keys = {"schema_version", "source", "run_id", "sequence", "parent_run_id"}
        if artifact_id is not None:
            keys |= {"artifact_id", "content_sha256"}
        if (
            not isinstance(origin, Mapping) or set(origin) != keys
            or type(origin.get("schema_version")) is not int or origin["schema_version"] != 1
            or origin["source"] != "hybrid_runtime" or origin["parent_run_id"] != run_id
            or artifact_id is not None and (
                origin["artifact_id"] != artifact_id or origin["content_sha256"] != digest
            )
        ):
            raise ValueError
        run_id, sequence = origin["run_id"], origin["sequence"]
    if (not isinstance(run_id, str) or str(UUID(run_id)) != run_id
            or type(sequence) is not int or sequence < 1):
        raise ValueError
    return run_id, sequence


def _valid_model_scope_proof(event: Mapping[str, object]) -> bool:
    proof, payload = event.get("model_artifact"), event.get("payload")
    if not isinstance(proof, Mapping) or not isinstance(payload, Mapping):
        return False
    try:
        original, public, redacted = _model_artifact_hashes(proof)
        if (
            proof.get("source") != "public_event_artifact"
            or proof.get("hash_verified") is not True or redacted is True
            or proof.get("hash_verification_scope") != (
                "original" if redacted is None else "public_projection"
            )
            or original != public or event.get("actor") != "main_agent"
            or payload.get("artifact_origin") == "builtin_fixture"
            or proof.get("id") != payload.get("artifact_id")
        ):
            return False
        artifact = Artifact.from_payload({
            key: proof.get(key) for key in (
                "id", "type", "producer", "version", "source_ids", "provenance"
            )
        } | {"content": proof.get("scope_content"), "content_sha256": original})
        source_id, _ = _model_scope_source(event, artifact_id=str(artifact.id), digest=original)
        content = _acceptance_model_scope_content(artifact, source_id)
        provenance = artifact.provenance
        if provenance is None:
            return False
        calls = cast(list[Mapping[str, object]], content["calls"])
        attempts = list(dict.fromkeys(
            model for call in calls
            for model in cast(list[str], cast(Mapping[str, object], call["receipt"])[
                "attempted_logical_models"
            ])
        ))
        return all(payload.get(key) == value for key, value in (
            ("logical_model", provenance.logical_model),
            ("requested_logical_model", content["requested_logical_model"]),
            ("attempted_logical_models", attempts), ("deployment", provenance.deployment_id),
            ("provider", provenance.provider_id), ("upstream_model", provenance.provider_model),
        ))
    except Exception:  # noqa: BLE001 - untrusted report content must fail closed.
        return False


def _has_failed_attempt_scope(events: list[object]) -> bool:
    starts: dict[str, tuple[int, int, object]] = {}
    artifacts: dict[str, tuple[int, Mapping[str, object], Mapping[str, object], str, int, str]] = {}
    linked_ids: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        sequence, payload = event.get("sequence"), event.get("payload")
        if type(sequence) is not int or sequence < 1 or not isinstance(payload, Mapping):
            if event.get("kind") == "model.failure_receipt":
                return False
            continue
        if event.get("actor") != "main_agent":
            if event.get("kind") == "model.failure_receipt":
                return False
            continue
        if event.get("kind") == "model.started":
            try:
                source_id, source_sequence = _model_scope_source(event)
            except (TypeError, ValueError):
                continue
            starts[source_id] = (sequence, source_sequence, payload.get("logical_model"))
        elif event.get("kind") == "artifact.created" and _valid_model_scope_proof(event):
            proof = cast(Mapping[str, object], event["model_artifact"])
            source_id, source_sequence = _model_scope_source(
                event, artifact_id=cast(str, proof["id"]), digest=cast(str, proof["content_sha256"])
            )
            start = starts.get(source_id)
            if (start is not None and start[0] < sequence and start[1] < source_sequence
                    and start[2] == payload.get("requested_logical_model")):
                if cast(str, payload["artifact_id"]) in artifacts:
                    return False
                artifacts[cast(str, payload["artifact_id"])] = (
                    sequence, payload, cast(Mapping[str, object], proof["scope_content"]),
                    source_id, source_sequence, cast(str, proof["content_sha256"]),
                )
        elif event.get("kind") == "model.failure_receipt":
            linked = artifacts.get(cast(str, payload.get("artifact_id")))
            if linked is None:
                return False
            try:
                source_id, source_sequence = _model_scope_source(
                    event, artifact_id=cast(str, payload["artifact_id"]), digest=linked[5]
                )
            except (TypeError, ValueError):
                return False
            if linked[0] < sequence and linked[3] == source_id and linked[4] < source_sequence and all(
                payload.get(key) == linked[1].get(key) for key in _MODEL_SCOPE_PAYLOAD_KEYS
                if key != "model_scope_origin"
            ):
                artifact_id = cast(str, payload["artifact_id"])
                if artifact_id in linked_ids:
                    return False
                linked_ids.add(artifact_id)
            else:
                return False
    if not artifacts or linked_ids != set(artifacts):
        return False
    groups: dict[str, list[Mapping[str, object]]] = {}
    for _, _, content, _, _, _ in artifacts.values():
        groups.setdefault(cast(str, content["scope_id"]), []).append(content)
    call_ids: set[str] = set()
    for parts in groups.values():
        parts.sort(key=lambda part: cast(int, part["part_index"]))
        first = parts[0]
        if len(parts) != first["part_count"]:
            return False
        offset = 0
        for index, part in enumerate(parts, 1):
            if part["part_index"] != index or part["call_offset"] != offset or any(
                part[key] != first[key] for key in (
                    "run_id", "tenant_id", "requested_logical_model", "call_count", "part_count"
                )
            ):
                return False
            calls = cast(list[Mapping[str, object]], part["calls"])
            for call in calls:
                receipt = cast(Mapping[str, object], call["receipt"])
                call_id = cast(str, receipt["call_id"])
                if call_id in call_ids:
                    return False
                call_ids.add(call_id)
            offset += len(calls)
        if offset != first["call_count"]:
            return False
    return True


def _model_scope_errors(
    evidence: object, *, logical_model: str | None, result_run_id: object
) -> list[str]:
    if not isinstance(evidence, Mapping):
        return ["model scope evidence is missing"]
    errors: list[str] = []
    if evidence.get("source") != "public_run_events" or evidence.get("errors") != []:
        errors.append("model scope public evidence is incomplete")
    original = evidence.get("original_run_id")
    repairs = evidence.get("accepted_repair_run_ids")
    if (
        not isinstance(original, str)
        or not original.strip()
        or not isinstance(result_run_id, str)
        or not result_run_id.strip()
        or evidence.get("result_run_id") != result_run_id
        or not isinstance(repairs, list)
        or any(not isinstance(item, str) or not item.strip() for item in repairs)
    ):
        return [*errors, "model scope original/result/repair run identity is incomplete"]
    required = {original, result_run_id, *cast(list[str], repairs)}
    runs = evidence.get("runs")
    if not isinstance(runs, list):
        return [*errors, "model scope runs must be a list"]
    seen: set[str] = set()
    for run in runs:
        if not isinstance(run, Mapping) or not isinstance(run.get("run_id"), str):
            errors.append("model scope contains malformed run evidence")
            continue
        run_id = cast(str, run["run_id"])
        if run_id in seen or run_id not in required:
            errors.append("model scope contains duplicate or unrelated run evidence")
        seen.add(run_id)
        endpoint = f"/api/v1/runs/{quote(run_id, safe='')}/events"
        if run.get("events_endpoint") != endpoint:
            errors.append(f"{run_id}: model evidence endpoint does not match run")
        if run.get("status") not in {"completed", "failed"}:
            errors.append(f"{run_id}: model evidence requires a terminal run")
        events = run.get("model_events")
        if not isinstance(events, list):
            errors.append(f"{run_id}: model events are malformed")
            continue
        completions = int(_has_direct_model_completion(events))
        scope_present = False
        for event in events:
            if not isinstance(event, Mapping) or event.get("run_id") != run_id:
                errors.append(f"{run_id}: model event run scope is invalid")
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                errors.append(f"{run_id}: model event payload is malformed")
                continue
            # Runtime completion markers are telemetry, not artifact-backed proof.
            completed = False
            if event.get("kind") == "model.scope_incomplete":
                errors.append(f"{run_id}: model call scope is incomplete")
            if event.get("kind") == "model.failure_receipt":
                scope_present = True
            proof = event.get("model_artifact")
            if event.get("kind") == "artifact.created" and proof is None and any(
                key in payload for key in ("attempted_logical_models", "requested_logical_model")
            ):
                errors.append(f"{run_id}: actual model artifact proof is missing")
            if (
                event.get("kind") == "model.completed"
                and payload.get("artifact_origin") == "builtin_fixture"
            ):
                errors.append(f"{run_id}: builtin fixture is not an actual model completion")
            if proof is not None:
                scope_proof = isinstance(proof, Mapping) and proof.get("type") == "model_attempt"
                scope_present = scope_present or scope_proof
                if not (_valid_model_scope_proof(event) if scope_proof
                        else _valid_model_artifact_proof(event)):
                    errors.append(f"{run_id}: actual model artifact proof is invalid")
                elif (
                    event.get("kind") == "artifact.created" and isinstance(proof, Mapping)
                    and proof.get("type") == "model_response"
                    and event.get("actor") == proof.get("producer")
                    and type(event.get("sequence")) is int
                    and cast(int, event["sequence"]) > 0
                ):
                    completed = True
                    if not _crew_model_start_matches(event, events):
                        errors.append(f"{run_id}: model completion does not match actor start")
            if completed:
                completions += 1
            if logical_model is not None and (completed or "logical_model" in payload) and payload.get(
                "logical_model"
            ) != logical_model:
                errors.append(f"{run_id}: observed logical model does not match selected model")
            if (
                logical_model is not None and "requested_logical_model" in payload
                and payload["requested_logical_model"] != logical_model
            ):
                errors.append(f"{run_id}: requested logical model does not match selected model")
            if "attempted_logical_models" in payload:
                attempted = payload.get("attempted_logical_models")
                if not isinstance(attempted, list) or any(
                    not isinstance(item, str) or not item
                    or logical_model is not None and item != logical_model
                    for item in attempted
                ):
                    errors.append(f"{run_id}: attempted logical models do not match selected model")
        scope_complete = _has_failed_attempt_scope(events)
        if scope_present and not scope_complete:
            errors.append(f"{run_id}: model call scope chain is incomplete")
        recovered_failure = (
            run_id in repairs and run_id not in {original, result_run_id}
            and run.get("status") == "failed" and scope_complete
        )
        if completions == 0 and not recovered_failure:
            errors.append(f"{run_id}: no actual model completion evidence")
    if seen != required:
        errors.append("model scope evidence does not cover every original/result/repair run")
    return errors


def _collect_model_scope_evidence(
    client: RealUserAcceptanceClient,
    *,
    logical_model: str | None,
    submitted_run_ids: Sequence[str],
    accepted_repair_run_ids: Sequence[str],
    result_run_id: str | None,
) -> dict[str, object]:
    original = submitted_run_ids[0] if submitted_run_ids else None
    repairs = list(dict.fromkeys((*submitted_run_ids[1:], *accepted_repair_run_ids)))
    required = list(
        dict.fromkeys(
            run_id for run_id in (original, *repairs, result_run_id) if run_id is not None
        )
    )
    runs: list[dict[str, object]] = []
    errors: list[str] = []
    for run_id in required:
        root = f"/api/v1/runs/{quote(run_id, safe='')}"
        item: dict[str, object] = {
            "run_id": run_id,
            "events_endpoint": f"{root}/events",
            "model_events": [],
        }
        try:
            details = client.request_json("GET", root)
            if not isinstance(details, Mapping) or details.get("id") != run_id:
                raise ValueError("public run identity does not match model evidence")
            item["status"] = details.get("status")
            response = client.request_json("GET", f"{root}/events")
            events = response.get("items") if isinstance(response, Mapping) else None
            if not isinstance(events, list):
                raise TypeError("public run events must contain an items list")
            model_events: list[dict[str, object]] = []
            for event in events:
                if not isinstance(event, Mapping):
                    raise TypeError("public run events contain a malformed event")
                payload = event.get("payload")
                kind = event.get("kind")
                artifact = event.get("artifact")
                proof: dict[str, object] | None = None
                if kind == "artifact.created" and (
                    (isinstance(artifact, Mapping)
                     and artifact.get("type") in {"model_response", "model_attempt"})
                    or (isinstance(payload, Mapping) and any(
                        key in payload for key in ("attempted_logical_models", "requested_logical_model")
                    ))
                ):
                    if not isinstance(artifact, Mapping):
                        raise ValueError("public model artifact envelope is missing")
                    proof = _public_model_artifact(event)
                    provenance = cast(Mapping[str, object], proof["provenance"])
                    actual_payload: dict[str, object] = dict(payload) if isinstance(payload, Mapping) else {}
                    if (
                        "artifact_id" in actual_payload and actual_payload["artifact_id"] != proof["id"]
                        or "logical_model" in actual_payload
                        and actual_payload["logical_model"] != provenance["logical_model"]
                    ):
                        raise ValueError("public model artifact identity contradicts its event")
                    if artifact.get("type") == "model_response":
                        content = artifact.get("content")
                        if not isinstance(content, Mapping):
                            raise ValueError("public model completion content is unavailable")
                        if "attempted_logical_models" in actual_payload and (
                            actual_payload["attempted_logical_models"]
                            != content.get("attempted_logical_models")
                        ):
                            raise ValueError("public model attempt histories contradict")
                        actual_payload["logical_model"] = provenance["logical_model"]
                        actual_payload["attempted_logical_models"] = content.get("attempted_logical_models")
                    actual_payload["artifact_id"] = proof["id"]
                    payload = actual_payload
                if (
                    proof is not None
                    or isinstance(kind, str)
                    and kind.startswith("model.")
                    or (
                        isinstance(payload, Mapping)
                        and any(
                            key in payload for key in ("logical_model", "attempted_logical_models")
                        )
                    )
                ):
                    # Preserve verified completion identities, never response text/tool arguments.
                    model_events.append(
                        {
                            **{
                                key: event[key]
                                for key in ("kind", "run_id", "sequence", "actor")
                                if key in event
                            },
                            **({"actor": payload.get("actor")}
                               if kind == "model.failure_receipt" and isinstance(payload, Mapping)
                               and event.get("actor") is None else {}),
                            "payload": {
                                key: copy.deepcopy(payload[key])
                                for key in _MODEL_SCOPE_PAYLOAD_KEYS
                                if key in payload
                            }
                            if isinstance(payload, Mapping)
                            else None,
                            **({"model_artifact": proof} if proof is not None else {}),
                        }
                    )
            item["model_events"] = model_events
        except Exception as error:  # noqa: BLE001 - missing public evidence must fail the case.
            errors.append(f"{run_id}: public_model_scope_read_failed ({type(error).__name__})")
        runs.append(item)
    evidence: dict[str, object] = {
        "source": "public_run_events",
        "original_run_id": original,
        "result_run_id": result_run_id,
        "accepted_repair_run_ids": repairs,
        "runs": runs,
        "errors": [],
    }
    errors.extend(
        _model_scope_errors(evidence, logical_model=logical_model, result_run_id=result_run_id)
    )
    evidence.update(ok=not errors, errors=errors)
    return evidence


def _validated_manifest(value: object) -> dict[str, tuple[int, str]] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    result: dict[str, tuple[int, str]] = {}
    for path, metadata in value.items():
        if not isinstance(path, str) or not isinstance(metadata, (list, tuple)) or len(metadata) != 2:
            return None
        try:
            if str(_safe_zip_member_path(path)) != path:
                return None
        except RuntimeError:
            return None
        size, digest = metadata
        if type(size) is not int or size < 0 or not isinstance(digest, str):
            return None
        if _SHA256_RE.fullmatch(digest) is None:
            return None
        result[path] = (size, digest)
    return result


def _public_bundle_matches_validation(
    expected: object,
    public: Mapping[str, object],
) -> bool:
    validated = _validated_manifest(expected)
    downloaded = _validated_manifest(public.get("workspace_manifest"))
    return (
        validated is not None
        and downloaded == validated
        and public.get("validated_bundle_matches") is True
        and all(
            type(public.get(key)) is int and public[key] == len(validated)
            for key in ("file_count", "downloaded_file_count", "zip_member_count")
        )
    )


def verify_public_workspace_artifacts(
    client: AcceptanceClient,
    *,
    project_id: str,
    workspace_session_id: str,
    validated_workspace_manifest: object = None,
) -> dict[str, object]:
    """Bind public downloads to the exact content that passed build and business tests."""

    encoded_project = quote(project_id, safe="")
    encoded_session = quote(workspace_session_id, safe="")
    root = f"/api/v1/workspaces/projects/{encoded_project}/sessions/{encoded_session}"
    files_path = f"{root}/files"
    bundle_path = f"{root}/bundle/download"
    errors: list[str] = []
    downloaded_file_count = 0
    zip_sha256: str | None = None
    zip_crc_ok = False
    metadata_matches_zip = False
    unsafe_member_count = 0
    file_items: list[Mapping[str, object]] = []
    bundle = b""

    try:
        listing = client.request_json("GET", files_path)
        if not isinstance(listing, dict):
            raise TypeError("public workspace file list returned non-object JSON")
        raw_items = listing.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise RuntimeError("public workspace file list is empty")
        if listing.get("bundle_download_url") != bundle_path:
            errors.append("public bundle_download_url does not match the requested workspace")
        file_items = [
            cast(Mapping[str, object], item) for item in raw_items if isinstance(item, Mapping)
        ]
        if len(file_items) != len(raw_items):
            errors.append("public workspace file list contains invalid items")
        bundle = client.request_bytes("GET", bundle_path)
    except Exception as error:  # noqa: BLE001 - report every scale instead of aborting the matrix.
        errors.append(f"public workspace API: {error}")

    zip_files: dict[str, bytes] = {}
    if bundle:
        zip_sha256 = hashlib.sha256(bundle).hexdigest()
        try:
            with zipfile.ZipFile(BytesIO(bundle)) as archive:
                seen: set[str] = set()
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    try:
                        safe_path = str(_safe_zip_member_path(info.filename))
                    except RuntimeError:
                        unsafe_member_count += 1
                        continue
                    if safe_path in seen:
                        errors.append(f"public ZIP contains duplicate member: {safe_path}")
                        continue
                    seen.add(safe_path)
                    zip_files[safe_path] = archive.read(info)
                bad_member = archive.testzip()
                zip_crc_ok = bad_member is None
                if bad_member is not None:
                    errors.append(f"public ZIP CRC failed: {bad_member}")
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            errors.append(f"public ZIP invalid: {error}")
    else:
        errors.append("public ZIP download is empty")

    listed_paths: set[str] = set()
    for item in file_items:
        raw_path = item.get("path")
        raw_size = item.get("size_bytes")
        raw_sha256 = item.get("sha256")
        raw_download_url = item.get("download_url")
        if not isinstance(raw_path, str):
            errors.append("public file metadata is missing path")
            continue
        try:
            safe_path = str(_safe_zip_member_path(raw_path))
        except RuntimeError as error:
            errors.append(str(error))
            continue
        if safe_path in listed_paths:
            errors.append(f"public file list contains duplicate path: {safe_path}")
            continue
        listed_paths.add(safe_path)
        if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
            errors.append(f"public file has invalid size: {safe_path}")
            continue
        if not isinstance(raw_sha256, str) or _SHA256_RE.fullmatch(raw_sha256) is None:
            errors.append(f"public file has invalid sha256: {safe_path}")
            continue
        if not isinstance(raw_download_url, str) or not _valid_public_file_url(
            raw_download_url,
            expected_path=f"{root}/files/download",
            expected_file=safe_path,
        ):
            errors.append(f"public file has invalid download_url: {safe_path}")
            continue
        try:
            downloaded = client.request_bytes("GET", raw_download_url)
        except Exception as error:  # noqa: BLE001 - preserve the complete evidence report.
            errors.append(f"public file download failed {safe_path}: {error}")
            continue
        downloaded_file_count += 1
        if len(downloaded) != raw_size:
            errors.append(f"public file size mismatch: {safe_path}")
        if hashlib.sha256(downloaded).hexdigest() != raw_sha256:
            errors.append(f"public file sha256 mismatch: {safe_path}")
        zipped = zip_files.get(safe_path)
        if zipped is None:
            errors.append(f"public ZIP is missing listed file: {safe_path}")
        elif zipped != downloaded:
            errors.append(f"public ZIP content mismatch: {safe_path}")

    if listed_paths != set(zip_files):
        errors.append("public file list and ZIP member set differ")
    elif listed_paths:
        metadata_matches_zip = True
    if unsafe_member_count:
        errors.append(f"public ZIP contains {unsafe_member_count} unsafe members")

    public_manifest = {
        path: (len(content), hashlib.sha256(content).hexdigest())
        for path, content in zip_files.items()
    }
    expected_manifest = _validated_manifest(validated_workspace_manifest)
    validated_bundle_matches = expected_manifest is not None and public_manifest == expected_manifest
    if expected_manifest is None:
        errors.append("validated workspace manifest is missing or invalid")
    elif not validated_bundle_matches:
        errors.append("public ZIP differs from the workspace that passed validation")

    return {
        "ok": not errors,
        "source": "public_workspace_api",
        "admin_internal_run_data_used": False,
        "files_endpoint": files_path,
        "bundle_endpoint": bundle_path,
        "file_count": len(listed_paths),
        "downloaded_file_count": downloaded_file_count,
        "zip_member_count": len(zip_files),
        "zip_size_bytes": len(bundle),
        "zip_sha256": zip_sha256,
        "zip_crc_ok": zip_crc_ok,
        "metadata_matches_zip": metadata_matches_zip,
        "unsafe_member_count": unsafe_member_count,
        "workspace_manifest": {path: list(item) for path, item in public_manifest.items()},
        "validated_bundle_matches": validated_bundle_matches,
        "errors": errors,
    }


def _valid_public_file_url(value: str, *, expected_path: str, expected_file: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != expected_path:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return query.get("path") == [expected_file]


def _dynamic_preview_pending() -> dict[str, object]:
    return {
        "status": "pending_real_device",
        "counted_as_passed": False,
        "reason": "desktop and mobile browser interaction require deployed real-device acceptance",
        "required_evidence": [
            "desktop_browser_interaction",
            "mobile_browser_interaction",
        ],
    }


def verify_dynamic_web_preview(
    client: AcceptanceClient,
    *,
    project_id: str,
    conversation_id: str,
    workspace_session_id: str,
) -> dict[str, object]:
    """Exercise the public preview lifecycle without retaining its capability token."""

    preview_id: str | None = None
    preview_url: str | None = None
    reachable = False
    current_preview_matches = False
    renewed = False
    lease_extended = False
    stopped = False
    revoked_after_stop = False
    content_size_bytes = 0
    referenced_asset_count = 0
    referenced_assets_loaded = 0
    errors: list[str] = []
    try:
        started = client.request_json(
            "POST",
            "/api/v1/web-previews/start",
            body={
                "project_id": project_id,
                "conversation_id": conversation_id,
                "workspace_session_id": workspace_session_id,
            },
        )
        if not isinstance(started, dict):
            raise TypeError("preview start returned non-object JSON")
        raw_id = started.get("id")
        raw_url = started.get("preview_url")
        if started.get("status") != "ready" or not isinstance(raw_id, str):
            raise RuntimeError("preview did not become ready")
        if not isinstance(raw_url, str) or not raw_url.startswith(
            f"/api/v1/web-previews/{quote(raw_id, safe='')}/content/"
        ):
            raise RuntimeError("preview returned an invalid public URL")
        preview_id = raw_id
        preview_url = raw_url
        current = client.request_json(
            "GET",
            f"/api/v1/web-previews/conversations/{quote(conversation_id, safe='')}",
        )
        current_preview_matches = (
            isinstance(current, dict)
            and current.get("id") == preview_id
            and current.get("status") == "ready"
            and current.get("preview_url") == preview_url
        )
        if not current_preview_matches:
            errors.append("current preview lookup did not match the started preview")
        content = client.request_bytes("GET", preview_url)
        content_size_bytes = len(content)
        lowered = content[:4096].lower()
        if not content or b"<html" not in lowered and b"<!doctype html" not in lowered:
            raise RuntimeError("preview root did not return HTML")
        reachable = True
        asset_paths = _preview_asset_paths(content, preview_url)
        referenced_asset_count = len(asset_paths)
        for asset_path in asset_paths:
            asset = client.request_bytes("GET", asset_path)
            if not asset:
                raise RuntimeError(f"preview asset returned an empty response: {asset_path}")
            referenced_assets_loaded += 1
        renewed_payload = client.request_json(
            "POST",
            f"/api/v1/web-previews/{quote(preview_id, safe='')}/renew",
        )
        if not isinstance(renewed_payload, dict):
            raise TypeError("preview renew returned non-object JSON")
        renewed = (
            renewed_payload.get("id") == preview_id and renewed_payload.get("status") == "ready"
        )
        initial_expiry = _timestamp(started.get("lease_expires_at"))
        renewed_expiry = _timestamp(renewed_payload.get("lease_expires_at"))
        lease_extended = (
            initial_expiry is not None
            and renewed_expiry is not None
            and renewed_expiry > initial_expiry
        )
        if not renewed:
            errors.append("preview renew did not preserve the ready preview")
        if not lease_extended:
            errors.append("preview renew did not extend the lease")
    except Exception as error:  # noqa: BLE001 - preserve complete lifecycle evidence.
        errors.append(f"preview lifecycle: {error}")
    finally:
        if preview_id is not None:
            try:
                stopped_payload = client.request_json(
                    "DELETE",
                    f"/api/v1/web-previews/{quote(preview_id, safe='')}",
                )
                stopped = (
                    isinstance(stopped_payload, dict)
                    and stopped_payload.get("id") == preview_id
                    and stopped_payload.get("status") == "stopped"
                    and stopped_payload.get("preview_url") is None
                )
                if not stopped:
                    errors.append("preview stop did not return a stopped state")
            except Exception as error:  # noqa: BLE001 - report cleanup failures.
                errors.append(f"preview stop: {error}")
        if preview_url is not None and stopped:
            try:
                client.request_bytes("GET", preview_url)
            except Exception as error:  # noqa: BLE001 - record exact public revocation evidence.
                if _is_explicit_preview_not_found(error):
                    revoked_after_stop = True
                else:
                    errors.append(f"preview revocation check failed: {error}")
            else:
                errors.append("preview capability URL remained readable after stop")

    passed = (
        reachable
        and current_preview_matches
        and renewed
        and lease_extended
        and referenced_assets_loaded == referenced_asset_count
        and stopped
        and revoked_after_stop
        and not errors
    )
    return {
        "status": "passed" if passed else "failed",
        "counted_as_passed": passed,
        "reachable_preview_url": reachable,
        "current_preview_matches": current_preview_matches,
        "renewed": renewed,
        "lease_extended": lease_extended,
        "referenced_asset_count": referenced_asset_count,
        "referenced_assets_loaded": referenced_assets_loaded,
        "stopped": stopped,
        "revoked_after_stop": revoked_after_stop,
        "content_size_bytes": content_size_bytes,
        "browser_interaction": "pending_real_device",
        "capability_token_retained": False,
        "errors": errors,
    }


def _deferred_preview() -> dict[str, object]:
    return {
        "status": "deferred",
        "counted_as_passed": False,
        "browser_interaction": "pending_preview",
    }


def _reject_deferred_report(report: Mapping[str, object]) -> None:
    entries: list[Mapping[str, object]] = [report]
    for key in ("cases", "attempt_history"):
        value = report.get(key)
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, Mapping))
    for entry in entries:
        preview = entry.get("dynamic_web_preview")
        deferred_preview = isinstance(preview, Mapping) and (
            preview.get("status") in ("deferred", "pending_preview")
            or preview.get("browser_interaction") == "pending_preview"
        )
        if (
            entry.get("defer_preview") is True
            or entry.get("status") in ("deferred", "pending_preview")
            or deferred_preview
        ):
            raise ValueError("deferred preview reports cannot resume or finalize")


def build_case_report(
    *,
    scale: str,
    project: Mapping[str, object],
    conversation: Mapping[str, object],
    result: ProjectScaleCaseResult,
    public_artifacts: Mapping[str, object],
    dynamic_web_preview: Mapping[str, object],
    logical_model: str | None = None,
    model_scope_evidence: Mapping[str, object] | None = None,
    defer_preview: bool = False,
) -> dict[str, object]:
    generated_project_ok = result.evidence.get("generated_project_validation") is True
    requirements_ok = result.evidence.get("requirements_validation") is True
    public_artifacts_ok = public_artifacts.get("ok") is True
    validated_bundle_matches = _public_bundle_matches_validation(
        result.validated_workspace_manifest, public_artifacts,
    )
    if defer_preview:
        dynamic_web_preview = _deferred_preview()
    preview_ok = dynamic_web_preview.get("counted_as_passed") is True
    route_intent = result.case_id.split(":", 1)[1]
    case_kind = "auto_scale" if route_intent == "auto" else "mode_capability"
    expected_modes = {
        "auto": (
            frozenset({"hybrid"})
            if scale in {"large", "ultra"}
            else frozenset({"direct", "dispatch", "hybrid"})
        ),
        "direct": frozenset({"direct"}),
        "dispatch": frozenset({"dispatch"}),
        "hybrid": frozenset({"hybrid"}),
        "multi_agent": frozenset({"dispatch"}),
    }
    allowed_modes = expected_modes.get(route_intent)
    final_mode = result.final_observed_mode or result.observed_mode
    route_observed_mode = result.observed_mode if route_intent == "auto" else final_mode
    exact_mode_coverage_ok = allowed_modes is None or route_observed_mode in allowed_modes
    safe_upgrade = (
        route_intent == "direct"
        and result.requested_mode == "direct"
        and final_mode == "hybrid"
        and result.route_reason == "project_scale_mode_upgrade"
        and result.mode_source == "project_scale_assessment"
        and result.effective_scale in {"large", "ultra"}
    )
    route_policy_ok = exact_mode_coverage_ok or safe_upgrade
    effective_scale = result.effective_scale
    final_effective_scale = result.completion_scale
    scale_fidelity_ok = effective_scale == scale and final_effective_scale == scale
    artifact_origin_ok = result.artifact_origin in {
        "model_workspace_bundle",
        "tool_workspace_write",
        "incremental_workspace_delivery",
    }
    required_multi_agent_ids = {"architect", "implementer", "tester", "synthesizer"}
    multi_agent_evidence_ok = route_intent != "multi_agent" or (
        result.evidence.get("multi_agent_participation") is True
        and required_multi_agent_ids <= set(result.participant_agent_ids)
        and result.participant_event_count >= 8
    )
    delivery_ok = (
        result.status == "completed"
        and bool(result.run_id)
        and result.ok
        and generated_project_ok
        and requirements_ok
        and public_artifacts_ok
        and validated_bundle_matches
        and route_policy_ok
        and scale_fidelity_ok
        and result.scale_specific_evidence_ok
        and artifact_origin_ok
        and multi_agent_evidence_ok
    )
    core_ok = delivery_ok and preview_ok
    report: dict[str, object] = {
        "case_id": result.case_id,
        "case_kind": case_kind,
        "scale": scale,
        "route_intent": route_intent,
        "observed_mode": result.observed_mode,
        "initial_observed_mode": result.observed_mode,
        "final_observed_mode": final_mode,
        "route_observed_mode": route_observed_mode,
        "requested_mode": result.requested_mode,
        "route_reason": result.route_reason,
        "mode_source": result.mode_source,
        "effective_scale": effective_scale,
        "initial_effective_scale": effective_scale,
        "final_effective_scale": final_effective_scale,
        "scale_fidelity_ok": scale_fidelity_ok,
        "scale_specific_evidence_ok": result.scale_specific_evidence_ok,
        "route_policy_ok": route_policy_ok,
        "observed_route_ok": route_policy_ok,
        "exact_mode_coverage_ok": exact_mode_coverage_ok,
        "coverage_credit": (
            "exact_mode" if exact_mode_coverage_ok else "safe_upgrade" if safe_upgrade else "none"
        ),
        "artifact_origin_ok": artifact_origin_ok,
        "validated_bundle_matches": validated_bundle_matches,
        "autonomous_mode_selected": case_kind == "auto_scale" and route_policy_ok,
        "multi_agent_evidence_ok": multi_agent_evidence_ok,
        "status": "pending_real_device" if core_ok else "failed",
        "core_acceptance_ok": core_ok,
        "automated_acceptance_complete": core_ok,
        "real_device_acceptance_complete": False,
        "acceptance_complete": False,
        "project": dict(project),
        "conversation": dict(conversation),
        "run": result.to_payload(),
        "build_and_test": {
            "status": (
                "passed"
                if generated_project_ok and requirements_ok and validated_bundle_matches
                and result.scale_specific_evidence_ok
                else "failed"
            ),
            "source": "project_scale_runner.generated_project_validation",
            "generated_project_validation": generated_project_ok,
            "requirements_validation": requirements_ok,
            "scale_specific_evidence_ok": result.scale_specific_evidence_ok,
            "validated_bundle_matches": validated_bundle_matches,
        },
        "public_artifacts": dict(public_artifacts),
        "dynamic_web_preview": dict(dynamic_web_preview),
        "artifact_provenance": {
            "artifact_origin": result.artifact_origin,
            "embedded_bundle_available": result.workspace_bundle_source == "embedded_bundle",
            "public_materialized": public_artifacts_ok,
            "preview_available": preview_ok,
            "fixture_origin_allowed": False,
        },
        "success_basis": {
            "logged_in_user_http_api": True,
            "public_run_api": True,
            "public_workspace_file_api": public_artifacts_ok,
            "public_workspace_zip": public_artifacts_ok,
            "validated_workspace_bundle": validated_bundle_matches,
            "public_preview_lifecycle": preview_ok,
            "observed_route": route_policy_ok,
            "exact_mode_coverage": exact_mode_coverage_ok,
            "scale_fidelity": scale_fidelity_ok,
            "scale_specific_evidence": result.scale_specific_evidence_ok,
            "artifact_origin": artifact_origin_ok,
            "multi_agent_participation": multi_agent_evidence_ok,
            "admin_internal_run_data": False,
        },
    }
    model_ok = (
        model_scope_evidence is not None
        and model_scope_evidence.get("ok") is True
        and not _model_scope_errors(
            model_scope_evidence, logical_model=logical_model, result_run_id=result.run_id
        )
    )
    if logical_model is not None:
        report["model_profile"] = _model_profile(logical_model)
    report["model_scope_evidence"] = copy.deepcopy(model_scope_evidence)
    cast(dict[str, object], report["success_basis"])["model_scope"] = model_ok
    if not model_ok:
        report.update(
            status="failed", core_acceptance_ok=False, automated_acceptance_complete=False
        )
    if defer_preview:
        report.update(
            defer_preview=True,
            delivery_acceptance_ok=delivery_ok and model_ok,
            status="pending_preview" if delivery_ok and model_ok else "failed",
            core_acceptance_ok=False,
            automated_acceptance_complete=False,
        )
    return report


def _create_or_recover_case_resource(
    client: RealUserAcceptanceClient,
    *,
    path: str,
    body: dict[str, object],
    idempotency_key: str,
) -> dict[str, object]:
    is_project = path == "/api/v1/admin/project-workspaces"
    expected_code = "project_workspace_conflict" if is_project else "conversation_conflict"
    try:
        resource = client.request_json("POST", path, body=body, idempotency_key=idempotency_key)
    except AcceptanceHTTPError as error:
        if error.status_code != 409 or error.method != "POST" or error.path != path:
            raise
        try:
            payload = json.loads(error.response_body)
        except json.JSONDecodeError:
            raise error from None
        details = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(details, dict) or details.get("code") != expected_code:
            raise
        # These authenticated reads enforce tenant ownership; the API exposes no user owner field.
        if is_project:
            existing = client.request_json("GET", path)
            if not isinstance(existing, list):
                raise TypeError("project resource scope lookup returned non-list JSON") from error
            matches = [
                item
                for item in existing
                if isinstance(item, dict) and item.get("project_id") == body["project_id"]
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "project resource scope is not uniquely visible to this actor"
                ) from error
            resource = matches[0]
        else:
            resource = client.request_json(
                "GET",
                f"{path}/{quote(str(body['conversation_id']), safe='')}",
            )
    if not isinstance(resource, dict):
        raise TypeError("case resource scope lookup returned non-object JSON")
    for key, expected in body.items():
        if resource.get(key) != expected:
            raise RuntimeError(f"case resource scope mismatch: {key}")
    if not is_project and resource.get("archived_at") is not None:
        raise RuntimeError("case resource scope mismatch: conversation is archived")
    return resource


def run_real_user_four_scale_acceptance(
    client: RealUserAcceptanceClient,
    *,
    username: str,
    base_url: str,
    execution_id: str,
    wait_seconds: float,
    poll_interval_seconds: float,
    artifact_build_timeout_seconds: float,
    progress: Callable[[str], None] | None = None,
    authentication_method: str = "password",
    output_path: str | None = None,
    resume_report: Mapping[str, object] | None = None,
    logical_model: str | None = None,
    evidence_root: Path | None = None,
    defer_preview: bool = False,
    stop_on_failure: bool = False,
) -> dict[str, object]:
    if type(defer_preview) is not bool:
        raise TypeError("defer_preview must be a boolean")
    if type(stop_on_failure) is not bool:
        raise TypeError("stop_on_failure must be a boolean")
    if resume_report is not None:
        _reject_deferred_report(resume_report)
        if defer_preview:
            raise ValueError("deferred preview execution cannot resume existing reports")
    profile = _model_profile(logical_model)
    if resume_report is not None:
        _validate_report_model_profile(resume_report, profile)
    if not isinstance(output_path, str) or not output_path.strip():
        raise ValueError("paid acceptance requires a durable output_path")
    if resume_report is None and os.path.lexists(output_path):
        raise ValueError("fresh acceptance cannot overwrite an existing checkpoint")
    started_at = _utc_now()
    principal = client.request_json("GET", "/api/v1/auth/me")
    if not isinstance(principal, dict):
        raise TypeError("GET /api/v1/auth/me returned non-object JSON")
    cases: list[dict[str, object]] = []
    attempt_history: list[dict[str, object]] = []
    safe_execution_id = _safe_identifier(execution_id)
    identity = _execution_identity(execution_id, base_url, principal, profile)
    journal = (
        _submission_journal_from_report(resume_report, identity)
        if resume_report is not None else SubmissionJournal(identity)
    )
    if resume_report is not None:
        cases = _resume_cases(resume_report, identity, attempt_history)
        if _is_finalized_report(resume_report):
            return _validated_finalized_report(resume_report, evidence_root=evidence_root)
        _verify_retry_submissions(client, journal, resume_report, safe_execution_id)
        previous_start = resume_report.get("started_at")
        if isinstance(previous_start, str) and _timestamp(previous_start) is not None:
            started_at = previous_start

    def snapshot(*, finished: bool = False) -> dict[str, object]:
        return _matrix_report(
            client=client,
            cases=cases,
            attempt_history=attempt_history,
            username=username,
            principal=principal,
            base_url=base_url,
            execution_id=execution_id,
            identity=identity,
            started_at=started_at,
            authentication_method=authentication_method,
            finished=finished,
            defer_preview=defer_preview,
        )

    client.configure_submission_journal(journal, lambda: _save_report(output_path, snapshot()))
    _save_report(output_path, snapshot())

    for case_kind, scale, route_intent, case_key in _ACCEPTANCE_CASES:
        case_id = f"{scale}:{route_intent}"
        previous = next((case for case in cases if case.get("case_id") == case_id), None)
        if previous is not None and _has_complete_core_evidence(
            previous, safe_execution_id=safe_execution_id, case_key=case_key
        ):
            if progress is not None:
                progress(f"{case_kind}/{scale}/{route_intent}: resuming completed core evidence")
            continue
        attempt = _case_attempt(previous) + 1 if previous is not None else 1
        scope_token = _case_execution_token(safe_execution_id, case_key, attempt)
        project_id = _bounded_identifier(f"uat-{scope_token}", 128)
        conversation_id = _bounded_identifier(
            f"conv-{scope_token}",
            128,
        )
        project_label = (
            f"真实用户 {scale} AUTO 规模验收"
            if case_kind == "auto_scale"
            else f"真实用户 {route_intent} 模式能力验收"
        )
        runner_execution_id = scope_token
        workspace_session_id = _safe_workspace_session_token(
            conversation_id,
            runner_execution_id,
        )
        client.set_submission_context({
            "case_id": case_id, "attempt": attempt, "project_id": project_id,
            "conversation_id": conversation_id, "workspace_session_id": workspace_session_id,
        })
        if progress is not None:
            progress(f"{case_kind}/{scale}/{route_intent}: creating project and conversation")
        submitted_start = len(client.submitted_run_ids)
        repair_start = len(client.accepted_repair_run_ids)
        result_run_id: str | None = None
        model_scope_evidence: dict[str, object] | None = None
        try:
            project = _create_or_recover_case_resource(
                client,
                path="/api/v1/admin/project-workspaces",
                body={
                    "project_id": project_id,
                    "label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{scope_token}-project",
            )
            if not isinstance(project, dict) or project.get("project_id") != project_id:
                raise RuntimeError("project workspace creation returned the wrong project")
            conversation = _create_or_recover_case_resource(
                client,
                path="/api/v1/admin/conversations",
                body={
                    "conversation_id": conversation_id,
                    "title": f"{case_kind} {scale} {route_intent} 真实用户项目验收",
                    "project_id": project_id,
                    "project_label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{scope_token}-conversation",
            )
            if (
                not isinstance(conversation, dict)
                or conversation.get("conversation_id") != conversation_id
            ):
                raise RuntimeError("conversation creation returned the wrong conversation")

            plan = build_real_user_scale_plan(
                scale=scale,
                project_id=project_id,
                project_label=project_label,
                conversation_id=conversation_id,
                workspace_session_id=workspace_session_id,
                route_intent=route_intent,
                logical_model=logical_model,
                defer_preview=defer_preview,
            )
            effective_wait = _effective_execute_wait_seconds(
                plan,
                wait_seconds,
                generated_project_timeout_seconds=artifact_build_timeout_seconds,
                runtime_observation_budget_seconds=_REAL_USER_RUNTIME_OBSERVATION_BUDGET_SECONDS,
            )
            if progress is not None:
                progress(
                    f"{case_kind}/{scale}/{route_intent}: executing capability run "
                    f"with wait budget {effective_wait:.0f}s"
                )
            case_progress: Callable[[str], None] | None = None
            if progress is not None:
                progress_callback = progress
                progress_prefix = f"{case_kind}/{scale}/{route_intent}"

                def emit_case_progress(
                    message: str,
                    callback: Callable[[str], None] = progress_callback,
                    prefix: str = progress_prefix,
                ) -> None:
                    callback(f"{prefix}: {message}")

                case_progress = emit_case_progress
            runner_report = execute_project_scale_plan(
                plan,
                client,
                wait_seconds=effective_wait,
                poll_interval_seconds=poll_interval_seconds,
                execution_id=runner_execution_id,
                validate_generated_project=True,
                generated_project_timeout_seconds=artifact_build_timeout_seconds,
                progress=case_progress,
                auto_approve_capability_requests=True,
                runtime_observation_budget_seconds=_REAL_USER_RUNTIME_OBSERVATION_BUDGET_SECONDS,
            )
            result = runner_report.results[0]
            result_run_id = result.run_id
            if result.case_id != case_id:
                raise RuntimeError("capability runner returned evidence for the wrong case")
            model_scope_evidence = _collect_model_scope_evidence(
                client,
                logical_model=logical_model,
                submitted_run_ids=client.submitted_run_ids[submitted_start:],
                accepted_repair_run_ids=client.accepted_repair_run_ids[repair_start:],
                result_run_id=result_run_id,
            )
            public_artifacts = verify_public_workspace_artifacts(
                client,
                project_id=project_id,
                workspace_session_id=workspace_session_id,
                validated_workspace_manifest=result.validated_workspace_manifest,
            )
            dynamic_web_preview = (
                _deferred_preview() if defer_preview else verify_dynamic_web_preview(
                    client,
                    project_id=project_id,
                    conversation_id=conversation_id,
                    workspace_session_id=workspace_session_id,
                )
            )
            completed_case = build_case_report(
                scale=scale,
                project=project,
                conversation=conversation,
                result=result,
                public_artifacts=public_artifacts,
                dynamic_web_preview=dynamic_web_preview,
                logical_model=logical_model,
                model_scope_evidence=model_scope_evidence,
                defer_preview=defer_preview,
            )
        except Exception as error:  # noqa: BLE001 - every matrix case must be attempted.
            completed_case = {
                "case_id": case_id,
                "scale": scale,
                "case_kind": case_kind,
                "route_intent": route_intent,
                "status": "failed",
                "core_acceptance_ok": False,
                "automated_acceptance_complete": False,
                "real_device_acceptance_complete": False,
                "acceptance_complete": False,
                "project": {"project_id": project_id},
                "conversation": {"conversation_id": conversation_id},
                "errors": [str(error)],
                "dynamic_web_preview": {
                    "status": "failed",
                    "counted_as_passed": False,
                    "browser_interaction": "pending_real_device",
                },
                "success_basis": {
                    "logged_in_user_http_api": True,
                    "admin_internal_run_data": False,
                },
            }
        client.require_resolved_submissions()
        if "model_scope_evidence" not in completed_case:
            if model_scope_evidence is None:
                model_scope_evidence = _collect_model_scope_evidence(
                    client,
                    logical_model=logical_model,
                    submitted_run_ids=client.submitted_run_ids[submitted_start:],
                    accepted_repair_run_ids=client.accepted_repair_run_ids[repair_start:],
                    result_run_id=result_run_id,
                )
            completed_case["model_scope_evidence"] = model_scope_evidence
            cast(dict[str, object], completed_case["success_basis"])["model_scope"] = (
                model_scope_evidence.get("ok") is True
            )
        if logical_model is not None:
            completed_case["model_profile"] = copy.deepcopy(profile)
        completed_case["attempt"] = attempt
        if defer_preview:
            completed_case["defer_preview"] = True
            completed_case.setdefault("delivery_acceptance_ok", False)
            completed_case["dynamic_web_preview"] = _deferred_preview()
        if previous is not None:
            cases[cases.index(previous)] = completed_case
        else:
            cases.append(completed_case)
        # Saving is outside the case exception handler: a failed checkpoint must stop execution.
        if output_path:
            _save_report(output_path, snapshot())
        if stop_on_failure and completed_case["status"] == "failed":
            break

    payload = snapshot(finished=True)
    if output_path:
        _save_report(output_path, payload)
    return payload


def _canonical_case_index(cases: object) -> dict[str, dict[str, object]]:
    if not isinstance(cases, list):
        raise TypeError("canonical case evidence must be a list")
    expected = {f"{scale}:{route}": (scale, route) for _, scale, route, _ in _ACCEPTANCE_CASES}
    by_id: dict[str, dict[str, object]] = {}
    for case in cases:
        if not isinstance(case, dict):
            raise TypeError("canonical case evidence must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id not in expected:
            raise ValueError("case evidence has an invalid canonical case_id")
        if case_id in by_id:
            raise ValueError(f"case evidence has duplicate case_id: {case_id}")
        scale, route = expected[case_id]
        if case.get("scale") != scale or case.get("route_intent") != route:
            raise ValueError(f"case evidence identity does not match scale/route: {case_id}")
        if "run" in case:
            run = case["run"]
            if not isinstance(run, Mapping):
                raise TypeError(f"case evidence run must be an object: {case_id}")
            if run.get("case_id") != case_id:
                raise ValueError(f"case evidence run.case_id does not match: {case_id}")
        by_id[case_id] = case
    return by_id


def _matrix_mode_coverage(cases: object, *, execution_id: str) -> dict[str, object]:
    by_id = _canonical_case_index(cases)
    safe_execution_id = _safe_identifier(execution_id)
    auto_count = explicit_count = core_passed = auto_passed = exact_passed = safe_upgrades = 0
    missing_auto: list[str] = []
    missing_exact: list[str] = []
    for kind, scale, route, case_key in _ACCEPTANCE_CASES:
        case_id = f"{scale}:{route}"
        case = by_id.get(case_id)
        core_ok = case is not None and _has_complete_core_evidence(
            case, safe_execution_id=safe_execution_id, case_key=case_key,
        )
        core_passed += int(core_ok)
        # Core validation rebuilds and checks these credits against the underlying run.
        exact_ok = core_ok and case is not None and case["exact_mode_coverage_ok"] is True
        if core_ok and case is not None and case["coverage_credit"] == "safe_upgrade":
            safe_upgrades += 1
        if kind == "auto_scale":
            auto_count += 1
            auto_passed += int(exact_ok)
            if not exact_ok:
                missing_auto.append(case_id)
        else:
            explicit_count += 1
            exact_passed += int(exact_ok)
            if not exact_ok:
                missing_exact.append(case_id)
    return {
        "auto_scale_case_count": auto_count,
        "mode_capability_case_count": explicit_count,
        "core_passed_case_count": core_passed,
        "auto_scale_passed_case_count": auto_passed,
        "exact_mode_passed_case_count": exact_passed,
        "safe_upgrade_case_count": safe_upgrades,
        "missing_auto_scale_case_ids": missing_auto,
        "missing_exact_mode_case_ids": missing_exact,
        "exact_mode_coverage_complete": not missing_auto and not missing_exact,
    }


def _matrix_report(
    *,
    client: RealUserAcceptanceClient,
    cases: list[dict[str, object]],
    attempt_history: list[dict[str, object]],
    username: str,
    principal: Mapping[str, object],
    base_url: str,
    execution_id: str,
    identity: Mapping[str, object],
    started_at: str,
    authentication_method: str,
    finished: bool,
    defer_preview: bool = False,
) -> dict[str, object]:

    coverage = _matrix_mode_coverage(cases, execution_id=execution_id)
    core_ok = (
        finished
        and client.submission_journal is not None
        and not client.submission_journal.has_unresolved
        and coverage["core_passed_case_count"] == len(_ACCEPTANCE_CASES)
    )
    automated_ok = core_ok and coverage["exact_mode_coverage_complete"] is True
    status = (
        "pending_real_device" if automated_ok else "pending_mode_coverage" if core_ok
        else "failed" if finished else "in_progress"
    )
    report = {
        "schema_version": 1,
        "kind": "real_user_four_scale_acceptance",
        "status": status,
        "core_acceptance_ok": core_ok,
        "automated_acceptance_complete": automated_ok,
        "real_device_acceptance_complete": False,
        "acceptance_complete": False,
        "started_at": started_at,
        "finished_at": _utc_now() if finished else None,
        "base_url": base_url.rstrip("/"),
        "execution_id": execution_id,
        "execution_identity": dict(identity),
        "benchmark_kind": "capability",
        "run_mode": "mixed",
        "auto_scale_run_mode": "auto",
        "mode_capabilities": list(_MODE_CAPABILITIES),
        "scales": list(PROJECT_SCALE_TIERS),
        "route_intents": list(_MODE_CAPABILITIES),
        **coverage,
        "actor": {
            "username_from_environment": username,
            "authentication_method": authentication_method,
            "authenticated_via_password_environment": authentication_method == "password",
            "authenticated_via_bearer_environment": authentication_method == "bearer_token",
            "principal": principal,
        },
        "case_count": len(cases),
        "failed_case_count": sum(1 for case in cases if case.get("status") == "failed"),
        "pending_case_count": sum(
            1 for case in cases if case.get("status") == "pending_real_device"
        ),
        "dynamic_web_preview": _dynamic_preview_pending(),
        "real_device_acceptance": {
            "status": "pending_real_device",
            "counted_as_complete": False,
            "required_evidence": [
                "desktop_browser_interaction",
                "mobile_browser_interaction",
            ],
        },
        "success_policy": {
            "admin_internal_run_data_allowed": False,
            "fixture_results_allowed": False,
            "public_files_and_zip_required": True,
            "public_preview_lifecycle_required": True,
            "generated_project_build_and_tests_required": True,
            "pending_preview_counts_as_complete": False,
        },
        "blocked_admin_run_requests": list(client.blocked_admin_run_requests),
        "cases": copy.deepcopy(cases),
        "attempt_history": copy.deepcopy(attempt_history),
        "submission_journal": (
            client.submission_journal.snapshot() if client.submission_journal is not None else None
        ),
    }
    if "model_profile" in identity:
        report["model_profile"] = copy.deepcopy(identity["model_profile"])
    if defer_preview:
        delivered = [case for case in cases if case.get("delivery_acceptance_ok") is True]
        delivery_ok = (
            finished and len(delivered) == len(_ACCEPTANCE_CASES)
            and client.submission_journal is not None
            and not client.submission_journal.has_unresolved
        )
        report.update(
            defer_preview=True,
            delivery_acceptance_ok=delivery_ok,
            delivery_passed_case_count=len(delivered),
            delivery_failed_case_count=sum(
                case.get("delivery_acceptance_ok") is False for case in cases
            ),
            delivery_unsubmitted_case_count=len(_ACCEPTANCE_CASES) - len(cases),
            delivery_auto_scale_passed_case_count=sum(
                case["case_kind"] == "auto_scale" and case["exact_mode_coverage_ok"] is True
                for case in delivered
            ),
            delivery_exact_mode_passed_case_count=sum(
                case["case_kind"] == "mode_capability" and case["exact_mode_coverage_ok"] is True
                for case in delivered
            ),
            delivery_safe_upgrade_case_count=sum(
                case["coverage_credit"] == "safe_upgrade" for case in delivered
            ),
            status="pending_preview" if delivery_ok else "failed" if finished else "in_progress",
            core_acceptance_ok=False,
            automated_acceptance_complete=False,
            pending_case_count=sum(case.get("status") == "pending_preview" for case in cases),
            dynamic_web_preview=_deferred_preview(),
        )
    return report


def _execution_identity(
    execution_id: str,
    base_url: str,
    principal: Mapping[str, object],
    model_profile: Mapping[str, object] | None = None,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "execution_id": execution_id,
        "base_url": base_url.rstrip("/"),
    }
    for key in ("user_id", "tenant_id"):
        value = principal.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"execution identity requires authenticated {key}")
        identity[key] = value
    if not execution_id.strip() or not _SAFE_ID_RE.sub("-", execution_id.casefold()).strip("-"):
        raise ValueError("execution identity requires a usable execution_id")
    if model_profile is not None:
        identity["model_profile"] = copy.deepcopy(dict(model_profile))
    return identity


def _verify_retry_submissions(
    client: RealUserAcceptanceClient, journal: SubmissionJournal,
    report: Mapping[str, object], safe_execution_id: str,
) -> None:
    case_keys = {f"{scale}:{route}": key for _, scale, route, key in _ACCEPTANCE_CASES}
    for case_id, case in _canonical_case_index(report.get("cases")).items():
        key = case_keys[case_id]
        if _has_complete_core_evidence(
            case, safe_execution_id=safe_execution_id, case_key=key,
        ):
            continue
        for record in journal.records:
            context = cast(Mapping[str, object], record["context"])
            if context["case_id"] == case_id:
                response = client.read_confirmed_submission(
                    cast(Mapping[str, object], record["response"]),
                )
                if response["status"] not in {"completed", "failed", "cancelled"}:
                    raise ValueError("nonterminal confirmed submission cannot start a new attempt")


def _submission_journal_from_report(
    report: Mapping[str, object], identity: Mapping[str, object],
) -> SubmissionJournal:
    _canonical_case_index(report.get("cases"))
    raw = report.get("submission_journal")
    if not isinstance(raw, Mapping):
        raise ValueError("submission journal is missing; legacy submissions cannot be replayed")  # noqa: TRY004
    journal = SubmissionJournal(identity, raw)
    if journal.has_unresolved:
        raise ValueError("unresolved submission: automatic paid-request replay is forbidden")
    case_keys = {f"{scale}:{route}": key for _, scale, route, key in _ACCEPTANCE_CASES}
    confirmed: dict[tuple[str, int], set[str]] = {}
    post_counts: dict[tuple[str, int], int] = {}
    for record in journal.records:
        context = cast(dict[str, object], record["context"])
        case_id, attempt = cast(str, context["case_id"]), cast(int, context["attempt"])
        if case_id not in case_keys:
            raise ValueError("submission journal contains an unknown case")
        scope = _case_execution_token(
            _safe_identifier(cast(str, identity["execution_id"])), case_keys[case_id], attempt,
        )
        conversation = _bounded_identifier(f"conv-{scope}", 128)
        if context != {
            "case_id": case_id, "attempt": attempt,
            "project_id": _bounded_identifier(f"uat-{scope}", 128),
            "conversation_id": conversation,
            "workspace_session_id": _safe_workspace_session_token(conversation, scope),
        }:
            raise ValueError("submission journal does not match the canonical case scope")
        case = (case_id, attempt)
        ids = confirmed.setdefault(case, set())
        path = cast(str, record["path"])
        if path == "/api/v1/runs":
            ordinal = post_counts.get(case, 0)
            expected_key = (
                _idempotency_key(case_id, 0, execution_id=scope) if ordinal == 0
                else _deliverable_repair_idempotency_key(
                    case_id, 0, execution_id=scope, repair_attempt=ordinal,
                )
            )
            if record["idempotency_key"] != expected_key:
                raise ValueError("submission journal does not match the canonical request key")
            if ordinal == 0:
                scale, route = case_id.split(":")
                profile = identity.get("model_profile")
                plan = build_real_user_scale_plan(
                    scale=scale, route_intent=route,
                    project_id=cast(str, context["project_id"]),
                    conversation_id=conversation,
                    workspace_session_id=cast(str, context["workspace_session_id"]),
                    project_label=(
                        f"真实用户 {scale} AUTO 规模验收" if route == "auto"
                        else f"真实用户 {route} 模式能力验收"
                    ),
                    logical_model=(
                        cast(str, profile["direct_model"]) if isinstance(profile, Mapping) else None
                    ),
                )
                expected_digest = hashlib.sha256(json.dumps(
                    plan.requests[0].body, sort_keys=True, ensure_ascii=False,
                    allow_nan=False, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if record["request_sha256"] != expected_digest:
                    raise ValueError("submission request digest does not match the case plan")
            post_counts[case] = ordinal + 1
        else:
            parent = unquote(path.split("/")[4])
            if parent not in ids:
                raise ValueError("repair submission parent is not confirmed in this case")
        response = cast(Mapping[str, object], record["response"])
        ids.add(cast(str, response["id"]))
    for section in ("cases", "attempt_history"):
        entries = report.get(section, [])
        if not isinstance(entries, list):
            raise ValueError("submission journal requires flat case evidence")  # noqa: TRY004
        for case in entries:
            if not isinstance(case, Mapping):
                raise ValueError("submission journal requires object case evidence")  # noqa: TRY004
            evidence_case_id = case.get("case_id")
            ids = confirmed.get((cast(str, evidence_case_id), _case_attempt(case)), set())
            run = case.get("run")
            run_id = run.get("run_id") if isinstance(run, Mapping) else None
            scope_evidence = case.get("model_scope_evidence")
            required = {run_id} if isinstance(run_id, str) and run_id else set()
            if isinstance(scope_evidence, Mapping):
                required.update(
                    value for key in ("original_run_id", "result_run_id")
                    if isinstance(value := scope_evidence.get(key), str) and value
                )
                repairs = scope_evidence.get("accepted_repair_run_ids", [])
                if isinstance(repairs, list):
                    required.update(value for value in repairs if isinstance(value, str) and value)
            if not required <= ids:
                raise ValueError("submission journal does not match case core evidence")
    return journal


def _resume_cases(
    report: Mapping[str, object],
    identity: Mapping[str, object],
    attempt_history: list[dict[str, object]],
) -> list[dict[str, object]]:
    if (
        report.get("kind") != "real_user_four_scale_acceptance"
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("benchmark_kind") != "capability"
    ):
        raise ValueError("resume report must be a supported capability acceptance report")
    actor = report.get("actor")
    principal = actor.get("principal") if isinstance(actor, Mapping) else None
    execution_id, base_url = report.get("execution_id"), report.get("base_url")
    if (
        not isinstance(principal, Mapping)
        or not isinstance(execution_id, str)
        or not isinstance(base_url, str)
    ):
        raise TypeError("resume report execution identity is incomplete")
    profile = _report_model_profile(report)
    _validate_report_model_profile(report, profile)
    saved_identity = _execution_identity(execution_id, base_url, principal, profile)
    if saved_identity != identity or (
        "execution_identity" in report and report["execution_identity"] != identity
    ):
        raise ValueError("resume report execution identity does not match this execution")
    _submission_journal_from_report(report, identity)
    by_id = copy.deepcopy(_canonical_case_index(report.get("cases")))
    expected_ids = {f"{scale}:{route}" for _, scale, route, _ in _ACCEPTANCE_CASES}
    raw_history = report.get("attempt_history", [])
    if not isinstance(raw_history, list):
        raise TypeError("resume attempt_history must be a list")
    archived_attempts: set[tuple[str, int]] = set()
    for item in raw_history:
        if not isinstance(item, dict) or "attempt_history" in item or "cases" in item:
            raise ValueError("resume attempt_history must contain flat case evidence")
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or case_id not in expected_ids:
            raise ValueError("resume attempt_history contains an unknown case_id")
        key = (case_id, _case_attempt(item))
        if key in archived_attempts:
            raise ValueError("resume attempt_history contains a duplicate attempt")
        archived_attempts.add(key)
        attempt_history.append(copy.deepcopy(item))
    cases = []
    for _, scale, route, case_key in _ACCEPTANCE_CASES:
        case = by_id.get(f"{scale}:{route}")
        if case is None:
            continue
        if not _has_complete_core_evidence(
            case, safe_execution_id=_safe_identifier(execution_id), case_key=case_key
        ):
            # Archive before normalizing flags or saving the pre-retry checkpoint.
            key = (cast(str, case["case_id"]), _case_attempt(case))
            if key not in archived_attempts:
                attempt_history.append(copy.deepcopy(case))
                archived_attempts.add(key)
            case.update(
                {
                    "status": "failed",
                    "core_acceptance_ok": False,
                    "automated_acceptance_complete": False,
                    "real_device_acceptance_complete": False,
                    "acceptance_complete": False,
                }
            )
        cases.append(case)
    return cases


def _case_attempt(case: Mapping[str, object] | None) -> int:
    attempt = case.get("attempt", 1) if case is not None else 1
    if type(attempt) is not int or attempt < 1:
        raise ValueError("resume case attempt must be a positive integer")
    return attempt


def _case_execution_token(execution_id: str, case_key: str, attempt: int) -> str:
    if attempt == 1:
        return f"{execution_id}-{case_key}"
    # Keep retry identity within the runner's 64-character workspace limit.
    digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]
    return f"{case_key}-retry-{attempt}-{digest}"


def _has_complete_core_evidence(
    case: Mapping[str, object], *, safe_execution_id: str, case_key: str
) -> bool:
    if (
        case.get("core_acceptance_ok") is not True
        or case.get("automated_acceptance_complete") is not True
        or case.get("status") not in ("passed", "pending_real_device")
        or case.get("errors", []) != []
    ):
        return False
    profile = _report_model_profile(case)
    scope = case.get("model_scope_evidence")
    run_payload = case.get("run")
    if (
        not isinstance(scope, Mapping)
        or scope.get("ok") is not True
        or not isinstance(run_payload, Mapping)
        or _model_scope_errors(
            scope,
            logical_model=cast(str, profile["direct_model"]) if profile is not None else None,
            result_run_id=run_payload.get("run_id"),
        )
    ):
        return False
    sections = ("run", "project", "conversation", "public_artifacts", "dynamic_web_preview")
    if any(not isinstance(case.get(key), Mapping) for key in sections):
        return False
    run, project, conversation, public, preview = (
        cast(Mapping[str, object], case[key]) for key in sections
    )
    case_id, scale = case.get("case_id"), case.get("scale")
    if (
        not isinstance(case_id, str)
        or not isinstance(scale, str)
        or case_id != f"{scale}:{case.get('route_intent')}"
    ):
        return False
    attempt = _case_attempt(case)
    scope_token = _case_execution_token(safe_execution_id, case_key, attempt)
    project_id = _bounded_identifier(f"uat-{scope_token}", 128)
    conversation_id = _bounded_identifier(f"conv-{scope_token}", 128)
    session_id = _safe_workspace_session_token(conversation_id, scope_token)
    root = (
        f"/api/v1/workspaces/projects/{quote(project_id, safe='')}"
        f"/sessions/{quote(session_id, safe='')}"
    )
    if (
        project.get("project_id") != project_id
        or conversation.get("conversation_id") != conversation_id
        or project.get("workspace_path") != session_id
        or conversation.get("workspace_path") != session_id
        or conversation.get("project_id") != project_id
        or public.get("files_endpoint") != f"{root}/files"
        or public.get("bundle_endpoint") != f"{root}/bundle/download"
        or run.get("case_id") != case_id
        or run.get("status") != "completed"
        or not isinstance(run.get("run_id"), str)
        or not str(run["run_id"]).strip()
        or run.get("ok") is not True
        or run.get("errors") != []
        or run.get("missing_evidence") != []
    ):
        return False
    evidence = run.get("evidence")
    if not isinstance(evidence, dict) or any(
        type(value) is not bool for value in evidence.values()
    ):
        return False
    string_fields = (
        "observed_mode",
        "final_observed_mode",
        "requested_mode",
        "route_reason",
        "mode_source",
        "effective_scale",
        "final_effective_scale",
        "artifact_origin",
        "workspace_bundle_source",
    )
    if any(run.get(key) is not None and not isinstance(run.get(key), str) for key in string_fields):
        return False
    if run.get("final_observed_mode") not in ("direct", "dispatch", "hybrid"):
        return False
    if run.get("final_effective_scale") not in ("small", "medium", "large", "ultra"):
        return False
    participants, event_kinds = run.get("participant_agent_ids"), run.get("participant_event_kinds")
    event_count = run.get("participant_event_count")
    if (
        not isinstance(participants, list)
        or any(not isinstance(item, str) for item in participants)
        or not isinstance(event_kinds, list)
        or any(not isinstance(item, str) for item in event_kinds)
        or type(event_count) is not int
        or event_count < 0
    ):
        return False
    validated_manifest = _validated_manifest(run.get("validated_workspace_manifest"))
    if not _public_bundle_matches_validation(validated_manifest, public):
        return False
    scale_validation = run.get("scale_validation")
    if scale_validation is not None and not isinstance(scale_validation, dict):
        return False
    result = ProjectScaleCaseResult(
        case_id=case_id,
        run_id=cast(str, run["run_id"]),
        status="completed",
        evidence=cast(dict[str, bool], evidence),
        observed_mode=cast(str | None, run.get("observed_mode")),
        final_observed_mode=cast(str | None, run.get("final_observed_mode")),
        requested_mode=cast(str | None, run.get("requested_mode")),
        route_reason=cast(str | None, run.get("route_reason")),
        mode_source=cast(str | None, run.get("mode_source")),
        effective_scale=cast(str | None, run.get("effective_scale")),
        final_effective_scale=cast(str | None, run.get("final_effective_scale")),
        artifact_origin=cast(str | None, run.get("artifact_origin")),
        workspace_bundle_source=cast(str | None, run.get("workspace_bundle_source")),
        participant_agent_ids=tuple(cast(list[str], participants)),
        participant_event_kinds=tuple(cast(list[str], event_kinds)),
        participant_event_count=event_count,
        validated_workspace_manifest=validated_manifest,
        scale_validation=scale_validation,
    )
    if not result.ok or run.get("required_evidence") != list(result.required_evidence):
        return False
    if (
        public.get("ok") is not True
        or public.get("source") != "public_workspace_api"
        or public.get("admin_internal_run_data_used") is not False
        or public.get("zip_crc_ok") is not True
        or public.get("metadata_matches_zip") is not True
        or type(public.get("unsafe_member_count")) is not int
        or public.get("unsafe_member_count") != 0
        or public.get("errors") != []
        or not isinstance(public.get("zip_sha256"), str)
        or _SHA256_RE.fullmatch(cast(str, public["zip_sha256"])) is None
    ):
        return False
    for key in ("file_count", "downloaded_file_count", "zip_member_count", "zip_size_bytes"):
        value = public.get(key)
        if type(value) is not int or value <= 0:
            return False
    if not public["file_count"] == public["downloaded_file_count"] == public["zip_member_count"]:
        return False
    if (
        preview.get("status") != "passed"
        or preview.get("errors") != []
        or any(
            preview.get(key) is not True
            for key in (
                "counted_as_passed",
                "reachable_preview_url",
                "current_preview_matches",
                "renewed",
                "lease_extended",
                "stopped",
                "revoked_after_stop",
            )
        )
        or preview.get("capability_token_retained") is not False
    ):
        return False
    for key in ("referenced_asset_count", "referenced_assets_loaded", "content_size_bytes"):
        value = preview.get(key)
        if type(value) is not int or value < (1 if key == "content_size_bytes" else 0):
            return False
    if preview["referenced_asset_count"] != preview["referenced_assets_loaded"]:
        return False
    rebuilt = build_case_report(
        scale=scale,
        project=project,
        conversation=conversation,
        result=result,
        public_artifacts=public,
        dynamic_web_preview=preview,
        logical_model=cast(str, profile["direct_model"]) if profile is not None else None,
        model_scope_evidence=cast(Mapping[str, object] | None, case.get("model_scope_evidence")),
    )
    compared_fields = (
        "case_kind",
        "route_intent",
        "scale_fidelity_ok",
        "scale_specific_evidence_ok",
        "route_policy_ok",
        "artifact_origin_ok",
        "validated_bundle_matches",
        "multi_agent_evidence_ok",
        "build_and_test",
        "success_basis",
        "artifact_provenance",
        "observed_mode",
        "initial_observed_mode",
        "final_observed_mode",
        "route_observed_mode",
        "requested_mode",
        "route_reason",
        "mode_source",
        "effective_scale",
        "initial_effective_scale",
        "final_effective_scale",
        "observed_route_ok",
        "exact_mode_coverage_ok",
        "coverage_credit",
    )
    return rebuilt["core_acceptance_ok"] is True and json.dumps(
        {key: case.get(key) for key in compared_fields}, sort_keys=True
    ) == json.dumps({key: rebuilt[key] for key in compared_fields}, sort_keys=True)


def _safe_identifier(value: str) -> str:
    normalized = _SAFE_ID_RE.sub("-", value.strip().casefold()).strip("-")
    return normalized or uuid4().hex[:12]


def _is_explicit_preview_not_found(error: Exception) -> bool:
    message = str(error).casefold()
    return "status=404" in message or "http 404" in message


def _bounded_identifier(value: str, limit: int) -> str:
    return value[:limit].rstrip("-")


def _default_execution_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"


def _save_report(path: str, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_report(path: str | None, payload: Mapping[str, object]) -> None:
    if path:
        _save_report(path, payload)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")


def _validated_device_result(
    evidence: Mapping[str, object],
    key: str,
) -> dict[str, object]:
    result = evidence.get(key)
    if not isinstance(result, Mapping) or result.get("passed") is not True:
        raise ValueError(f"{key} must be a passed evidence object")
    if _timestamp(result.get("observed_at")) is None:
        raise ValueError(f"{key}.observed_at must be timezone-aware")
    viewport = result.get("viewport")
    if not isinstance(viewport, Mapping):
        raise TypeError(f"{key}.viewport is required")
    width = viewport.get("width")
    height = viewport.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError(f"{key}.viewport must contain positive integer dimensions")
    if key.startswith("desktop") and width < 1024:
        raise ValueError("desktop_browser_interaction viewport must be at least 1024px wide")
    if key.startswith("mobile") and (width <= 0 or width > 600):
        raise ValueError("mobile_browser_interaction viewport must be at most 600px wide")
    checks = result.get("checks")
    required_checks = (
        "login",
        "project_navigation",
        "preview_rendered",
        "preview_interaction",
        "preview_revoked",
    )
    if not isinstance(checks, Mapping) or any(
        checks.get(name) is not True for name in required_checks
    ):
        raise ValueError(f"{key}.checks must pass every required real-user interaction")
    return copy.deepcopy(dict(result))


def _case_scope_value(
    case: Mapping[str, object],
    section: str,
    key: str,
) -> str:
    value = case.get(section)
    if not isinstance(value, Mapping):
        raise TypeError(f"case evidence scope is missing {section}.{key}")
    scoped = value.get(key)
    if not isinstance(scoped, str) or not scoped.strip():
        raise ValueError(f"case evidence scope is missing {section}.{key}")
    return scoped


def case_browser_collection_scope(
    report: Mapping[str, object], case_id: str,
) -> dict[str, object]:
    """Return verified collection inputs for one completed case, including partial matrices."""
    profile = _report_model_profile(report)
    actor, execution_id, base_url = (
        report.get("actor"), report.get("execution_id"), report.get("base_url")
    )
    principal = actor.get("principal") if isinstance(actor, Mapping) else None
    if (
        not isinstance(principal, Mapping)
        or not isinstance(execution_id, str)
        or not isinstance(base_url, str)
    ):
        raise TypeError("browser collection requires complete execution identity")
    identity = _execution_identity(execution_id, base_url, principal, profile)
    # Reuse authenticated resume validation without requests, writes or promotion.
    _resume_cases(report, identity, [])
    case_keys = {f"{scale}:{route}": key for _, scale, route, key in _ACCEPTANCE_CASES}
    by_id = _canonical_case_index(report.get("cases"))
    if not isinstance(case_id, str) or case_id not in by_id:
        raise ValueError("browser collection requires an existing canonical case")
    case = by_id[case_id]
    if not _has_complete_core_evidence(
        case, safe_execution_id=_safe_identifier(execution_id), case_key=case_keys[case_id],
    ):
        raise ValueError(f"browser collection case evidence must pass core acceptance: {case_id}")
    manifest = _validated_manifest(cast(Mapping[str, object], case["run"])[
        "validated_workspace_manifest"
    ])
    if manifest is None:
        raise ValueError("browser collection requires a validated workspace manifest")
    return {
        "scope": _browser_case_scope(case, identity),
        "validated_manifest": {path: list(item) for path, item in manifest.items()},
    }


def _browser_case_scope(
    case: Mapping[str, object], identity: Mapping[str, object],
) -> dict[str, object]:
    return {
        "execution_identity": copy.deepcopy(dict(identity)),
        "case_id": case["case_id"],
        "project_id": _case_scope_value(case, "project", "project_id"),
        "conversation_id": _case_scope_value(case, "conversation", "conversation_id"),
        "run_id": _case_scope_value(case, "run", "run_id"),
        "workspace_session_id": _case_scope_value(case, "conversation", "workspace_path"),
    }


def _validated_case_device_result(
    result: object,
    *,
    case_id: str,
    device: str,
    evidence_root: Path,
    expected_scope: Mapping[str, object],
    validated_manifest: Mapping[str, tuple[int, str]],
) -> dict[str, object]:
    label = f"case evidence {case_id}.{device}"
    if not isinstance(result, Mapping) or result.get("passed") is not True:
        raise ValueError(f"{label} must be a passed evidence object")
    if _timestamp(result.get("observed_at")) is None:
        raise ValueError(f"{label}.observed_at must be timezone-aware")
    evidence_ref = result.get("evidence_ref")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError(f"{label}.evidence_ref is required")
    checks = result.get("checks")
    required_checks = ("preview_rendered", "preview_interaction", "preview_revoked")
    if not isinstance(checks, Mapping) or any(
        checks.get(name) is not True for name in required_checks
    ):
        raise ValueError(f"{label}.checks must pass every required preview interaction")
    return validate_case_browser_bundle(
        result, evidence_root=evidence_root, expected_scope=expected_scope,
        validated_manifest=validated_manifest, device=device,
    )


def _validated_case_evidence(
    automated_report: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    identity: Mapping[str, object],
    evidence_root: Path | None,
) -> dict[str, dict[str, object]]:
    by_id = _canonical_case_index(automated_report.get("cases"))
    if not by_id:
        raise ValueError("automated report must contain case evidence scopes")
    case_keys = {f"{scale}:{route}": key for _, scale, route, key in _ACCEPTANCE_CASES}
    expected_ids = set(case_keys)
    execution_id = automated_report.get("execution_id")
    if not isinstance(execution_id, str):
        raise TypeError("automated report core evidence requires execution_id")
    for case_id, case in by_id.items():
        if not _has_complete_core_evidence(
            case, safe_execution_id=_safe_identifier(execution_id), case_key=case_keys[case_id]
        ):
            raise ValueError(f"automated report case evidence must pass core acceptance: {case_id}")
    if set(by_id) != expected_ids:
        raise ValueError(
            "automated report case evidence must contain every canonical case exactly once"
        )
    raw_evidence = evidence.get("cases")
    if raw_evidence is None:
        raise ValueError("real-device case evidence is required")
    if not isinstance(raw_evidence, Mapping):
        raise TypeError("real-device case evidence must be an object")
    if set(raw_evidence) != expected_ids:
        raise ValueError("real-device case evidence must match the canonical case set exactly")
    if not isinstance(evidence_root, Path):
        raise TypeError("real-device evidence_root must be an explicit trusted Path")
    validated: dict[str, dict[str, object]] = {}
    for case_id, case in by_id.items():
        item = raw_evidence.get(case_id)
        if item is None:
            raise ValueError(f"case evidence is missing for {case_id}")
        if not isinstance(item, Mapping):
            raise TypeError(f"case evidence must be an object for {case_id}")
        expected_scope = {
            "project_id": _case_scope_value(case, "project", "project_id"),
            "conversation_id": _case_scope_value(case, "conversation", "conversation_id"),
            "run_id": _case_scope_value(case, "run", "run_id"),
        }
        for key, expected in expected_scope.items():
            if item.get(key) != expected:
                raise ValueError(f"case evidence {case_id}.{key} does not match the report")
        browser_scope = _browser_case_scope(case, identity)
        manifest = _validated_manifest(cast(Mapping[str, object], case["run"])[
            "validated_workspace_manifest"
        ])
        if manifest is None:
            raise ValueError(f"case evidence {case_id} requires a validated manifest")
        validated[case_id] = {
            **expected_scope,
            "desktop": _validated_case_device_result(
                item.get("desktop"),
                case_id=case_id,
                device="desktop",
                evidence_root=evidence_root,
                expected_scope=browser_scope,
                validated_manifest=manifest,
            ),
            "mobile": _validated_case_device_result(
                item.get("mobile"),
                case_id=case_id,
                device="mobile",
                evidence_root=evidence_root,
                expected_scope=browser_scope,
                validated_manifest=manifest,
            ),
        }
    return validated


def finalize_real_device_acceptance(
    automated_report: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    evidence_root: Path | None = None,
) -> dict[str, object]:
    """Merge deployed desktop/mobile evidence into a completed acceptance report."""

    _reject_deferred_report(automated_report)
    profile = _report_model_profile(automated_report)
    _validate_report_model_profile(automated_report, profile)
    if _report_model_profile(evidence) != profile:
        raise ValueError("real-device evidence model profile does not match automated report")
    if (
        automated_report.get("kind") != "real_user_four_scale_acceptance"
        or type(automated_report.get("schema_version")) is not int
        or automated_report.get("schema_version") != 1
        or automated_report.get("benchmark_kind") != "capability"
    ):
        raise ValueError("automated report must be a supported capability acceptance report")
    if type(evidence.get("schema_version")) is not int or evidence.get("schema_version") != 2:
        raise ValueError("real-device evidence requires supported integer schema_version 2")
    if automated_report.get("errors", []) != []:
        raise ValueError("automated report errors must be empty before finalization")
    if (
        automated_report.get("core_acceptance_ok") is not True
        or automated_report.get("automated_acceptance_complete") is not True
    ):
        raise ValueError("automated acceptance must pass before real-device finalization")
    execution_id = automated_report.get("execution_id")
    if not isinstance(execution_id, str) or evidence.get("execution_id") != execution_id:
        raise ValueError("real-device evidence execution_id does not match the automated report")
    actor, base_url = automated_report.get("actor"), automated_report.get("base_url")
    principal = actor.get("principal") if isinstance(actor, Mapping) else None
    if not isinstance(principal, Mapping) or not isinstance(base_url, str):
        raise TypeError("automated report execution identity is incomplete")
    identity = _execution_identity(execution_id, base_url, principal, profile)
    if (
        "execution_identity" in automated_report
        and automated_report["execution_identity"] != identity
    ):
        raise ValueError("automated report execution identity is inconsistent")
    device_identity = evidence.get("execution_identity")
    if not isinstance(device_identity, Mapping) or device_identity != identity:
        raise ValueError("real-device evidence execution identity must match the automated identity")
    if "base_url" in evidence and evidence["base_url"] != identity["base_url"]:
        raise ValueError("real-device evidence base_url contradicts its execution identity")
    if "actor" in evidence:
        device_actor = evidence["actor"]
        device_principal = (
            device_actor.get("principal") if isinstance(device_actor, Mapping) else None
        )
        if not isinstance(device_principal, Mapping) or any(
            device_principal.get(key) != identity[key] for key in ("user_id", "tenant_id")
        ):
            raise ValueError("real-device evidence actor.principal contradicts its execution identity")
    _submission_journal_from_report(automated_report, identity)
    desktop = _validated_device_result(evidence, "desktop_browser_interaction")
    mobile = _validated_device_result(evidence, "mobile_browser_interaction")
    case_evidence = _validated_case_evidence(
        automated_report, evidence, identity=identity, evidence_root=evidence_root,
    )
    coverage = _matrix_mode_coverage(automated_report.get("cases"), execution_id=execution_id)
    for key, value in coverage.items():
        if key in automated_report and (
            type(automated_report[key]) is not type(value) or automated_report[key] != value
        ):
            raise ValueError(f"automated report mode coverage is inconsistent: {key}")
    if coverage["exact_mode_coverage_complete"] is not True:
        raise ValueError("automated report exact mode coverage must pass before finalization")

    completed = copy.deepcopy(dict(automated_report))
    completed.update(coverage)
    completed["status"] = "passed"
    completed["real_device_acceptance_complete"] = True
    completed["acceptance_complete"] = True
    completed["real_device_acceptance"] = {
        "schema_version": evidence["schema_version"],
        "execution_id": evidence["execution_id"],
        "execution_identity": copy.deepcopy(dict(device_identity)),
        "status": "passed",
        "counted_as_complete": True,
        "desktop_browser_interaction": desktop,
        "mobile_browser_interaction": mobile,
        "cases": case_evidence,
    }
    if profile is not None:
        cast(dict[str, object], completed["real_device_acceptance"])["model_profile"] = (
            copy.deepcopy(profile)
        )
    dynamic_preview = completed.get("dynamic_web_preview")
    if isinstance(dynamic_preview, dict):
        dynamic_preview["status"] = "passed"
        dynamic_preview["counted_as_passed"] = True
    cases = completed.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict) or case.get("core_acceptance_ok") is not True:
                continue
            case["status"] = "passed"
            case["real_device_acceptance_complete"] = True
            case["acceptance_complete"] = True
            case_id = case.get("case_id")
            if isinstance(case_id, str):
                case["real_device_evidence"] = case_evidence[case_id]
            preview = case.get("dynamic_web_preview")
            if isinstance(preview, dict):
                preview["browser_interaction"] = "verified_by_deployed_real_device_acceptance"
        completed["case_count"] = len(cases)
        completed["failed_case_count"] = sum(1 for case in cases if case["status"] == "failed")
        completed["pending_case_count"] = sum(
            1 for case in cases if case["status"] == "pending_real_device"
        )
    return completed


def _is_finalized_report(report: Mapping[str, object]) -> bool:
    device = report.get("real_device_acceptance")
    return (
        report.get("status") == "passed"
        or report.get("acceptance_complete") is True
        or report.get("real_device_acceptance_complete") is True
        or isinstance(device, Mapping)
        and (device.get("status") == "passed" or device.get("counted_as_complete") is True)
    )


def _validated_finalized_report(
    report: Mapping[str, object], *, evidence_root: Path | None = None,
) -> dict[str, object]:
    device = report.get("real_device_acceptance")
    if not isinstance(device, Mapping) or report.get("errors", []) != []:
        raise ValueError("finalized report requires complete real-device evidence")
    rebuilt = finalize_real_device_acceptance(report, device, evidence_root=evidence_root)
    if json.dumps(rebuilt, sort_keys=True) != json.dumps(dict(report), sort_keys=True):
        raise ValueError("finalized report contradicts its complete acceptance evidence")
    return copy.deepcopy(dict(report))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_mapping(path: str) -> dict[str, object]:
    parsed = json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(parsed, dict):
        raise TypeError(f"JSON file must contain an object: {path}")
    return cast(dict[str, object], parsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run small/medium/large/ultra capability acceptance as one logged-in user. "
            "Exit 0 means complete, 1 means failed, and 2 means core passed with pending "
            "exact mode coverage or real-device browser evidence."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS", "20")),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_WAIT_SECONDS", "0")),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_POLL_INTERVAL_SECONDS", "2")),
    )
    parser.add_argument(
        "--artifact-build-timeout",
        type=float,
        default=float(
            os.environ.get("AGENT_HUB_PROJECT_SCALE_ARTIFACT_BUILD_TIMEOUT_SECONDS", "120")
        ),
    )
    parser.add_argument(
        "--execution-id",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID"),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_REPORT_PATH"),
    )
    parser.add_argument(
        "--defer-preview", action="store_true",
        help="Validate delivery only; do not start previews. Reports cannot resume or finalize.",
    )
    parser.add_argument(
        "--stop-on-failure", action="store_true",
        help="Stop after saving the first known failed case; unknown submissions always stop.",
    )
    parser.add_argument("--finalize-report")
    parser.add_argument("--real-device-evidence")
    parser.add_argument("--evidence-root", type=Path, help="Trusted browser evidence directory.")
    parser.add_argument("--logical-model", help="Scope this test to one safe logical model ID.")
    parser.add_argument(
        "--resume-report",
        "--resume",
        dest="resume_report",
        help="Resume matching core evidence and retry failed/incomplete cases from this JSON report.",
    )
    args = parser.parse_args(argv)
    try:
        profile = _model_profile(args.logical_model)
    except ValueError as error:
        parser.error(str(error))

    if args.finalize_report or args.real_device_evidence:
        if args.defer_preview:
            parser.error("--defer-preview cannot be combined with real-device finalization")
        if args.resume_report:
            parser.error("--resume-report cannot be combined with real-device finalization")
        if not args.finalize_report or not args.real_device_evidence:
            parser.error("--finalize-report and --real-device-evidence must be used together")
        try:
            automated = _read_json_mapping(args.finalize_report)
            _reject_deferred_report(automated)
            device_evidence = _read_json_mapping(args.real_device_evidence)
            saved_profile = _report_model_profile(automated)
            _validate_report_model_profile(automated, saved_profile)
            if args.logical_model is not None and profile != saved_profile:
                raise ValueError("model profile does not match --logical-model")
            if _report_model_profile(device_evidence) != saved_profile:
                raise ValueError(
                    "real-device evidence model profile does not match automated report"
                )
        except (OSError, TypeError, ValueError) as error:
            parser.error(str(error))
        try:
            payload = finalize_real_device_acceptance(
                automated,
                device_evidence,
                evidence_root=(
                    args.evidence_root.absolute() if args.evidence_root is not None
                    else Path(args.real_device_evidence).absolute().parent
                ),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            if args.output and Path(args.output).resolve() == Path(args.finalize_report).resolve():
                print(f"finalization rejected; source checkpoint preserved: {error}", file=sys.stderr)
                return 1
            payload = {
                "schema_version": 1,
                "kind": "real_user_four_scale_acceptance",
                "status": "failed",
                "acceptance_complete": False,
                "errors": [str(error)],
            }
        _write_report(args.output, payload)
        return 0 if payload.get("acceptance_complete") is True else 1

    resume_report = None
    if args.resume_report:
        if args.defer_preview:
            parser.error("deferred preview execution cannot resume existing reports")
        try:
            resume_report = _read_json_mapping(args.resume_report)
            _reject_deferred_report(resume_report)
            _validate_report_model_profile(resume_report, profile)
        except (OSError, TypeError, ValueError) as error:
            parser.error(f"cannot read resume report: {error}")
        saved_execution_id = resume_report.get("execution_id")
        if not isinstance(saved_execution_id, str) or not saved_execution_id.strip():
            parser.error("resume report execution identity requires execution_id")
        if args.execution_id is not None and args.execution_id != saved_execution_id:
            parser.error("resume report execution identity does not match --execution-id")
        args.execution_id = saved_execution_id
        args.output = args.output or args.resume_report
    args.execution_id = args.execution_id or _default_execution_id()

    if args.resume_report is None and args.output and os.path.lexists(args.output):
        parser.error("fresh acceptance cannot overwrite an existing checkpoint")

    username, password, tenant_id = _acceptance_credentials_from_env()
    bearer_token = os.environ.get("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "").strip()
    if not bearer_token and (not username or not password):
        parser.error(
            "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or "
            "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required "
            "(AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME/PASSWORD is also accepted)"
        )
    login_username = None if bearer_token else username
    login_password = None if bearer_token else password

    delegate = UrllibAcceptanceClient(
        base_url=args.base_url,
        bearer_token=bearer_token,
        timeout=args.timeout,
        username=login_username,
        password=login_password,
        tenant_id=tenant_id,
    )
    client = RealUserAcceptanceClient(delegate)
    try:
        payload = run_real_user_four_scale_acceptance(
            client,
            username=username or "bearer-token",
            base_url=args.base_url,
            execution_id=args.execution_id,
            wait_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_interval,
            artifact_build_timeout_seconds=args.artifact_build_timeout,
            authentication_method="bearer_token" if bearer_token else "password",
            output_path=args.output,
            resume_report=resume_report,
            logical_model=args.logical_model,
            defer_preview=args.defer_preview,
            stop_on_failure=args.stop_on_failure,
            evidence_root=(
                args.evidence_root.absolute() if args.evidence_root is not None else None
            ),
            progress=lambda message: print(
                f"real-user-four-scale progress: {message}",
                file=sys.stderr,
                flush=True,
            ),
        )
    except Exception as error:  # noqa: BLE001 - emit a machine-readable authentication/setup failure.
        payload = {
            "schema_version": 1,
            "kind": "real_user_four_scale_acceptance",
            "status": "failed",
            "core_acceptance_ok": False,
            "automated_acceptance_complete": False,
            "real_device_acceptance_complete": False,
            "acceptance_complete": False,
            "base_url": args.base_url.rstrip("/"),
            "execution_id": args.execution_id,
            "errors": [str(error)],
            "dynamic_web_preview": _dynamic_preview_pending(),
        }
        # Authentication, identity, and save errors must not overwrite durable case evidence.
        finalized_resume = resume_report is not None and _is_finalized_report(resume_report)
        if finalized_resume or args.output and Path(args.output).exists():
            checkpoint = args.resume_report if finalized_resume else args.output
            print(f"preserved checkpoint: {checkpoint}", file=sys.stderr, flush=True)
            _write_report(None, payload)
            return 1
    finalized_resume = resume_report is not None and payload.get("acceptance_complete") is True
    _write_report(None if finalized_resume else args.output, payload)
    if payload.get("status") == "failed":
        return 1
    if payload.get("acceptance_complete") is not True:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
