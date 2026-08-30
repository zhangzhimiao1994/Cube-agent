"""Storage and metadata for generated runtime files."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ALLOWED_GENERATED_FILE_MIME_TYPES = frozenset({DOCX_MIME_TYPE, PPTX_MIME_TYPE})
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


@dataclass(frozen=True, slots=True)
class GeneratedFileMetadata:
    """Safe artifact metadata stored in run artifact JSON."""

    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    download_url: str

    def to_public_dict(self) -> dict[str, str | int]:
        """Return the public file metadata contract used by API/UI callers."""

        return asdict(self)

    def to_content_file(self) -> dict[str, str | int]:
        """Return metadata suitable for storing under Artifact.content['file']."""

        return self.to_public_dict()


class GeneratedFileStore:
    """Tenant/run/artifact-scoped storage for generated files."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store_bytes(
        self,
        tenant_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        filename: str,
        mime_type: str,
        data: bytes,
    ) -> GeneratedFileMetadata:
        """Persist a generated file and return safe download metadata."""

        safe_filename = safe_generated_filename(filename)
        if mime_type not in ALLOWED_GENERATED_FILE_MIME_TYPES:
            raise ValueError("unsupported generated file MIME type")

        storage_key = f"{tenant_id}/{run_id}/{artifact_id}/{safe_filename}"
        output_path = self._path_for_storage_key(storage_key)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)

        digest = sha256(data).hexdigest()
        return GeneratedFileMetadata(
            filename=safe_filename,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=digest,
            storage_key=storage_key,
            download_url=f"/api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download",
        )

    def resolve(self, storage_key: str) -> Path:
        """Resolve a storage key to an existing file under the generated artifact root."""

        path = self._path_for_storage_key(storage_key)
        if not path.is_file():
            raise FileNotFoundError(storage_key)
        return path

    def resolve_for(
        self, tenant_id: UUID, run_id: UUID, artifact_id: UUID, storage_key: str
    ) -> Path:
        """Resolve a storage key only when it belongs to the requested context."""

        stored_tenant_id, stored_run_id, stored_artifact_id, _ = self._parse_storage_key(
            storage_key
        )
        if (
            stored_tenant_id != tenant_id
            or stored_run_id != run_id
            or stored_artifact_id != artifact_id
        ):
            raise ValueError("storage_key context does not match requested artifact")
        return self.resolve(storage_key)

    def _path_for_storage_key(self, storage_key: str) -> Path:
        tenant_id, run_id, artifact_id, safe_filename = self._parse_storage_key(storage_key)
        candidate = (
            self._root / str(tenant_id) / str(run_id) / str(artifact_id) / safe_filename
        ).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError("storage_key escapes generated artifact root")
        return candidate

    def _parse_storage_key(self, storage_key: str) -> tuple[UUID, UUID, UUID, str]:
        parts = PurePosixPath(storage_key).parts
        if len(parts) != 4 or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("storage_key must have tenant/run/artifact/filename segments")
        if PurePosixPath(storage_key).is_absolute() or PureWindowsPath(storage_key).is_absolute():
            raise ValueError("storage_key must be relative")

        tenant_id, run_id, artifact_id, filename = parts
        try:
            parsed_tenant_id = UUID(tenant_id)
            parsed_run_id = UUID(run_id)
            parsed_artifact_id = UUID(artifact_id)
        except ValueError:
            raise ValueError("storage_key contains invalid UUID segments") from None

        safe_filename = safe_generated_filename(filename)
        return parsed_tenant_id, parsed_run_id, parsed_artifact_id, safe_filename


def safe_generated_filename(filename: str) -> str:
    """Return a filename that is safe to write under generated artifact storage."""

    normalized = filename.strip()
    if not normalized:
        raise ValueError("filename must not be blank")
    if normalized != filename:
        raise ValueError("filename must not contain surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("filename must not contain control characters")
    if PurePosixPath(normalized).name != normalized or PureWindowsPath(normalized).name != normalized:
        raise ValueError("filename must not contain path segments")
    if any(character in normalized for character in '<>:"|?*'):
        raise ValueError("filename contains unsupported characters")
    if normalized in {".", ".."}:
        raise ValueError("filename must be a file name")
    if normalized.endswith("."):
        raise ValueError("filename must not end with a dot")
    if normalized.split(".", 1)[0].upper() in _WINDOWS_RESERVED_DEVICE_NAMES:
        raise ValueError("filename must not use a Windows reserved device name")
    return normalized
