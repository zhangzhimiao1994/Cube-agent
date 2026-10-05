"""Bounded operator evidence reads and actual image decoding."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import re
import stat
import sys
import warnings
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import UUID

from PIL import Image, ImageStat

from agent_hub.previews.cleanup import PreviewCleanupRecord, PreviewIdentityV1, timestamp
from agent_hub.previews.provenance import PreviewProvenanceV1, snapshot_manifest_for_identity

if sys.platform == "win32":
    import ctypes
else:
    ctypes = importlib.import_module("ctypes")

_MAX_IMAGE_PIXELS = 4_000_000


def _directory_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("evidence directory alias or invalid type")
    return info.st_dev, info.st_ino


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("evidence file alias or invalid type")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_nlink)


def _windows_handle_path(fd: int) -> Path:
    # Verify the opened object, not just the pathname checked before opening it.
    msvcrt = importlib.import_module("msvcrt")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.GetFinalPathNameByHandleW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = cast(int, function(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0))
    if not 0 < length < len(buffer):
        raise ValueError("cannot verify evidence handle path")
    name = buffer.value
    if name.startswith("\\\\?\\UNC\\"):
        name = "\\\\" + name[8:]
    elif name.startswith("\\\\?\\"):
        name = name[4:]
    return Path(name)


def _read_evidence_file(
    root: Path, descriptor: object, *, max_bytes: int,
) -> bytes:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("invalid evidence read budget")
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "size_bytes", "sha256"}:
        raise ValueError("invalid evidence descriptor")
    name, size, digest = descriptor["path"], descriptor["size_bytes"], descriptor["sha256"]
    if not isinstance(name, str) or not 1 <= len(name) <= 512 or "\\" in name or "\0" in name:
        raise ValueError("invalid evidence path")
    member = PurePosixPath(name)
    if (member.is_absolute() or str(member) != name or not member.parts
            or len(member.parts) > 64 or any(part in {".", ".."} or ":" in part for part in member.parts)):
        raise ValueError("noncanonical evidence path")
    if type(size) is not int or not 0 <= size <= max_bytes:
        raise ValueError("invalid evidence size")
    if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid evidence digest")
    if not root.is_absolute():
        raise ValueError("evidence root must be caller-selected absolute path")
    try:
        directories = [Path(root.anchor)]
        for part in root.parts[1:]:
            directories.append(directories[-1] / part)
        for part in member.parts[:-1]:
            directories.append(directories[-1] / part)
        facts = [_directory_identity(path) for path in directories]
        target = root.joinpath(*member.parts)
        with ExitStack() as stack:
            parent_fd: int | None = None
            if sys.platform != "win32":
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
                    raise ValueError("evidence no-follow traversal unavailable")
                for index, directory in enumerate(directories):
                    fd = (os.open(str(directory), flags) if parent_fd is None
                          else os.open(directory.name, flags, dir_fd=parent_fd))
                    stack.callback(os.close, fd)
                    info = os.fstat(fd)
                    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != facts[index]:
                        raise ValueError("evidence directory changed")
                    parent_fd = fd
                before = os.stat(member.name, dir_fd=parent_fd, follow_symlinks=False)
                fd = os.open(member.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
            else:
                before = target.lstat()
                fd = os.open(target, os.O_RDONLY | os.O_BINARY)
            stack.callback(os.close, fd)
            identity = _file_identity(before)
            opened_identity = _file_identity(os.fstat(fd))
            same_binding = opened_identity == identity
            if sys.platform == "win32":
                # Windows pathname ctime and handle ctime have different semantics.
                # Bind the object across APIs, then check each API's full tuple below.
                same_binding = (opened_identity[:4] == identity[:4]
                                and opened_identity[5] == identity[5])
            if not same_binding or before.st_size != size:
                raise ValueError("evidence file changed or size mismatch")
            if sys.platform == "win32" and _windows_handle_path(fd) != target:
                raise ValueError("opened evidence handle escapes expected path")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 131072))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = (os.stat(member.name, dir_fd=parent_fd, follow_symlinks=False)
                     if parent_fd is not None else target.lstat())
            if (_file_identity(os.fstat(fd)) != opened_identity or _file_identity(after) != identity
                    or [_directory_identity(path) for path in directories] != facts):
                raise ValueError("evidence identity changed during read")
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("evidence bytes do not match descriptor")
            return data
    except OSError:
        raise ValueError("evidence file unavailable or unstable") from None


def _parse_json_evidence(data: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate evidence JSON field")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError("non-finite evidence JSON number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite evidence JSON number")
        return number

    try:
        parsed = json.loads(data, object_pairs_hook=pairs, parse_constant=reject_constant,
                            parse_float=finite_float)
    except (UnicodeError, RecursionError):
        raise ValueError("invalid evidence JSON") from None
    if not isinstance(parsed, dict):
        raise ValueError("evidence JSON must be an object")  # noqa: TRY004 - validation contract
    return cast(dict[str, object], parsed)


def _png_observation(data: bytes, *, width: int, height: int) -> dict[str, object]:
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or width * height > _MAX_IMAGE_PIXELS):
        raise ValueError("invalid screenshot dimensions")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=["PNG"]) as image:
                if image.size != (width, height):
                    raise ValueError("screenshot dimensions mismatch")
                image.verify()
            with Image.open(BytesIO(data), formats=["PNG"]) as image:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                try:
                    background.alpha_composite(rgba)
                    with background.convert("RGB") as displayed:
                        nonblank = any(value >= 2.0 for value in ImageStat.Stat(displayed).stddev)
                finally:
                    background.close()
                    rgba.close()
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("invalid screenshot PNG") from None
    if not nonblank:
        raise ValueError("blank screenshot PNG")
    return {"width": width, "height": height, "sha256": hashlib.sha256(data).hexdigest(),
            "nonblank": True}


_VIEWPORTS = {"desktop": (1440, 960), "mobile": (390, 844)}
_SCOPE_KEYS = {"execution_identity", "case_id", "project_id", "conversation_id", "run_id",
               "workspace_session_id"}
_JSON_BYTES = 2 * 1024 * 1024
_PNG_BYTES = 8 * 1024 * 1024


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid browser evidence fields")
    return cast(dict[str, object], value)


def _text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError("invalid bounded evidence string")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid browser evidence URL digest")
    return value


def _version(value: object, expected: int) -> None:
    if type(value) is not int or value != expected:
        raise ValueError("invalid browser evidence schema version; legacy evidence unresolved")


def _list(value: object, *, maximum: int, minimum: int = 0) -> list[object]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("evidence list missing or resource budget exceeded")
    return cast(list[object], value)


def _bounded_json(value: object) -> None:
    # Bound the whole document, including otherwise unused generated-app fields.
    pending: list[tuple[object, int]] = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 100_000 or depth > 32:
            raise ValueError("evidence JSON resource budget exceeded")
        if isinstance(item, dict):
            if len(item) > 4096 or any(not isinstance(key, str) or len(key) > 512 for key in item):
                raise ValueError("evidence JSON object budget exceeded")
            pending.extend((entry, depth + 1) for entry in item.values())
        elif isinstance(item, list):
            if len(item) > 4096:
                raise ValueError("evidence JSON list budget exceeded")
            pending.extend((entry, depth + 1) for entry in item)
        elif isinstance(item, str):
            if len(item) > 262144:
                raise ValueError("evidence JSON string budget exceeded")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("non-finite evidence JSON number")
        elif item is not None and type(item) not in {bool, int}:
            raise ValueError("invalid evidence JSON value")


def _same_json(actual: object, expected: object) -> None:
    _bounded_json(actual)
    _bounded_json(expected)
    if json.dumps(actual, sort_keys=True, separators=(",", ":"), allow_nan=False) != json.dumps(
        expected, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ):
        raise ValueError("browser evidence scope or viewport mismatch")


def _scope(value: object) -> dict[str, object]:
    scope = _object(value, _SCOPE_KEYS)
    _bounded_json(scope)
    for key in _SCOPE_KEYS - {"execution_identity"}:
        _text(scope[key])
    identity = scope["execution_identity"]
    if not isinstance(identity, dict):
        raise ValueError("missing authenticated execution identity")  # noqa: TRY004 - wire validation
    keys = {"execution_id", "base_url", "tenant_id", "user_id"}
    execution = _object(identity, keys | ({"model_profile"} if "model_profile" in identity else set()))
    for key in keys:
        _text(execution[key])
    if "model_profile" in execution and not isinstance(execution["model_profile"], dict):
        raise ValueError("invalid execution model profile")
    return scope


def _bound_identity(value: object, scope: Mapping[str, object]) -> PreviewIdentityV1:
    identity = PreviewIdentityV1.from_wire(value)
    execution = cast(dict[str, object], scope["execution_identity"])
    if (identity.tenant_id != execution["tenant_id"] or identity.user_id != execution["user_id"]
            or any(getattr(identity, key) != scope[key] for key in (
                "project_id", "conversation_id", "workspace_session_id",
            ))):
        raise ValueError("preview identity owner or workspace mismatch")
    return identity


def _observation(value: object, keys: set[str], scope: dict[str, object],
                 identity: PreviewIdentityV1) -> dict[str, object]:
    data = _object(value, {"schema_version", "scope", "preview_identity"} | keys)
    _version(data["schema_version"], 1)
    _same_json(data["scope"], scope)
    if PreviewIdentityV1.from_wire(data["preview_identity"]) != identity:
        raise ValueError("observation preview identity mismatch")
    return data


def _app_path(value: object) -> str:
    name = _text(value)
    if (re.fullmatch(r"[A-Za-z0-9_./-]+", name) is None
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("unsafe relative application path")
    return name


def _number(value: object) -> float:
    if type(value) not in {int, float}:
        raise ValueError("invalid evidence bounds number")
    try:
        number = float(cast(int | float, value))
    except OverflowError:
        raise ValueError("invalid evidence bounds number") from None
    if not math.isfinite(number):
        raise ValueError("invalid evidence bounds number")
    return number


def _bounds(value: object, width: int, height: int) -> dict[str, float]:
    data = _object(value, {"x", "y", "width", "height"})
    bounds = {key: _number(entry) for key, entry in data.items()}
    if (bounds["x"] < 0 or bounds["y"] < 0 or bounds["width"] <= 0 or bounds["height"] <= 0
            or bounds["x"] + bounds["width"] > width or bounds["y"] + bounds["height"] > height):
        raise ValueError("clipped or overflowing evidence bounds")
    return bounds


def _http_success(value: object) -> None:
    if type(value) is not int or not 200 <= value < 300:
        raise ValueError("unsuccessful application HTTP observation")


def _render_observation(
    value: object, *, scope: dict[str, object], identity: PreviewIdentityV1,
    device: str, viewport: dict[str, object], frame_png: bytes | None,
) -> tuple[datetime, str, set[str]]:
    data = _observation(value, {
        "device", "viewport", "observed_at", "content_url_sha256", "title", "content_text",
        "visible_controls", "frame", "assets", "console_errors", "page_errors",
    }, scope, identity)
    if data["device"] != device:
        raise ValueError("render device mismatch")
    _same_json(data["viewport"], viewport)
    width, height = _VIEWPORTS[device]
    _text(data["title"], maximum=1024)
    _text(data["content_text"], maximum=16384)
    selectors: set[str] = set()
    control_bounds: list[dict[str, float]] = []
    for item in _list(data["visible_controls"], maximum=256, minimum=1):
        control = _object(item, {"selector", "text", "bounds"})
        selector = _text(control["selector"])
        if selector in selectors:
            raise ValueError("ambiguous duplicate app control selector")
        selectors.add(selector)
        _text(control["text"], maximum=2048)
        control_bounds.append(_bounds(control["bounds"], width, height))
    frame = data["frame"]
    if frame is None:
        if frame_png is not None:
            raise ValueError("unexpected framed screenshot")
    else:
        framed = _object(frame, {"bounds", "width", "height"})
        bounds = _bounds(framed["bounds"], width, height)
        fw, fh = framed["width"], framed["height"]
        if type(fw) is not int or type(fh) is not int or frame_png is None:
            raise ValueError("frame screenshot missing or dimensions disagree")
        capture_width = math.ceil(bounds["x"] + bounds["width"]) - math.floor(bounds["x"])
        capture_height = math.ceil(bounds["y"] + bounds["height"]) - math.floor(bounds["y"])
        if (fw, fh) != (capture_width, capture_height):
            raise ValueError("frame screenshot dimensions disagree with DPR1 CSS capture geometry")
        _png_observation(frame_png, width=fw, height=fh)
        if any(control["x"] < bounds["x"] or control["y"] < bounds["y"]
               or control["x"] + control["width"] > bounds["x"] + bounds["width"]
               or control["y"] + control["height"] > bounds["y"] + bounds["height"]
               for control in control_bounds):
            raise ValueError("observed app control falls outside the preview frame")
    for item in _list(data["assets"], maximum=512):
        asset = _object(item, {"path", "resource_type", "status", "loaded"})
        _app_path(asset["path"])
        _text(asset["resource_type"], maximum=64)
        if asset["loaded"] is not True:
            raise ValueError("relevant asset did not load")
        if type(asset["status"]) is not int or asset["status"] != 304:
            _http_success(asset["status"])
    if data["console_errors"] != [] or data["page_errors"] != []:
        raise ValueError("console or page error observation")
    return timestamp(data["observed_at"]), _digest(data["content_url_sha256"]), selectors


def _selector(value: object, *, empty: bool = False) -> list[str]:
    return [_text(key, maximum=128) for key in _list(value, maximum=16, minimum=0 if empty else 1)]


def _select(body: object, keys: list[str]) -> object:
    for key in keys:
        if not isinstance(body, dict) or key not in body:
            raise ValueError("business JSON selector does not resolve")
        body = body[key]
    return body


def _trace(value: object, *, mutation: bool = False, readback: bool = False) -> dict[str, object]:
    extras = {"input_value", "control"} if mutation else {"cache", "after_reload"} if readback else set()
    data = _object(value, {"method", "path", "observed_at", "status", "body"} | extras)
    methods = {"POST", "PUT", "PATCH"} if mutation else {"GET"}
    if not isinstance(data["method"], str) or data["method"] not in methods:
        raise ValueError("invalid business HTTP method")
    _app_path(data["path"])
    _http_success(data["status"])
    _bounded_json(data["body"])
    if len(json.dumps(data["body"], allow_nan=False).encode()) > 262144:
        raise ValueError("business JSON body resource budget exceeded")
    timestamp(data["observed_at"])
    if readback and (data["cache"] != "no-store" or data["after_reload"] is not True):
        raise ValueError("readback is not fresh after reload")
    return data


def _record_id(value: object) -> None:
    if type(value) is int:
        if not 1 <= value <= 2**53 - 1:
            raise ValueError("business record ID is outside safe integer range")
    else:
        _text(value)


def _business_observation(
    value: object, *, scope: dict[str, object], identity: PreviewIdentityV1,
    controls: set[str], rendered_at: datetime,
) -> datetime:
    data = _observation(value, {"unique_value", "selectors", "before", "mutation", "readback"}, scope, identity)
    unique = _text(data["unique_value"])
    matches = re.findall(r"(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])", unique)
    if not matches or any(str(UUID(match)) != match for match in matches):
        raise ValueError("business value lacks a fresh canonical UUID")
    fresh_markers = (unique, *matches)
    selectors = _object(data["selectors"], {
        "before_records", "before_success", "mutation_record", "readback_record", "success",
        "id_field", "value_field",
    })
    paths = {key: _selector(selectors[key], empty=key == "before_records") for key in (
        "before_records", "mutation_record", "readback_record", "success",
    )}
    id_field = _text(selectors["id_field"], maximum=128)
    value_field = _text(selectors["value_field"], maximum=128)
    if id_field == value_field:
        raise ValueError("business identity and value fields must be distinct")
    before = _trace(data["before"])
    mutation = _trace(data["mutation"], mutation=True)
    readback = _trace(data["readback"], readback=True)
    times = [timestamp(trace["observed_at"]) for trace in (before, mutation, readback)]
    if not rendered_at <= times[0] < times[1] < times[2]:
        raise ValueError("business observations are stale or out of order")
    control = _object(mutation["control"], {"selector", "visible"})
    if (control["visible"] is not True or _text(control["selector"]) not in controls
            or mutation["input_value"] != unique):
        raise ValueError("business mutation lacks an observed UI control and exact input")
    before_body = before["body"]
    root_array = isinstance(before_body, list)
    if root_array:
        if selectors["before_success"] is not None or paths["before_records"] != []:
            raise ValueError("before root array requires null success and an empty record selector")
    elif isinstance(before_body, dict):
        before_success = _selector(selectors["before_success"])
        if _select(before_body, before_success) is not True:
            raise ValueError("before query lacks application-level success")
    else:
        raise ValueError("before query is not a successful object or root record array")
    records = _list(_select(before_body, paths["before_records"]), maximum=4096)
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("before observation is not a list of records")  # noqa: TRY004
        if root_array:
            _record_id(record.get(id_field))
            _text(record.get(value_field))
        pending: list[object] = [record]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str) and any(marker in item for marker in fresh_markers):
                raise ValueError("business value was preexisting before interaction")
    observed_records: list[dict[str, object]] = []
    for trace, key in ((mutation, "mutation_record"), (readback, "readback_record")):
        if _select(trace["body"], paths["success"]) is not True:
            raise ValueError("HTTP success without application-level success")
        selected = _select(trace["body"], paths[key])
        if not isinstance(selected, dict):
            raise ValueError("business response record missing")  # noqa: TRY004 - wire validation
        record = cast(dict[str, object], selected)
        _record_id(record.get(id_field))
        if record.get(value_field) != unique:
            raise ValueError("business value is not persisted in a response record")
        observed_records.append(record)
    if (type(observed_records[0][id_field]) is not type(observed_records[1][id_field])
            or observed_records[0][id_field] != observed_records[1][id_field]):
        raise ValueError("readback record identity differs from mutation")
    return times[2]


def _cleanup_observation(
    value: object, *, scope: dict[str, object], identity: PreviewIdentityV1,
    url_digest: str, readback_at: datetime, descriptor_at: datetime,
) -> None:
    data = _observation(value, {"cleanup_record", "revocation"}, scope, identity)
    record = PreviewCleanupRecord.from_wire(data["cleanup_record"])
    receipt = record.cleanup_receipt
    if record.identity != identity or receipt is None or receipt.status != "confirmed":
        raise ValueError("cleanup is foreign, pending or unknown")
    revocation = _object(data["revocation"], {"method", "content_url_sha256", "observed_at", "status"})
    revoked_at = timestamp(revocation["observed_at"])
    if (revocation["method"] != "GET" or type(revocation["status"]) is not int
            or revocation["status"] != 404 or _digest(revocation["content_url_sha256"]) != url_digest):
        raise ValueError("exact old preview URL revocation missing")
    if not readback_at <= receipt.requested_at <= receipt.observed_at <= revoked_at <= descriptor_at:
        raise ValueError("cleanup or revocation observations out of order")
    if any(fact.reason_code != "observed" or fact.observed_at <= readback_at
           or not receipt.requested_at <= fact.observed_at <= receipt.observed_at
           for fact in receipt.observations):
        raise ValueError("cleanup resource observation stale or contradictory")


def validate_case_browser_bundle(
    descriptor: object, *, evidence_root: Path, expected_scope: Mapping[str, object],
    validated_manifest: Mapping[str, tuple[int, str]], device: str,
) -> dict[str, object]:
    """Reopen all operator files and validate the full device wire without promotion."""
    if not isinstance(device, str) or device not in _VIEWPORTS:
        raise ValueError("unknown browser evidence device")
    if not isinstance(evidence_root, Path) or not evidence_root.is_absolute():
        raise ValueError("evidence root must be caller-selected absolute path")
    scope = _scope(dict(expected_scope))
    data = _object(descriptor, {"schema_version", "passed", "observed_at", "evidence_ref", "checks", "bundle_file"})
    _bounded_json(data)
    _version(data["schema_version"], 2)
    checks = _object(data["checks"], {"preview_rendered", "preview_interaction", "preview_revoked"})
    if data["passed"] is not True or any(value is not True for value in checks.values()):
        raise ValueError("browser evidence checks not passed")
    descriptor_at = timestamp(data["observed_at"])
    bundle_ref = _object(data["bundle_file"], {"path", "size_bytes", "sha256"})
    if data["evidence_ref"] != bundle_ref["path"]:
        raise ValueError("evidence_ref must name the exact bundle file")
    bundle = _parse_json_evidence(_read_evidence_file(evidence_root, bundle_ref, max_bytes=65536))
    _bounded_json(bundle)
    _object(bundle, {"schema_version", "scope", "device", "viewport", "preview_identity", "captured_at", "files"})
    _version(bundle["schema_version"], 2)
    _same_json(bundle["scope"], scope)
    width, height = _VIEWPORTS[device]
    viewport: dict[str, object] = {"width": width, "height": height}
    if bundle["device"] != device:
        raise ValueError("bundle device mismatch")
    _same_json(bundle["viewport"], viewport)
    identity = _bound_identity(bundle["preview_identity"], scope)
    if identity.kind != "dynamic":
        raise ValueError("static previews cannot earn full business credit")
    captured_at = timestamp(bundle["captured_at"])
    files = _object(bundle["files"], {"viewport_png", "frame_png", "render", "business", "provenance", "cleanup"})
    paths = {_text(bundle_ref["path"]).casefold()}
    payloads: dict[str, bytes] = {}
    for role, reference in files.items():
        if role == "frame_png" and reference is None:
            continue
        ref = _object(reference, {"path", "size_bytes", "sha256"})
        name = _text(ref["path"]).casefold()
        if name in paths:
            raise ValueError("evidence file roles require distinct canonical paths")
        paths.add(name)
        payloads[role] = _read_evidence_file(
            evidence_root, ref, max_bytes=_PNG_BYTES if role.endswith("_png") else _JSON_BYTES,
        )
    documents = {role: _parse_json_evidence(payloads[role]) for role in ("render", "business", "provenance", "cleanup")}
    for document in documents.values():
        _bounded_json(document)
    provenance = PreviewProvenanceV1.from_wire(documents["provenance"])
    if provenance.identity != identity:
        raise ValueError("frozen provenance preview identity mismatch")
    if snapshot_manifest_for_identity(validated_manifest, identity) != provenance.snapshot_manifest:
        raise ValueError("preview snapshot does not match the case validated manifest")
    _png_observation(payloads["viewport_png"], width=width, height=height)
    rendered_at, url_digest, controls = _render_observation(
        documents["render"], scope=scope, identity=identity, device=device,
        viewport=viewport, frame_png=payloads.get("frame_png"),
    )
    if not provenance.captured_at <= captured_at <= rendered_at:
        raise ValueError("provenance, bundle and render timestamps out of order")
    readback_at = _business_observation(
        documents["business"], scope=scope, identity=identity, controls=controls, rendered_at=rendered_at,
    )
    _cleanup_observation(
        documents["cleanup"], scope=scope, identity=identity, url_digest=url_digest,
        readback_at=readback_at, descriptor_at=descriptor_at,
    )
    return copy.deepcopy(data)
