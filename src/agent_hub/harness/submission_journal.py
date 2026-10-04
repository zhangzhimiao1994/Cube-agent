"""Submission bookkeeping; callers own persistence and HTTP transport."""

import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from hashlib import sha256
from typing import cast

_RESPONSE_KEYS = frozenset({
    "id", "tenant_id", "status", "mode", "version", "project_id", "conversation_id",
    "workspace_session_id", "requested_mode", "effective_mode", "effective_scale",
    "route_reason", "mode_source",
})
_SCOPE_KEYS = frozenset({"project_id", "conversation_id", "workspace_session_id"})
_REQUIRED_RESPONSE_KEYS = _SCOPE_KEYS | {"id", "tenant_id", "status"}
_CONTEXT_KEYS = _SCOPE_KEYS | {"case_id", "attempt"}
_IDENTITY_KEYS = frozenset({"user_id", "tenant_id", "execution_id", "base_url"})
_RECORD_KEYS = frozenset({
    "path", "idempotency_key", "request_sha256", "context", "state", "response",
})


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return cast(dict[str, object], value)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_json_value(value: object) -> bool:
    if value is None or type(value) in (str, int, float, bool):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _canonical_json(value: object) -> bytes:
    try:
        if not _is_json_value(value):
            raise ValueError("submission data must contain only JSON values")
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("submission data must be finite JSON") from exc


def _validate_identity(value: object) -> dict[str, object]:
    identity = _object(value, "identity")
    if not _IDENTITY_KEYS <= identity.keys() or identity.keys() - _IDENTITY_KEYS - {"model_profile"}:
        raise ValueError("identity has invalid fields")
    if any(not _nonempty_string(identity[key]) for key in _IDENTITY_KEYS):
        raise ValueError("identity fields must be nonempty strings")
    if "model_profile" in identity:
        _object(identity["model_profile"], "model_profile")
    _canonical_json(identity)
    return identity


def _submission_key(path: object, key: object) -> tuple[str, str | None]:
    if not isinstance(path, str):
        raise ValueError("submission path must be a string")  # noqa: TRY004
    is_run = path in ("/runs", "/api/v1/runs")
    is_repair = re.fullmatch(r"(?:/api/v1)?/runs/[^/\s?#]+/accept-repair", path) is not None
    if not is_run and not is_repair:
        raise ValueError("unsupported submission path")
    if key is None and is_repair:
        return path, None
    if not isinstance(key, str) or not key.strip():
        raise ValueError("submission requires a nonempty idempotency key")
    return path, key


def _validate_context(value: object) -> dict[str, object]:
    context = _object(value, "submission context")
    if context.keys() != _CONTEXT_KEYS:
        raise ValueError("submission context has invalid fields")
    if not isinstance(context["case_id"], str):
        raise ValueError("submission case_id must be a string")  # noqa: TRY004
    attempt = context["attempt"]
    if type(attempt) is not int or attempt <= 0:
        raise ValueError("submission attempt must be a positive integer")
    if any(not _nonempty_string(context[key]) for key in _SCOPE_KEYS):
        raise ValueError("submission scope must contain nonempty strings")
    return context


def _validate_response(
    value: object, context: dict[str, object], identity: dict[str, object]
) -> dict[str, object]:
    response = _object(value, "submission response")
    if not _REQUIRED_RESPONSE_KEYS <= response.keys() or response.keys() - _RESPONSE_KEYS:
        raise ValueError("submission response has invalid fields")
    if not _nonempty_string(response["id"]) or not _nonempty_string(response["status"]):
        raise ValueError("submission response requires nonempty id and status")
    if response["tenant_id"] != identity["tenant_id"]:
        raise ValueError("submission response tenant mismatch")
    if any(response[key] != context[key] for key in _SCOPE_KEYS):
        raise ValueError("submission response scope mismatch")
    if "version" in response:
        version = response["version"]
        if type(version) is not int or version <= 0:
            raise ValueError("submission response version must be a positive integer")
    for key in response.keys() - _REQUIRED_RESPONSE_KEYS - {"version"}:
        if response[key] is not None and not isinstance(response[key], str):
            raise ValueError("submission response routing metadata must be strings or null")
    return response


def _load_records(snapshot: object, identity: dict[str, object]) -> list[dict[str, object]]:
    root = _object(snapshot, "submission snapshot")
    if root.keys() != {"schema_version", "identity", "records"}:
        raise ValueError("submission snapshot has invalid fields")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("unsupported submission snapshot schema_version")
    stored_identity = _validate_identity(root["identity"])
    if _canonical_json(stored_identity) != _canonical_json(identity):
        raise ValueError("submission snapshot identity mismatch")
    if not isinstance(root["records"], list):
        raise ValueError("submission snapshot records must be a list")  # noqa: TRY004
    records: list[dict[str, object]] = []
    seen: set[tuple[str, str | None]] = set()
    for value in root["records"]:
        record = _object(value, "submission record")
        if record.keys() != _RECORD_KEYS:
            raise ValueError("submission record has invalid fields")
        key = _submission_key(record["path"], record["idempotency_key"])
        if key in seen:
            raise ValueError("duplicate submission path and idempotency key")
        seen.add(key)
        digest = record["request_sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("submission request_sha256 must be a lowercase SHA256 digest")
        context = _validate_context(record["context"])
        if record["state"] == "confirmed":
            _validate_response(record["response"], context, identity)
        elif record["state"] == "unresolved":
            if record["response"] is not None:
                raise ValueError("unresolved submission must not contain a response")
        else:
            raise ValueError("invalid submission state")
        records.append(deepcopy(record))
    return records


class SubmissionJournal:
    """Persist intent before sending, then retain only a scoped response receipt."""

    def __init__(self, identity: Mapping[str, object], snapshot: object | None = None) -> None:
        self._identity = deepcopy(_validate_identity(dict(identity)))
        self._records = [] if snapshot is None else _load_records(snapshot, self._identity)

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "identity": deepcopy(self._identity),
            "records": deepcopy(self._records),
        }

    @property
    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(deepcopy(self._records))

    @property
    def has_unresolved(self) -> bool:
        return any(record["state"] == "unresolved" for record in self._records)

    def prepare(
        self,
        *,
        path: str,
        body: dict[str, object],
        idempotency_key: str | None,
        context: dict[str, object],
        persist: Callable[[], None],
    ) -> tuple[int, dict[str, object] | None]:
        if self.has_unresolved:
            raise ValueError("unresolved submission blocks further submissions")
        _submission_key(path, idempotency_key)
        _validate_context(context)
        digest = sha256(_canonical_json(_object(body, "submission body"))).hexdigest()
        for index, record in enumerate(self._records):
            if (record["path"], record["idempotency_key"]) == (path, idempotency_key):
                if record["request_sha256"] != digest or record["context"] != context:
                    raise ValueError("submission request or context mismatch")
                return index, deepcopy(cast(dict[str, object], record["response"]))
        index = len(self._records)
        self._records.append({
            "path": path,
            "idempotency_key": idempotency_key,
            "request_sha256": digest,
            "context": deepcopy(context),
            "state": "unresolved",
            "response": None,
        })
        # Retain the unresolved intent even if persistence is interrupted.
        persist()
        return index, None

    def confirm(
        self, index: int, response: dict[str, object], persist: Callable[[], None]
    ) -> None:
        if type(index) is not int or index < 0 or index >= len(self._records):
            raise ValueError("invalid submission index")
        record = self._records[index]
        if record["state"] != "unresolved":
            raise ValueError("submission is already confirmed")
        safe_response = {
            key: deepcopy(value) for key, value in _object(response, "submission response").items()
            if key in _RESPONSE_KEYS
        }
        _validate_response(safe_response, cast(dict[str, object], record["context"]), self._identity)
        record["response"] = safe_response
        record["state"] = "confirmed"
        try:
            persist()
        except BaseException:
            record["state"] = "unresolved"
            record["response"] = None
            raise
