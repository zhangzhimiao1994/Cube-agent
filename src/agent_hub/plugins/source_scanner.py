"""Non-executing static signals over already verified, frozen plugin bytes.

The caller owns signature verification, authority, registration and approval.
These DTOs and a complete report are not approval authority or proof of safety.
Byte limits apply before parsing; AST budgets apply after ast.parse. There is no
CPU isolation or hard parsing deadline here. Callers offloading parsing must drain
that work before releasing its resources, even after cancellation.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields, replace
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from agent_hub.plugins.handover_sources import (
    InvalidSource,
    SnapshotFile,
    SourceLimits,
    _validate_paths,
)
from agent_hub.plugins.handover_verification import VerifiedMember, VerifiedPackageFacts, _manifest
from agent_hub.runtime.contracts import (
    _MAX_JSON_BYTES,
    JsonValue,
    _canonical_json_bytes,
    _freeze_object,
)

SCANNER_ID = "plugin-python-source-v2"
RULESET_ID = "plugin-python-signals-v1"
PARSER_ID = f"{sys.implementation.name}-{sys.version.split()[0]}-ast-exec"

_RULES: Mapping[str, JsonValue] = _freeze_object(
    {
        "PY001": {"category": "dynamic_execution", "explanation": "Dynamic code execution signal."},
        "PY002": {"category": "import", "explanation": "Static or dynamic import signal."},
        "PY003": {"category": "network", "explanation": "Network API signal."},
        "PY004": {"category": "process", "explanation": "Process creation or shell API signal."},
        "PY005": {"category": "file_write", "explanation": "Possible file modification signal."},
        "PY006": {"category": "environment", "explanation": "Environment access signal."},
    },
    name="source scan rules",
)
_SOURCE_LIMITS = SourceLimits()
_NONEXECUTING_SUFFIXES = frozenset(
    {".md", ".rst", ".txt", ".json", ".toml", ".yaml", ".yml", ".csv", ".tsv"}
)
_LIMITATIONS = (
    "Static signals only; not proof of malice or absence of vulnerabilities. "
    "Complete means bounded Python AST coverage with a bound Python entrypoint contract. "
    "Nonexecuting format classification excludes the declared executable entrypoint. "
    "Alias resolution is simple and conservative, without control-flow or data-flow analysis. "
    "Dependencies, imported external code and runtime behavior are not inspected. "
    "AST budgets are not CPU isolation or a hard parser timeout."
)


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True, slots=True)
class PluginScanSubject:
    tenant_id: UUID
    run_id: UUID
    registration_id: UUID
    handover_id: str
    receipt_id: str
    snapshot_id: str
    source_grant_id: str
    plan_hash: str
    blob_sha256: str
    source_grant_binding_sha256: str
    registration_contract_sha256: str
    plan_version: int
    verified_package: VerifiedPackageFacts

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID for value in (self.tenant_id, self.run_id, self.registration_id)
        ):
            raise ValueError("invalid scan subject identifiers")
        if any(
            type(value) is not str
            or not value
            or len(value) > 255
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            for value in (self.handover_id, self.receipt_id, self.snapshot_id, self.source_grant_id)
        ):
            raise ValueError("invalid scan subject identifiers")
        if type(self.plan_version) is not int or self.plan_version < 1:
            raise ValueError("invalid scan subject plan version")
        if not all(
            _digest(value)
            for value in (
                self.plan_hash,
                self.blob_sha256,
                self.source_grant_binding_sha256,
                self.registration_contract_sha256,
            )
        ):
            raise ValueError("invalid scan subject digests")
        facts = self.verified_package
        if (
            type(facts) is not VerifiedPackageFacts
            or type(facts.members) is not tuple
            or any(type(member) is not VerifiedMember for member in facts.members)
        ):
            raise ValueError("invalid verified package facts")
        if not all(
            _digest(getattr(facts, name))
            for name in (
                "source_manifest_sha256",
                "frozen_manifest_sha256",
                "archive_sha256",
                "signature_payload_sha256",
                "public_key_sha256",
            )
        ):
            raise ValueError("invalid verified package digests")
        object.__setattr__(
            self,
            "verified_package",
            replace(facts, members=tuple(replace(member) for member in facts.members)),
        )


@dataclass(frozen=True, slots=True)
class PluginScanLimits:
    """Soft defaults, explicitly raisable by trusted callers within int64 storage."""

    max_nodes: int = 100_000
    max_depth: int = 64
    max_findings: int = 256

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 1 <= value <= 2**63 - 1
            for value in (self.max_nodes, self.max_depth, self.max_findings)
        ):
            raise ValueError("scan limits must be positive int64 integers")


@dataclass(frozen=True, slots=True)
class PluginSourceFinding:
    rule_id: str
    category: str
    path: str
    member_sha256: str
    line: int
    column: int


def _report_node_budget(findings: tuple[PluginSourceFinding, ...]) -> int:
    # Source-bounded metadata plus each actual finding's object, keys and scalar values.
    return 20_000 + len(findings) * (2 * len(fields(PluginSourceFinding)) + 3)


@dataclass(frozen=True, slots=True)
class PluginSourceScanReport:
    body: Mapping[str, JsonValue]
    report_sha256: str
    coverage: Literal["complete", "incomplete"]
    findings: tuple[PluginSourceFinding, ...]

    def __post_init__(self) -> None:
        findings = tuple(replace(finding) for finding in self.findings)
        object.__setattr__(
            self,
            "body",
            _freeze_object(
                self.body, name="plugin source scan report", max_nodes=_report_node_budget(findings)
            ),
        )
        object.__setattr__(self, "findings", findings)


def _subject_body(subject: PluginScanSubject) -> Mapping[str, JsonValue]:
    facts = subject.verified_package
    return _freeze_object(
        {
            **{
                field.name: getattr(subject, field.name)
                for field in fields(subject)
                if field.name not in {"tenant_id", "run_id", "registration_id", "verified_package"}
            },
            "tenant_id": str(subject.tenant_id),
            "run_id": str(subject.run_id),
            "registration_id": str(subject.registration_id),
            "verified_package": {
                **{
                    field.name: getattr(facts, field.name)
                    for field in fields(facts)
                    if field.name not in {"verified_at", "key_not_after", "members"}
                },
                "members": [
                    {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
                    for item in sorted(facts.members, key=lambda item: item.path)
                ],
            },
        },
        name="source scan subject",
        max_nodes=5_000,
    )


def _binding_matches(files: tuple[SnapshotFile, ...], facts: VerifiedPackageFacts) -> bool:
    expected = {member.path: member for member in facts.members}
    if len(expected) != len(facts.members) or set(expected) != {file.path for file in files}:
        return False
    for file in files:
        member = expected[file.path]
        if file.path == "plugin.json":
            if file.sha256 != facts.frozen_manifest_sha256:
                return False
        elif file.sha256 != member.sha256 or file.size_bytes != member.size_bytes:
            return False
    return True


def _execution_contract(
    files: tuple[SnapshotFile, ...],
    facts: VerifiedPackageFacts,
) -> tuple[str | None, str | None]:
    """Read execution roles from the exact frozen manifest, not its signed ZIP counterpart."""
    manifest = next((file for file in files if file.path == "plugin.json"), None)
    if manifest is None:
        return None, "manifest_missing"
    if len(manifest.content) > _SOURCE_LIMITS.max_file_bytes:
        return None, "source_byte_limit"
    if (
        manifest.size_bytes != len(manifest.content)
        or manifest.sha256 != facts.frozen_manifest_sha256
        or hashlib.sha256(manifest.content).hexdigest() != facts.frozen_manifest_sha256
    ):
        return None, "manifest_binding_mismatch"
    try:
        raw = _manifest(manifest.content)
    except (ValueError, UnicodeError, RecursionError, MemoryError):
        return None, "manifest_invalid"
    if (raw.get("id"), raw.get("version")) != (facts.plugin_id, facts.plugin_version):
        return None, "manifest_identity_mismatch"
    package = raw.get("package")
    if not isinstance(package, dict) or package.get("kind") != "adapter_package":
        return None, "manifest_invalid"
    entrypoint = package.get("entrypoint")
    if not isinstance(entrypoint, str) or not any(file.path == entrypoint for file in files):
        return None, "entrypoint_missing"
    if package.get("runtime") != "python":
        return entrypoint, "unsupported_runtime"
    if PurePosixPath(entrypoint).suffix.lower() != ".py":
        return entrypoint, "unsupported_python_entrypoint"
    return entrypoint, None


def _walk(tree: ast.AST) -> Iterator[tuple[ast.AST, int]]:
    # Iterator frames avoid recursion and do not queue every child of wide nodes.
    pending: list[tuple[Iterator[ast.AST], int]] = [(iter((tree,)), 1)]
    while pending:
        children, depth = pending[-1]
        node = next(children, None)
        if node is None:
            pending.pop()
            continue
        yield node, depth
        pending.append((ast.iter_child_nodes(node), depth + 1))


def _name(node: ast.AST, aliases: Mapping[str, str]) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Call):
        node = node.func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
    if not isinstance(node, ast.Name):
        return ""
    resolved = ".".join((aliases.get(node.id, node.id), *reversed(parts)))
    return resolved if len(resolved) <= 512 else ""


def _aliases(node: ast.AST, aliases: dict[str, str]) -> None:
    if isinstance(node, ast.Import):
        for item in node.names:
            aliases[item.asname or item.name.split(".")[0]] = (
                item.name if item.asname else item.name.split(".")[0]
            )
    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
        for item in node.names:
            aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    elif isinstance(node, ast.Assign):
        resolved = _name(node.value, aliases)
        if resolved:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = resolved


def _signals(node: ast.AST, aliases: Mapping[str, str]) -> Iterator[str]:
    if isinstance(node, ast.Import | ast.ImportFrom):
        yield "PY002"
    if (
        isinstance(node, ast.Attribute | ast.Name)
        and isinstance(node.ctx, ast.Load)
        and _name(node, aliases) in {"os.environ", "os.environb"}
    ):
        yield "PY006"
    if not isinstance(node, ast.Call):
        return
    name = _name(node.func, aliases)
    if name in {"eval", "exec", "compile", "builtins.eval", "builtins.exec", "builtins.compile"}:
        yield "PY001"
    if name in {"__import__", "builtins.__import__", "importlib.import_module", "importlib.reload"}:
        yield "PY002"
    root = name.split(".")[0]
    if root in {"socket", "requests", "httpx", "aiohttp", "urllib", "http", "ftplib", "smtplib"}:
        yield "PY003"
    if (
        root in {"subprocess", "multiprocessing"}
        or name in {"os.system", "os.popen", "pty.spawn"}
        or (name.startswith(("os.exec", "os.spawn")))
    ):
        yield "PY004"
    if name in {"os.getenv", "os.getenvb", "os.putenv", "os.unsetenv"}:
        yield "PY006"
    if name in {"open", "builtins.open", "io.open"} or name.endswith(".open"):
        mode_index = 1 if name in {"open", "builtins.open", "io.open", "os.open"} else 0
        mode: ast.AST | None = node.args[mode_index] if len(node.args) > mode_index else None
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        if (
            any(isinstance(arg, ast.Starred) for arg in node.args)
            or any(keyword.arg is None for keyword in node.keywords)
            or (
                mode is not None
                and (
                    not isinstance(mode, ast.Constant)
                    or not isinstance(mode.value, str)
                    or any(flag in mode.value for flag in "wax+")
                )
            )
        ):
            yield "PY005"
    elif name.rsplit(".", 1)[-1] in {
        "write",
        "writelines",
        "write_text",
        "write_bytes",
        "touch",
        "mkdir",
        "unlink",
        "rmdir",
        "rename",
        "replace",
        "truncate",
        "chmod",
        "symlink_to",
        "hardlink_to",
    } or name in {
        "os.remove",
        "os.makedirs",
        "shutil.copy",
        "shutil.copy2",
        "shutil.move",
        "shutil.rmtree",
    }:
        yield "PY005"


def _nonexecuting(file: SnapshotFile) -> bool:
    return PurePosixPath(file.path).suffix.lower() in _NONEXECUTING_SUFFIXES and not (
        file.content.startswith((b"#!", b"MZ", b"\x7fELF", b"\x00asm")) or b"\x00" in file.content
    )


def _finding_body(finding: PluginSourceFinding) -> Mapping[str, JsonValue]:
    rule = _RULES[finding.rule_id]
    assert isinstance(rule, Mapping)
    return {
        **{field.name: getattr(finding, field.name) for field in fields(finding)},
        "explanation": rule["explanation"],
    }


def scan_source(
    files: tuple[SnapshotFile, ...],
    subject: PluginScanSubject,
    *,
    limits: PluginScanLimits | None = None,
) -> PluginSourceScanReport:
    """Scan trusted frozen bytes, returning incomplete on any known coverage gap.

    No signature, dependency, schema, authorization or ZIP validation is repeated.
    Binding checks only prevent omitted/substituted inputs from appearing covered.
    """
    if (
        type(subject) is not PluginScanSubject
        or type(files) is not tuple
        or any(type(file) is not SnapshotFile or type(file.content) is not bytes for file in files)
    ):
        raise ValueError("invalid source scan inputs")
    subject = replace(subject)
    if limits is not None and type(limits) is not PluginScanLimits:
        raise ValueError("invalid scan limits")
    checked = PluginScanLimits() if limits is None else replace(limits)
    files = tuple(sorted((replace(file) for file in files), key=lambda file: file.path))
    reasons: set[str] = set()
    findings: list[PluginSourceFinding] = []
    members: list[dict[str, JsonValue]] = []
    selected = files[: _SOURCE_LIMITS.max_files]
    if len(selected) != len(files):
        reasons.add("source_file_limit")
    try:
        _validate_paths(tuple(file.path for file in selected), _SOURCE_LIMITS)
    except InvalidSource:
        # Invalid paths are not safe report fields; the trusted caller must repair its input.
        if files:
            raise ValueError("invalid source scan paths") from None
        reasons.add("no_members")
    if not _binding_matches(files, subject.verified_package):
        reasons.add("member_binding_mismatch")
    total = sum(len(file.content) for file in files)
    entrypoint, contract_reason = (
        (None, "source_byte_limit")
        if total > _SOURCE_LIMITS.max_total_bytes
        else _execution_contract(selected, subject.verified_package)
    )
    if contract_reason is not None:
        reasons.add(contract_reason)
    nodes = 0
    for file in selected:
        row: dict[str, JsonValue] = {
            "path": file.path,
            "member_sha256": hashlib.sha256(file.content).hexdigest(),
            "size_bytes": len(file.content),
            "status": "unsupported",
            "reason": "unsupported_format",
        }
        members.append(row)
        if (
            total > _SOURCE_LIMITS.max_total_bytes
            or len(file.content) > _SOURCE_LIMITS.max_file_bytes
        ):
            row.update(status="incomplete", reason="source_byte_limit")
        elif file.size_bytes != len(file.content) or file.sha256 != row["member_sha256"]:
            row.update(status="incomplete", reason="member_binding_mismatch")
        elif PurePosixPath(file.path).suffix.lower() == ".py":
            row.update(status="scanned", reason=None)
            if nodes >= checked.max_nodes:
                row.update(status="incomplete", reason="node_limit")
            else:
                try:
                    tree = ast.parse(file.content, mode="exec")
                except (SyntaxError, UnicodeError, ValueError, RecursionError, MemoryError):
                    row.update(status="incomplete", reason="parse_error")
                else:
                    bounded_nodes: list[ast.AST] = []
                    for node, depth in _walk(tree):
                        nodes += 1
                        if nodes > checked.max_nodes or depth > checked.max_depth:
                            row.update(
                                status="incomplete",
                                reason=(
                                    "node_limit" if nodes > checked.max_nodes else "depth_limit"
                                ),
                            )
                            break
                        bounded_nodes.append(node)
                    else:
                        # Resolve names only after the entire tree fits its budgets.
                        aliases: dict[str, str] = {}
                        for node in bounded_nodes:
                            _aliases(node, aliases)
                            for rule_id in _signals(node, aliases):
                                if len(findings) >= checked.max_findings:
                                    row.update(status="incomplete", reason="finding_limit")
                                    continue
                                rule = _RULES[rule_id]
                                assert isinstance(rule, Mapping)
                                category = rule["category"]
                                assert isinstance(category, str)
                                findings.append(
                                    PluginSourceFinding(
                                        rule_id,
                                        category,
                                        file.path,
                                        file.sha256,
                                        getattr(node, "lineno", 1),
                                        getattr(node, "col_offset", 0),
                                    )
                                )
        elif file.path == entrypoint and contract_reason is not None:
            row.update(status="unsupported", reason=contract_reason)
        elif contract_reason is not None and (entrypoint is None or file.path == "plugin.json"):
            row.update(status="incomplete", reason=contract_reason)
        elif _nonexecuting(file):
            row.update(status="nonexecuting", reason=None)
        if row["reason"] is not None:
            reasons.add(str(row["reason"]))
    ordered = tuple(sorted(findings, key=lambda f: (f.path, f.line, f.column, f.rule_id)))
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "scanner_id": SCANNER_ID,
        "ruleset_id": RULESET_ID,
        "rules_sha256": hashlib.sha256(_canonical_json_bytes(_RULES)).hexdigest(),
        "parser_id": PARSER_ID,
        "limits": {field.name: getattr(checked, field.name) for field in fields(checked)},
        "source_limits": {
            field.name: getattr(_SOURCE_LIMITS, field.name) for field in fields(_SOURCE_LIMITS)
        },
        "subject": _subject_body(subject),
        "coverage": "incomplete",
        "incomplete_reasons": tuple(sorted(reasons | {"report_byte_limit"})),
        "input_member_count": len(files),
        "members": tuple(members),
        "findings": (),
        "limitations": _LIMITATIONS,
        "report_max_bytes": _MAX_JSON_BYTES,
    }
    # Reserve the truncation metadata before spending the existing JSON contract's byte budget.
    remaining = _MAX_JSON_BYTES - len(_canonical_json_bytes(payload))
    finding_rows: list[Mapping[str, JsonValue]] = []
    for finding in ordered:
        finding_row = _finding_body(finding)
        size = len(_canonical_json_bytes(finding_row)) + bool(finding_rows)
        if size > remaining:
            reasons.add("report_byte_limit")
            break
        finding_rows.append(finding_row)
        remaining -= size
    ordered = ordered[: len(finding_rows)]
    coverage: Literal["complete", "incomplete"] = "incomplete" if reasons else "complete"
    payload.update(
        coverage=coverage, incomplete_reasons=tuple(sorted(reasons)), findings=tuple(finding_rows)
    )
    body = _freeze_object(
        payload,
        name="plugin source scan report",
        max_nodes=_report_node_budget(ordered),
    )
    return PluginSourceScanReport(
        body, hashlib.sha256(_canonical_json_bytes(body)).hexdigest(), coverage, ordered
    )
