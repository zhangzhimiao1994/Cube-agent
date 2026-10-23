"""Strict inert decoding of the trusted AST helper's bounded inventory.

Hashes and sizes describe raw active Go file bytes, without executable-mode tags.
The runner must authenticate the helper and compare against its sealed snapshot.
Inventory is neither compilation evidence nor an SDK/stdlib attestation.
The initial recipe requires at least one top-level Test per active package;
ordinary untested helper packages are currently rejected explicitly.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.go_project_evidence import GoTestExpectation

_SHA = re.compile(r"[a-f0-9]{64}")
_PATH = re.compile(r"[A-Za-z0-9_~+.-]+(?:/[A-Za-z0-9_~+.-]+)*")
_FIELDS = {"schema_version", "kind", "module_path", "packages", "expected_tests",
           "project_main_package", "source_go_files"}


class GoInventoryError(ValueError):
    """Malformed, oversized or unbound inventory; contains no source or host path."""


def _need(value: object) -> None:
    if not value:
        raise GoInventoryError("go_inventory_invalid")


def _path(value: object) -> str:
    _need(type(value) is str)
    text = cast(str, value)
    _need(0 < len(text) <= 1024 and _PATH.fullmatch(text) is not None
          and all(part not in {".", ".."} and not part.startswith(".")
                  and not part.endswith(".") for part in text.split("/")))
    return text


def _test_name(value: object) -> str:
    _need(type(value) is str)
    name = cast(str, value)
    _need(name.startswith("Test") and name != "TestMain" and len(name.encode("utf-8")) <= 1024
          and (len(name) == 4 or unicodedata.category(name[4]) != "Ll")
          # Newer SDK Unicode letters may be unassigned in Python's older table.
          # The authenticated Go AST helper owns identifier syntax and isTest.
          and all(char == "_" or unicodedata.category(char).startswith("L")
                  or unicodedata.category(char) in {"Nd", "Cn"} for char in name))
    return name


@dataclass(frozen=True, slots=True)
class GoSourceFile:
    path: str
    raw_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class GoSourceInventory:
    module_path: str
    packages: tuple[str, ...]
    expected_tests: tuple[tuple[str, str], ...]
    project_main_package: str
    source_go_files: tuple[GoSourceFile, ...]

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, "kind": "go_source_inventory_v1",
                "module_path": self.module_path, "packages": list(self.packages),
                "expected_tests": [{"package": package, "name": name}
                                   for package, name in self.expected_tests],
                "project_main_package": self.project_main_package,
                "source_go_files": [{"path": row.path, "raw_sha256": row.raw_sha256,
                                     "size_bytes": row.size_bytes} for row in self.source_go_files]}

    @classmethod
    def from_payload(cls, value: object, *, expected_module: str,
                     expected_source: Mapping[str, tuple[str, int]] | None = None,
                     max_files: int = 10000, max_packages: int = 256,
                     max_tests: int = 1000) -> GoSourceInventory:
        try:
            module = _path(expected_module)
            _need(type(max_files) is int and 1 <= max_files <= 100000)
            _need(all(type(limit) is int and limit > 0 for limit in (max_packages, max_tests)))
            _need(type(value) is dict)
            item = cast(dict[str, object], value)
            _need(set(item) == _FIELDS and type(item["schema_version"]) is int
                  and item["schema_version"] == 1 and item["kind"] == "go_source_inventory_v1"
                  and all(type(item[key]) is str for key in
                          ("kind", "module_path", "project_main_package"))
                  and item["module_path"] == module and item["project_main_package"] == module)
            _need(expected_source is None or isinstance(expected_source, Mapping))
            rows = item["packages"]
            _need(type(rows) is list and 0 < len(cast(list[object], rows)) <= max_packages)
            packages = tuple(_path(p) for p in cast(list[object], rows))
            _need(tuple(sorted(set(packages))) == packages and module in packages
                  and all(p == module or p.startswith(module + "/") for p in packages))
            tests = item["expected_tests"]
            _need(type(tests) is list and 0 < len(cast(list[object], tests)) <= max_tests)
            expected: list[tuple[str, str]] = []
            for row in cast(list[object], tests):
                _need(type(row) is dict and set(cast(dict[str, object], row)) == {"package", "name"})
                test = cast(dict[str, object], row)
                package, name = _path(test["package"]), _test_name(test["name"])
                _need(package in packages)
                expected.append((package, name))
            _need(sorted(set(expected)) == expected and {p for p, _ in expected} == set(packages))
            sources = item["source_go_files"]
            _need(type(sources) is list and 0 < len(cast(list[object], sources)) <= max_files)
            files: list[GoSourceFile] = []
            for row in cast(list[object], sources):
                _need(type(row) is dict and set(cast(dict[str, object], row))
                      == {"path", "raw_sha256", "size_bytes"})
                file = cast(dict[str, object], row)
                path = _path(file["path"])
                digest, size = file["raw_sha256"], file["size_bytes"]
                _need(path.endswith(".go") and type(digest) is str
                      and _SHA.fullmatch(digest) is not None
                      and type(size) is int and 0 < size <= 500 * 1024**2)
                directory = path.rsplit("/", 1)[0] if "/" in path else ""
                _need(module + ("/" + directory if directory else "") in packages)
                if expected_source is not None:
                    actual = expected_source.get(path)
                    _need(type(actual) is tuple and len(actual) == 2
                          and type(actual[0]) is str and type(actual[1]) is int
                          and actual == (digest, size))
                files.append(GoSourceFile(path, cast(str, digest), cast(int, size)))
            paths = [row.path for row in files]
            _need(paths == sorted(set(paths)) and len({p.lower() for p in paths}) == len(paths)
                  and sum(row.size_bytes for row in files) <= 500 * 1024**2)
            test_packages = {module + ("/" + path.rsplit("/", 1)[0] if "/" in path else "")
                             for path in paths if path.endswith("_test.go")}
            _need(test_packages == set(packages))
            return cls(module, packages, tuple(expected), module, tuple(files))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
            raise GoInventoryError("go_inventory_invalid") from None

    def to_test_expectation(self, *, source_sha256: str, protected_sha256: str,
                            max_files: int = 10000, max_packages: int = 256,
                            max_tests: int = 1000) -> GoTestExpectation:
        from agent_hub.skills.sandbox.go_project_evidence import GoPackageTests, GoTestExpectation

        _need(type(source_sha256) is str and _SHA.fullmatch(source_sha256) is not None
              and type(protected_sha256) is str and _SHA.fullmatch(protected_sha256) is not None)
        current = self.from_payload(self.to_payload(), expected_module=self.module_path,
            max_files=max_files, max_packages=max_packages, max_tests=max_tests)
        return GoTestExpectation(packages=tuple(GoPackageTests(package,
            tuple(name for p, name in current.expected_tests if p == package))
            for package in current.packages), source_sha256=source_sha256, protected_sha256=protected_sha256)


def _pairs(rows: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in rows:
        _need(key not in result)
        result[key] = value
    return result


def parse_go_source_inventory(raw: bytes, *, expected_module: str,
    expected_source: Mapping[str, tuple[str, int]] | None = None, max_files: int = 10000,
    max_output_bytes: int = 16 * 1024**2, max_packages: int = 256, max_tests: int = 1000,
) -> GoSourceInventory:
    """Expected source may include inactive files; every returned active file must match."""
    try:
        _need(type(max_output_bytes) is int and max_output_bytes > 0
              and type(raw) is bytes and 0 < len(raw) <= max_output_bytes)
        value: object = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                                   parse_constant=lambda _: _need(False))
        return GoSourceInventory.from_payload(value, expected_module=expected_module,
            expected_source=expected_source, max_files=max_files, max_packages=max_packages,
            max_tests=max_tests)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise GoInventoryError("go_inventory_invalid") from None
