"""Bounded Go declaration subset, not a resolved toolchain or executable recipe.

The caller supplies a complete scoped snapshot when checking source intent. An
accepted declaration cannot prove imports belong to a particular SDK, compile
source, attest zero transitive dependencies, or authorize execution/downloads.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, Self, cast

from agent_hub.capabilities.project_stack_contracts import (
    GoDeclarationReason,
    ProjectStackContractError,
    validate_project_files,
)

_SHA = re.compile(r"[a-f0-9]{64}")
_VERSION = re.compile(r"1\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?")
_TOOLCHAIN = re.compile(r"go1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_PATH = re.compile(r"[A-Za-z0-9_~+.-]+(?:/[A-Za-z0-9_~+.-]+)*")
_LEX = re.compile(
    r'(?P<comment>//[^\r\n]*|/\*[\s\S]*?\*/)'
    r'|(?P<string>"(?:[^"\\\r\n]|\\[^\r\n])*"|`[^`]*`)'
    r"|(?P<rune>'(?:[^'\\\r\n]|\\[^\r\n])*')"
    r'|(?P<word>[A-Za-z_][A-Za-z0-9_]*)'
    r'|(?P<space>[ \t\f\v]+)|(?P<newline>\r\n|[\r\n])|(?P<other>.)'
)
_NATIVE_SUFFIXES = (
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".s", ".syso", ".m",
    ".f", ".for", ".f90", ".swig", ".swigcxx",
)


class GoProjectRecipeError(ValueError):
    def __init__(self, code: GoDeclarationReason) -> None:
        self.code = code
        super().__init__(code)


def _need(value: object, code: GoDeclarationReason = "go_declaration_invalid") -> None:
    if not value:
        raise GoProjectRecipeError(code)


def _module_path(value: object) -> str:
    _need(type(value) is str)
    path = cast(str, value)
    _need(0 < len(path) <= 512 and _PATH.fullmatch(path) is not None
          and all(part not in {".", ".."} and not part.startswith(".")
                  and not part.endswith(".") for part in path.split("/")))
    return path


def _go_string(token: str) -> str:
    if token.startswith("`"):
        return token[1:-1].replace("\r", "")
    _need(token.startswith('"') and token.endswith('"'))
    result: list[str] = []
    index = 1
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
               "t": "\t", "v": "\v", "\\": "\\", '"': '"'}
    while index < len(token) - 1:
        char = token[index]
        index += 1
        if char != "\\":
            result.append(char)
            continue
        _need(index < len(token) - 1)
        char = token[index]
        index += 1
        if char in escapes:
            result.append(escapes[char])
            continue
        if char in "01234567":
            digits, count, base = char, 2, 8
        else:
            _need(char in {"x", "u", "U"})
            digits, count, base = "", {"x": 2, "u": 4, "U": 8}[char], 16
        digits += token[index:index + count]
        _need(len(digits) == count + (1 if base == 8 else 0)
              and re.fullmatch(r"[0-7]+" if base == 8 else r"[0-9a-fA-F]+", digits))
        number = int(digits, base)
        _need(number <= (255 if base == 8 or char == "x" else 0x10FFFF)
              and not 0xD800 <= number <= 0xDFFF)
        result.append(chr(number))
        index += count
    return "".join(result)


def _text(data: bytes) -> str:
    try:
        result = data.decode("utf-8-sig")
    except UnicodeError:
        raise GoProjectRecipeError("go_declaration_invalid") from None
    _need("\x00" not in result)
    return result


def _lex(text: str) -> Iterator[tuple[str, str, int]]:
    index = 0
    while index < len(text):
        if text.startswith("/*", index):
            _need(text.find("*/", index + 2) != -1)
        match = _LEX.match(text, index)
        if match is None:
            raise GoProjectRecipeError("go_declaration_invalid")
        kind, raw = match.lastgroup or "other", match.group()
        _need(not (kind == "other" and raw in {'"', "'", "`"}))
        yield kind, raw, index
        index = match.end()


def _manifest(data: bytes) -> dict[str, str]:
    text = _text(data)
    pieces: list[str] = []
    for kind, raw, start in _lex(text):
        if kind == "comment":
            _need(raw.startswith('//') and (start == 0 or text[start - 1].isspace()))
            pieces.append("".join("\n" if char in "\r\n" else " " for char in raw))
        else:
            pieces.append(raw)
    values: dict[str, str] = {}
    for line in "".join(pieces).splitlines():
        tokens = line.split()
        if not tokens:
            continue
        directive = tokens[0]
        if directive in {"require", "exclude"}:
            raise GoProjectRecipeError("go_external_modules_unsupported")
        if directive == "replace":
            raise GoProjectRecipeError("go_replace_unsupported")
        if directive in {"godebug", "tool", "retract", "use"}:
            raise GoProjectRecipeError("go_directive_unsupported")
        _need(directive in {"module", "go", "toolchain"}
              and directive not in values and len(tokens) == 2)
        value = tokens[1]
        if directive == "module":
            if value.startswith('"'):
                _need(_LEX.fullmatch(value) is not None)
                value = _go_string(value)
            value = _module_path(value)
        elif directive == "go":
            _need(_VERSION.fullmatch(value) is not None)
        else:
            _need(value == "default" or _TOOLCHAIN.fullmatch(value) is not None,
                  "go_toolchain_unsupported")
        values[directive] = value
    _need({"module", "go"} <= values.keys())
    return values


def _check_import(path: str, module: str) -> None:
    if path in {"C", "runtime/cgo"}:
        raise GoProjectRecipeError("go_cgo_unsupported")
    own = path == module or path.startswith(module + "/")
    safe = (_PATH.fullmatch(path) is not None
            and all(part not in {".", ".."} for part in path.split("/")))
    _need(safe and (own or "." not in path.split("/", 1)[0]),
          "go_external_modules_unsupported")


def _source_intent(data: bytes, module: str) -> None:
    text = _text(data)
    tokens: list[tuple[str, str]] = []
    for kind, raw, _ in _lex(text):
        if kind == "comment":
            _need(not re.search(r"#\s*cgo\b|(?://go:build|//\s*\+build)\s+[^\r\n]*\bcgo\b", raw),
                  "go_cgo_unsupported")
        elif kind not in {"space", "newline"}:
            tokens.append((kind, raw))
    index = 0
    while index < len(tokens):
        if tokens[index] != ("word", "import"):
            index += 1
            continue
        index += 1
        _need(index < len(tokens))
        block = tokens[index][1] == "("
        if block:
            index += 1
        while index < len(tokens):
            if block and tokens[index][1] == ")":
                index += 1
                break
            if block and tokens[index][1] == ";":
                index += 1
                continue
            if tokens[index][0] == "word" or tokens[index][1] == ".":
                index += 1
            _need(index < len(tokens) and tokens[index][0] == "string")
            _check_import(_go_string(tokens[index][1]), module)
            index += 1
            if not block:
                break
        else:
            _need(not block)


@dataclass(frozen=True, slots=True)
class GoProjectDeclaration:
    module_path: str
    go_version: str
    manifest_sha256: str
    sum_sha256: str | None = None
    toolchain: str | None = None
    external_modules: tuple[str, ...] = ()
    kind: Literal["go_stdlib_singlemodule_v1"] = "go_stdlib_singlemodule_v1"

    def __post_init__(self) -> None:
        _module_path(self.module_path)
        _need(type(self.go_version) is str and _VERSION.fullmatch(self.go_version) is not None)
        _need(self.toolchain is None or type(self.toolchain) is str and (
            self.toolchain == "default" or _TOOLCHAIN.fullmatch(self.toolchain) is not None
        ), "go_toolchain_unsupported")
        _need(type(self.manifest_sha256) is str and _SHA.fullmatch(self.manifest_sha256) is not None)
        _need(self.sum_sha256 is None or type(self.sum_sha256) is str
              and _SHA.fullmatch(self.sum_sha256) is not None)
        _need(type(self.external_modules) is tuple and self.external_modules == (),
              "go_external_modules_unsupported")
        _need(type(self.kind) is str and self.kind == "go_stdlib_singlemodule_v1")

    @property
    def control_paths(self) -> tuple[str, ...]:
        return ("go.mod",) if self.sum_sha256 is None else ("go.mod", "go.sum")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1, "kind": self.kind, "module_path": self.module_path,
            "go_version": self.go_version, "toolchain": self.toolchain,
            "manifest_sha256": self.manifest_sha256, "sum_sha256": self.sum_sha256,
            "external_modules": [], "dependency_policy": "stdlib_only",
            "control_paths": list(self.control_paths),
        }

    @property
    def sha256(self) -> str:
        raw = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_payload(cls, value: object) -> Self:
        _need(type(value) is dict)
        item = cast(dict[str, object], value)
        _need(set(item) == {
            "schema_version", "kind", "module_path", "go_version", "toolchain",
            "manifest_sha256", "sum_sha256", "external_modules", "dependency_policy", "control_paths",
        })
        _need(type(item["schema_version"]) is int and item["schema_version"] == 1
              and type(item["external_modules"]) is list and item["external_modules"] == []
              and type(item["control_paths"]) is list
              and item["dependency_policy"] == "stdlib_only")
        result = cls(
            module_path=cast(str, item["module_path"]), go_version=cast(str, item["go_version"]),
            toolchain=cast(str | None, item["toolchain"]),
            manifest_sha256=cast(str, item["manifest_sha256"]),
            sum_sha256=cast(str | None, item["sum_sha256"]),
            kind=cast(Literal["go_stdlib_singlemodule_v1"], item["kind"]),
        )
        _need(result.to_payload() == item)
        return result


def parse_go_project_declaration(
    files: Mapping[str, bytes], *, max_control_bytes: int = 65_536,
    max_source_bytes: int = 1024 * 1024, max_files: int = 4096,
) -> GoProjectDeclaration:
    """Parse supplied bytes only. Pinned toolchain text is a constraint, never selection.

    Any future executor must independently resolve trusted catalog inputs and
    force GOTOOLCHAIN=local, offline resolution and CGO_ENABLED=0. Source scanning
    rejects visible unsupported intent; it is not a compiler or SDK import index.
    """
    _need(all(type(limit) is int and limit > 0
              for limit in (max_control_bytes, max_source_bytes, max_files)), "go_declaration_limit")
    try:
        snapshot = validate_project_files(
            files, max_files=max_files, max_total_bytes=max_control_bytes + max_source_bytes,
        )
    except ProjectStackContractError as error:
        code: GoDeclarationReason = (
            "go_declaration_limit" if error.code == "project_stack_input_limit"
            else "go_declaration_invalid"
        )
        raise GoProjectRecipeError(code) from None
    control_size = 0
    source_size = 0
    for path, data in snapshot.items():
        name = path.rsplit("/", 1)[-1]
        if name.casefold() in {"go.mod", "go.sum", "go.work", "go.work.sum"}:
            control_size += len(data)
        elif name.endswith(".go"):
            source_size += len(data)
    _need(control_size <= max_control_bytes and source_size <= max_source_bytes,
          "go_declaration_limit")
    for path, data in snapshot.items():
        name = path.rsplit("/", 1)[-1]
        folded = name.casefold()
        _need(folded not in {"go.work", "go.work.sum"}, "go_workspace_unsupported")
        if folded in {"go.mod", "go.sum"}:
            _need(name == folded)
            _need(path == name, "go_multiple_modules_unsupported")
        _need("vendor" not in path.casefold().split("/"), "go_external_modules_unsupported")
        _need(not folded.endswith(_NATIVE_SUFFIXES), "go_native_source_unsupported")
    _need("go.mod" in snapshot, "go_manifest_missing")
    manifest = snapshot["go.mod"]
    _need(bool(manifest))
    values = _manifest(manifest)
    lock = snapshot.get("go.sum")
    _need(lock is None or not lock.strip(), "go_external_modules_unsupported")
    for path, data in snapshot.items():
        if path.endswith(".go"):
            _source_intent(data, values["module"])
    return GoProjectDeclaration(
        module_path=values["module"], go_version=values["go"], toolchain=values.get("toolchain"),
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        sum_sha256=hashlib.sha256(lock).hexdigest() if lock is not None else None,
    )
