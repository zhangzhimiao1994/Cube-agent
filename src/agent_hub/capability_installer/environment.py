from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from agent_hub.capability_installer.python_attestation import (
    MANIFEST_NAME,
    MAX_CONTROL_BYTES,
    PythonAttestationError,
    PythonAttestationLimitError,
    ScanLimits,
    bounded_tree_size,
    canonical_json,
    read_control,
    scan_tree,
    verify_tree,
)
from agent_hub.capability_installer.python_relocation import relocate_python_environment

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,255}$")
_HASH = re.compile(r"^--hash=sha256:[0-9a-fA-F]{64}$")
_PINNED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9._+!-]*$"
)


class EnvironmentBuildError(RuntimeError):
    """A trusted environment could not be built or validated."""


class EnvironmentQuotaExceeded(EnvironmentBuildError):
    """The configured environment storage quota would be exceeded."""


class EnvironmentInUseError(EnvironmentBuildError):
    """A referenced or active environment cannot be removed."""


def tenant_environment_id(tenant_id: UUID, capability_id: str) -> str:
    if type(tenant_id) is not UUID:
        raise ValueError("environment tenant must be an identifier")
    capability_id = _safe_component(capability_id, "capability id")
    digest = hashlib.sha256((tenant_id.hex + ":" + capability_id).encode("utf-8")).hexdigest()
    return "tenant-" + digest


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class PythonEnvironmentSpec:
    capability_id: str
    version: str
    lock_file: Path
    wheel_cache: Path
    smoke_module: str
    executable_name: str | None = None


@dataclass(frozen=True, slots=True)
class CliArtifactSpec:
    capability_id: str
    version: str
    artifact: Path
    sha256: str
    executable_name: str


@dataclass(frozen=True, slots=True)
class EnvironmentRecord:
    capability_id: str
    version: str
    kind: str
    path: Path
    executable: str
    content_sha256: str
    attestation_sha256: str | None = None


class CapabilityEnvironmentManager:
    """Build and retain versioned capability environments from local trusted inputs only."""

    def __init__(
        self,
        root: Path,
        *,
        quota_bytes: int = 5 * 1024 * 1024 * 1024,
        runner: CommandRunner | None = None,
        cli_probe: Callable[[Path, Path], None] | None = None,
        python_executable: str = sys.executable,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        if quota_bytes <= 0:
            raise ValueError("quota_bytes must be positive")
        self._root = root.resolve()
        self._quota_bytes = quota_bytes
        self._runner = cast(CommandRunner, subprocess.run) if runner is None else runner
        self._cli_probe = cli_probe
        self._python_executable = python_executable
        self._lock_timeout_seconds = lock_timeout_seconds
        self._staging.mkdir(parents=True, exist_ok=True)
        self._locks.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def _staging(self) -> Path:
        return self._root / ".staging"

    @property
    def _locks(self) -> Path:
        return self._root / ".locks"

    def build_python(self, spec: PythonEnvironmentSpec) -> EnvironmentRecord:
        capability_id, version = self._validate_identity(spec.capability_id, spec.version)
        lock_file = spec.lock_file.resolve(strict=True)
        wheel_cache = spec.wheel_cache.resolve(strict=True)
        if not wheel_cache.is_dir():
            raise EnvironmentBuildError("wheel cache must be a local directory")
        if not _SAFE_MODULE.fullmatch(spec.smoke_module):
            raise EnvironmentBuildError("invalid Python smoke module")
        lock_digest = _validate_hashed_lock(lock_file)
        metadata = {
            "capability_id": capability_id,
            "version": version,
            "kind": "python",
            "executable": _venv_executable_relative(spec.executable_name),
            "content_sha256": lock_digest,
        }
        with self._build_lock(capability_id):
            existing = self._matching_existing(capability_id, version, metadata)
            if existing is not None:
                self._activate(existing)
                return existing
            staging = self._new_staging(capability_id, version)
            try:
                self._run(
                    [
                        str(Path(self._python_executable).resolve(strict=True)),
                        "-m",
                        "venv",
                        str(staging),
                    ],
                    cwd=staging,
                    env=_isolated_environment(staging),
                )
                python = staging / _venv_python_relative()
                self._run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--no-index",
                        "--require-hashes",
                        "--no-deps",
                        "--only-binary=:all:",
                        "--find-links",
                        str(wheel_cache),
                        "-r",
                        str(lock_file),
                    ],
                    cwd=staging,
                    env=_isolated_environment(staging),
                )
                executable = staging / metadata["executable"]
                if not executable.is_file():
                    raise EnvironmentBuildError("Python environment executable is missing")
                smoke = _module_presence_probe(staging, spec.smoke_module)
                self._run(
                    [str(python), "-I", "-S", "-c", smoke],
                    cwd=staging,
                    env=_isolated_environment(staging),
                    smoke=True,
                )
                return self._publish(staging, metadata)
            except Exception:
                _remove_tree(staging)
                raise

    def deploy_cli(self, spec: CliArtifactSpec) -> EnvironmentRecord:
        capability_id, version = self._validate_identity(spec.capability_id, spec.version)
        artifact = spec.artifact.resolve(strict=True)
        if not artifact.is_file():
            raise EnvironmentBuildError("CLI artifact must be a local file")
        executable_name = _safe_component(spec.executable_name, "executable name")
        expected = spec.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise EnvironmentBuildError("CLI SHA256 must contain 64 hexadecimal characters")
        actual = _sha256_file(artifact)
        if actual != expected:
            raise EnvironmentBuildError("CLI artifact SHA256 mismatch")
        metadata = {
            "capability_id": capability_id,
            "version": version,
            "kind": "cli",
            "executable": executable_name,
            "content_sha256": actual,
        }
        with self._build_lock(capability_id):
            existing = self._matching_existing(capability_id, version, metadata)
            if existing is not None:
                self._activate(existing)
                return existing
            staging = self._new_staging(capability_id, version)
            try:
                target = staging / executable_name
                shutil.copyfile(artifact, target)
                if os.name != "nt":
                    target.chmod(target.stat().st_mode | stat.S_IXUSR)
                if _sha256_file(target) != expected:
                    raise EnvironmentBuildError("deployed CLI SHA256 mismatch")
                if self._cli_probe is None:
                    self._run(
                        [str(target), "--version"],
                        cwd=staging,
                        env=_isolated_environment(staging),
                        smoke=True,
                    )
                else:
                    try:
                        self._cli_probe(target, staging)
                    except Exception as error:
                        raise EnvironmentBuildError("CLI namespace smoke test failed") from error
                return self._publish(staging, metadata)
            except Exception:
                _remove_tree(staging)
                raise

    def active(self, capability_id: str) -> EnvironmentRecord:
        capability_id = _safe_component(capability_id, "capability id")
        state = self._read_state(capability_id)
        version = state.get("active_version")
        if not isinstance(version, str):
            raise KeyError("capability has no active environment")
        return self._record(capability_id, version)

    def rollback(self, capability_id: str) -> EnvironmentRecord:
        capability_id = _safe_component(capability_id, "capability id")
        with self._build_lock(capability_id):
            state = self._read_state(capability_id)
            previous = state.get("previous_version")
            active = state.get("active_version")
            if not isinstance(previous, str) or not isinstance(active, str):
                raise EnvironmentBuildError("no previous environment version is available")
            record = self._record(capability_id, previous)
            self._write_state(
                capability_id,
                {**state, "active_version": previous, "previous_version": active},
            )
            return record

    def restore_active(self, capability_id: str, version: str | None) -> EnvironmentRecord | None:
        capability_id = _safe_component(capability_id, "capability id")
        with self._build_lock(capability_id):
            state = self._read_state(capability_id)
            if version is None:
                cleared_state = dict(state)
                cleared_state.pop("active_version", None)
                cleared_state.pop("previous_version", None)
                self._write_state(capability_id, cleared_state)
                return None
            version = _safe_component(version, "version")
            record = self._record(capability_id, version)
            current = state.get("active_version")
            restored_state: dict[str, Any] = {**state, "active_version": version}
            if isinstance(current, str) and current != version:
                restored_state["previous_version"] = current
            self._write_state(capability_id, restored_state)
            return record

    def list_versions(self, capability_id: str) -> tuple[str, ...]:
        capability_id = _safe_component(capability_id, "capability id")
        versions = self._versions(capability_id)
        if not versions.exists():
            return ()
        return tuple(sorted(path.name for path in versions.iterdir() if path.is_dir()))

    def add_reference(self, capability_id: str, version: str, owner: str) -> None:
        self._change_reference(capability_id, version, owner, add=True)

    def remove_reference(self, capability_id: str, version: str, owner: str) -> None:
        self._change_reference(capability_id, version, owner, add=False)

    def references(self, capability_id: str, version: str) -> tuple[str, ...]:
        capability_id, version = self._validate_identity(capability_id, version)
        state = self._read_state(capability_id)
        references = state.get("references", {})
        if not isinstance(references, dict):
            return ()
        owners = references.get(version, [])
        return tuple(sorted(item for item in owners if isinstance(item, str)))

    def cleanup(self, capability_id: str, *, keep_versions: int = 2) -> tuple[str, ...]:
        if keep_versions < 1:
            raise ValueError("keep_versions must be at least one")
        capability_id = _safe_component(capability_id, "capability id")
        with self._build_lock(capability_id):
            state = self._read_state(capability_id)
            active = state.get("active_version")
            versions = list(self.list_versions(capability_id))
            retained = set(versions[-keep_versions:])
            if isinstance(active, str):
                retained.add(active)
            references = state.get("references", {})
            if isinstance(references, dict):
                retained.update(version for version, owners in references.items() if owners)
            removed: list[str] = []
            for version in versions:
                if version not in retained:
                    _remove_tree(self._version(capability_id, version))
                    removed.append(version)
            return tuple(removed)

    def remove_version(self, capability_id: str, version: str) -> None:
        capability_id, version = self._validate_identity(capability_id, version)
        with self._build_lock(capability_id):
            state = self._read_state(capability_id)
            if state.get("active_version") == version:
                raise EnvironmentInUseError("active environment cannot be removed")
            if self.references(capability_id, version):
                raise EnvironmentInUseError("referenced environment cannot be removed")
            path = self._version(capability_id, version)
            if not path.is_dir():
                raise KeyError("environment version not found")
            _remove_tree(path)

    def _change_reference(self, capability_id: str, version: str, owner: str, *, add: bool) -> None:
        capability_id, version = self._validate_identity(capability_id, version)
        if not owner or len(owner) > 256:
            raise ValueError("reference owner must be non-empty and at most 256 characters")
        with self._build_lock(capability_id):
            self._record(capability_id, version)
            state = self._read_state(capability_id)
            raw_references = state.get("references", {})
            references: dict[str, list[str]] = (
                {
                    key: [item for item in value if isinstance(item, str)]
                    for key, value in raw_references.items()
                    if isinstance(key, str) and isinstance(value, list)
                }
                if isinstance(raw_references, dict)
                else {}
            )
            owners = set(references.get(version, []))
            if add:
                owners.add(owner)
            else:
                owners.discard(owner)
            if owners:
                references[version] = sorted(owners)
            else:
                references.pop(version, None)
            self._write_state(capability_id, {**state, "references": references})

    def _publish(self, staging: Path, metadata: Mapping[str, str]) -> EnvironmentRecord:
        capability_id = metadata["capability_id"]
        version = metadata["version"]
        final = self._version(capability_id, version)
        published_metadata: dict[str, Any] = dict(metadata)
        pending_anchor: dict[str, Any] | None = None
        pending_state: dict[str, Any] | None = None
        state_growth = 0
        if metadata["kind"] == "python":
            try:
                bounded_tree_size(staging, limits=ScanLimits(self._quota_bytes))
                relocate_python_environment(staging, final)
                manifest = scan_tree(
                    staging,
                    interpreter=Path(self._python_executable).resolve(strict=True),
                    limits=ScanLimits(self._quota_bytes),
                )
                manifest_bytes = canonical_json(manifest)
                if len(manifest_bytes) > min(MAX_CONTROL_BYTES, self._quota_bytes):
                    raise PythonAttestationLimitError("Python attestation manifest quota exceeded")
                _write_json_atomic(staging / MANIFEST_NAME, manifest, ascii_only=True)
                published_metadata["python_attestation"] = {
                    "schema_version": 1,
                    "manifest": MANIFEST_NAME,
                    "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                }
            except PythonAttestationLimitError as error:
                raise EnvironmentQuotaExceeded("Python attestation quota exceeded") from error
            except PythonAttestationError as error:
                raise EnvironmentBuildError("Python tree attestation failed") from error
            except (OSError, ValueError) as error:
                raise EnvironmentBuildError(
                    "Python environment entry point relocation failed"
                ) from error
        _write_json_atomic(
            staging / "environment.json",
            published_metadata,
            ascii_only=metadata["kind"] == "python",
        )
        if metadata["kind"] == "python":
            try:
                _, metadata_receipt = read_control(staging / "environment.json", max_bytes=65536)
                _, manifest_receipt = read_control(staging / MANIFEST_NAME)
                if (
                    metadata_receipt["sha256"]
                    != hashlib.sha256(canonical_json(published_metadata)).hexdigest()
                    or manifest_receipt["sha256"]
                    != published_metadata["python_attestation"]["sha256"]
                ):
                    raise PythonAttestationError("Python attestation changed before publication")
                pending_anchor = {"metadata": metadata_receipt, "manifest": manifest_receipt}
                state = self._read_state(capability_id)
                anchors = state.get("python_attestations", {})
                if not isinstance(anchors, dict):
                    raise EnvironmentBuildError("Python attestation state is invalid")
                pending_state = {
                    **state,
                    "python_attestations": {**anchors, version: pending_anchor},
                }
                activated_state = {**pending_state, "schema_version": 1, "active_version": version}
                current = state.get("active_version")
                if isinstance(current, str) and current != version:
                    activated_state["previous_version"] = current
                activated_size = len(canonical_json(activated_state))
                if activated_size > 1024 * 1024:
                    raise EnvironmentQuotaExceeded(
                        "Python attestation state reference quota exceeded"
                    )
                state_path = self._root / capability_id / "state.json"
                old_size = (
                    read_control(state_path, max_bytes=1024 * 1024)[1]["identity"][4]
                    if (state_path.exists())
                    else 0
                )
                # Reserve both the receipt reference and subsequent activation write.
                state_growth = max(0, activated_size - old_size)
            except PythonAttestationError as error:
                raise EnvironmentBuildError(
                    "Python attestation publication identity changed"
                ) from error
        try:
            used_bytes = (
                bounded_tree_size(self._root, limits=ScanLimits(self._quota_bytes))
                if metadata["kind"] == "python"
                else _tree_size(self._root)
            )
        except PythonAttestationLimitError as error:
            raise EnvironmentQuotaExceeded("Python attestation storage quota exceeded") from error
        except PythonAttestationError as error:
            raise EnvironmentBuildError("Python attestation storage identity changed") from error
        if used_bytes + state_growth > self._quota_bytes:
            _remove_tree(staging)
            raise EnvironmentQuotaExceeded("capability environment disk quota exceeded")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise EnvironmentBuildError("environment version already exists")
        staging.replace(final)
        if metadata["kind"] == "python":
            try:
                _, metadata_receipt = read_control(final / "environment.json", max_bytes=65536)
                _, manifest_receipt = read_control(final / MANIFEST_NAME)
                if pending_anchor != {"metadata": metadata_receipt, "manifest": manifest_receipt}:
                    raise PythonAttestationError("Python attestation replaced during publication")
                if pending_state is None:
                    raise PythonAttestationError("Python attestation publication state is missing")
                self._write_state(capability_id, pending_state)
            except PythonAttestationError as error:
                raise EnvironmentBuildError(
                    "Python attestation publication identity changed"
                ) from error
        record = self._record(capability_id, version)
        self._activate(record)
        return record

    def _matching_existing(
        self, capability_id: str, version: str, metadata: Mapping[str, str]
    ) -> EnvironmentRecord | None:
        path = self._version(capability_id, version)
        if not path.exists():
            return None
        record = self._record(capability_id, version)
        if any(
            getattr(record, key) != value for key, value in metadata.items() if key != "executable"
        ):
            raise EnvironmentBuildError(
                "immutable environment version already exists with other content"
            )
        if record.executable != metadata["executable"]:
            raise EnvironmentBuildError("immutable environment version has another executable")
        executable = record.path / record.executable
        if not executable.is_file():
            raise EnvironmentBuildError("immutable environment executable is missing")
        if record.kind == "cli" and _sha256_file(executable) != record.content_sha256:
            raise EnvironmentBuildError("immutable CLI environment content SHA256 changed")
        return record

    def _activate(self, record: EnvironmentRecord) -> None:
        state = self._read_state(record.capability_id)
        current = state.get("active_version")
        next_state: dict[str, Any] = {
            **state,
            "schema_version": 1,
            "active_version": record.version,
        }
        if isinstance(current, str) and current != record.version:
            next_state["previous_version"] = current
        self._write_state(record.capability_id, next_state)

    def _record(self, capability_id: str, version: str) -> EnvironmentRecord:
        try:
            capability_id, version = self._validate_identity(capability_id, version)
        except ValueError:
            raise EnvironmentBuildError("environment record identity is invalid") from None
        path = self._version(capability_id, version)
        try:
            for directory in (self._root, path.parent.parent, path.parent, path):
                if directory.resolve(strict=True) != directory or not stat.S_ISDIR(
                    directory.lstat().st_mode
                ):
                    raise EnvironmentBuildError("environment directory identity changed")
            data, metadata_receipt = read_control(path / "environment.json", max_bytes=65536)
            payload = json.loads(data)
        except (OSError, ValueError) as exc:
            raise EnvironmentBuildError("environment metadata is missing or invalid") from exc
        fields = ("capability_id", "version", "kind", "executable", "content_sha256")
        if (
            not isinstance(payload, dict)
            or any(type(payload.get(key)) is not str for key in fields)
            or payload["capability_id"] != capability_id
            or payload["version"] != version
            or payload["kind"] not in ("cli", "python")
            or re.fullmatch(r"[0-9a-f]{64}", payload["content_sha256"]) is None
        ):
            raise EnvironmentBuildError("environment metadata identity is invalid")
        executable: str = payload["executable"]
        attestation_sha256: str | None = None
        anchors = self._read_state(capability_id).get("python_attestations", {})
        anchor = anchors.get(version) if isinstance(anchors, dict) else None
        if payload["kind"] == "python" or anchor is not None:
            if not isinstance(anchor, dict) or "python_attestation" not in payload:
                raise EnvironmentBuildError(
                    "Python record lacks complete tree attestation; rebuild required"
                )
            if payload["kind"] != "python":
                raise EnvironmentBuildError("Python attestation record kind changed")
            try:
                attestation_sha256 = verify_tree(
                    path,
                    metadata=payload,
                    metadata_receipt=metadata_receipt,
                    anchor=anchor,
                    limits=ScanLimits(self._quota_bytes),
                )
            except PythonAttestationError as error:
                raise EnvironmentBuildError(
                    "installed Python tree attestation is invalid"
                ) from error
        try:
            if payload["kind"] == "cli":
                _safe_component(executable, "stored executable")
            else:
                parts = executable.split("/")
                if len(parts) != 2 or parts[0] not in ("bin", "Scripts"):
                    raise ValueError("invalid Python executable path")
                name = parts[1]
                if parts[0] == "Scripts" and name.lower().endswith(".exe"):
                    name = name[:-4]
                _safe_component(name, "stored executable")
        except ValueError:
            raise EnvironmentBuildError("environment executable path is invalid") from None
        try:
            target = path / executable
            if payload["kind"] == "cli":
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise EnvironmentBuildError("immutable CLI executable identity changed")
                if _sha256_file(target) != payload["content_sha256"]:
                    raise EnvironmentBuildError("immutable CLI environment content SHA256 changed")
            elif target.parent.resolve(strict=True) != target.parent or not target.is_file():
                raise EnvironmentBuildError("immutable Python executable is missing or invalid")
            return EnvironmentRecord(
                capability_id=capability_id,
                version=version,
                kind=payload["kind"],
                path=path,
                executable=executable,
                content_sha256=payload["content_sha256"],
                attestation_sha256=attestation_sha256,
            )
        except OSError as exc:
            raise EnvironmentBuildError(
                "immutable environment executable is missing or invalid"
            ) from exc

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        smoke: bool = False,
    ) -> None:
        try:
            self._runner(
                argv,
                cwd=cwd,
                env=dict(env),
                shell=False,
                check=True,
                capture_output=True,
                text=True,
                timeout=30 if smoke else 300,
            )
        except Exception as exc:
            label = "smoke test" if smoke else "environment build command"
            raise EnvironmentBuildError(f"{label} failed") from exc

    def _new_staging(self, capability_id: str, version: str) -> Path:
        path = self._staging / uuid4().hex
        path.mkdir(parents=False, exist_ok=False)
        return path

    def _versions(self, capability_id: str) -> Path:
        return self._root / capability_id / "versions"

    def _version(self, capability_id: str, version: str) -> Path:
        return self._versions(capability_id) / version

    def _read_state(self, capability_id: str) -> dict[str, Any]:
        path = self._root / capability_id / "state.json"
        if not path.exists():
            return {"schema_version": 1, "references": {}}
        try:
            data, _ = read_control(path, max_bytes=1024 * 1024)
            payload = json.loads(data)
        except (OSError, ValueError) as exc:
            raise EnvironmentBuildError("capability environment state is invalid") from exc
        if not isinstance(payload, dict):
            raise EnvironmentBuildError("capability environment state is invalid")
        return payload

    def _write_state(self, capability_id: str, payload: Mapping[str, Any]) -> None:
        _write_json_atomic(self._root / capability_id / "state.json", payload)

    def _validate_identity(self, capability_id: str, version: str) -> tuple[str, str]:
        return (
            _safe_component(capability_id, "capability id"),
            _safe_component(version, "version"),
        )

    @contextmanager
    def _build_lock(self, capability_id: str) -> Iterator[None]:
        lock = self._locks / f"{capability_id}.lock"
        deadline = time.monotonic() + self._lock_timeout_seconds
        while True:
            try:
                lock.mkdir()
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise EnvironmentBuildError(
                        "timed out waiting for capability build lock"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock.rmdir()
            except FileNotFoundError:
                pass


def _validate_hashed_lock(path: Path) -> str:
    if not path.is_file():
        raise EnvironmentBuildError("Python lock file must be a local file")
    content = path.read_text(encoding="utf-8")
    if "://" in content or re.search(
        r"(^|\s)(?:--index-url|--extra-index-url|-e|--editable|@)(?:\s|$)", content
    ):
        raise EnvironmentBuildError("remote or editable requirements are forbidden")
    logical = content.replace("\\\n", " ").splitlines()
    requirements = [
        line.strip() for line in logical if line.strip() and not line.lstrip().startswith("#")
    ]
    if not requirements:
        raise EnvironmentBuildError("Python lock file is empty")
    for requirement in requirements:
        tokens = requirement.split()
        package = tokens[0]
        if _PINNED_REQUIREMENT.fullmatch(package) is None:
            raise EnvironmentBuildError("Python requirements must use fixed versions")
        if len(tokens) < 2 or not all(_HASH.fullmatch(token) for token in tokens[1:]):
            if any(
                token.startswith("--") and not token.startswith("--hash=") for token in tokens[1:]
            ):
                raise EnvironmentBuildError("extra pip directives are forbidden in lock files")
            raise EnvironmentBuildError("every Python requirement must include a SHA256 hash")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_component(value: str, label: str) -> str:
    if not _SAFE_NAME.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return value


def _module_presence_probe(staging: Path, module: str) -> str:
    # -S avoids installed .pth code; PathFinder never imports a parent package.
    return (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from importlib.machinery import PathFinder\n"
        f"root = Path({str(staging)!r})\n"
        "site = (root / 'Lib' / 'site-packages' if os.name == 'nt' else "
        "root / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages')\n"
        "paths = [str(site), *sys.path]\n"
        f"for part in {module!r}.split('.'):\n"
        "    spec = PathFinder.find_spec(part, paths)\n"
        "    if spec is None:\n"
        "        raise SystemExit(1)\n"
        "    paths = list(spec.submodule_search_locations or ())\n"
        "raise SystemExit(0)\n"
    )


def _venv_python_relative() -> str:
    return "Scripts/python.exe" if os.name == "nt" else "bin/python"


def _venv_executable_relative(executable_name: str | None) -> str:
    if executable_name is None:
        return _venv_python_relative()
    executable_name = _safe_component(executable_name, "executable name")
    if os.name == "nt" and not executable_name.lower().endswith(".exe"):
        executable_name = f"{executable_name}.exe"
    directory = "Scripts" if os.name == "nt" else "bin"
    return f"{directory}/{executable_name}"


def _isolated_environment(staging: Path) -> dict[str, str]:
    home = staging / ".home"
    temporary = staging / ".tmp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "TMPDIR": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    }
    for key in ("SystemRoot", "WINDIR"):
        if value := os.environ.get(key):
            environment[key] = value
    return environment


def _write_json_atomic(
    path: Path,
    payload: Mapping[str, Any],
    *,
    ascii_only: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=ascii_only, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


__all__ = [
    "CapabilityEnvironmentManager",
    "CliArtifactSpec",
    "EnvironmentBuildError",
    "EnvironmentInUseError",
    "EnvironmentQuotaExceeded",
    "EnvironmentRecord",
    "PythonEnvironmentSpec",
]
