from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import re
import socket
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_MAX_API_RESPONSE_BYTES = 1_000_000
_MAX_ARCHIVE_ENTRIES = 4096
_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
_ALLOWED_DOWNLOAD_HOSTS = frozenset({"api.github.com", "codeload.github.com"})


class SkillSourceFetchError(RuntimeError):
    """Stable source synchronization failure without credentials or response bodies."""


class UnsafeSkillSource(SkillSourceFetchError):
    """The source URL, redirect, resolved address, or archive path is unsafe."""


class Resolver(Protocol):
    async def resolve(self, host: str) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class SkillSourceFetchRequest:
    repository_url: str
    ref: str
    subdirectory: str = ""
    expected_archive_sha256: str | None = None
    credential: str | None = None


@dataclass(frozen=True, slots=True)
class SkillSourceSnapshot:
    repository_url: str
    commit_sha: str
    archive_sha256: str
    source_archive_bytes: int
    skill_archive: bytes


class SystemResolver:
    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            results = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeSkillSource("skill source host could not be resolved") from exc
        return list(dict.fromkeys(item[4][0] for item in results))


def normalize_github_repository_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeSkillSource("skill source must be an HTTPS GitHub repository URL")
    parts = tuple(part for part in parsed.path.split("/") if part)
    if len(parts) != 2:
        raise UnsafeSkillSource("skill source must point to a GitHub repository root")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not _repository_component(owner) or not _repository_component(repository):
        raise UnsafeSkillSource("skill source repository path is invalid")
    return f"https://github.com/{owner}/{repository}"


def normalize_source_ref(value: str) -> str:
    normalized = value.strip()
    if (
        not _SAFE_REF.fullmatch(normalized)
        or ".." in PurePosixPath(normalized).parts
        or "//" in normalized
    ):
        raise UnsafeSkillSource("skill source ref is invalid")
    return normalized


def normalize_source_subdirectory(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if not normalized:
        return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeSkillSource("skill source subdirectory is invalid")
    return path.as_posix()


class GitHubSkillSourceFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
        max_archive_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self._resolver = resolver or SystemResolver()
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_archive_bytes = max_archive_bytes

    async def fetch(self, request: SkillSourceFetchRequest) -> SkillSourceSnapshot:
        repository_url = normalize_github_repository_url(request.repository_url)
        ref = normalize_source_ref(request.ref)
        subdirectory = normalize_source_subdirectory(request.subdirectory)
        owner, repository = repository_url.removeprefix("https://github.com/").split("/", 1)
        api_root = f"https://api.github.com/repos/{owner}/{repository}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-hub-skill-source/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if request.credential:
            headers["Authorization"] = f"Bearer {request.credential}"
        timeout = httpx.Timeout(self._timeout_seconds)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=timeout,
                trust_env=False,
            ) as client:
                commit_bytes, _ = await self._request_bytes(
                    client,
                    f"{api_root}/commits/{quote(ref, safe='')}",
                    headers=headers,
                    max_bytes=_MAX_API_RESPONSE_BYTES,
                    allow_redirect=False,
                )
                commit_sha = _commit_sha(commit_bytes)
                archive_bytes, _ = await self._request_bytes(
                    client,
                    f"{api_root}/zipball/{commit_sha}",
                    headers=headers,
                    max_bytes=self._max_archive_bytes,
                    allow_redirect=True,
                )
        except httpx.TimeoutException as exc:
            raise SkillSourceFetchError("skill source request timed out") from exc
        except httpx.HTTPError as exc:
            raise SkillSourceFetchError("skill source request failed") from exc
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        expected_sha256 = request.expected_archive_sha256
        if expected_sha256 is not None and archive_sha256 != expected_sha256.lower():
            raise SkillSourceFetchError("skill source archive hash does not match")
        filtered = _filter_repository_archive(archive_bytes, subdirectory)
        return SkillSourceSnapshot(
            repository_url=repository_url,
            commit_sha=commit_sha,
            archive_sha256=archive_sha256,
            source_archive_bytes=len(archive_bytes),
            skill_archive=filtered,
        )

    async def _request_bytes(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        max_bytes: int,
        allow_redirect: bool,
    ) -> tuple[bytes, str]:
        current = url
        current_headers = headers
        for redirect_count in range(2):
            await self._validate_target(current)
            async with client.stream("GET", current, headers=current_headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not allow_redirect or redirect_count > 0:
                        raise UnsafeSkillSource("skill source redirect is not allowed")
                    location = response.headers.get("location")
                    if not location:
                        raise SkillSourceFetchError("skill source redirect is invalid")
                    redirected = urljoin(current, location)
                    if urlsplit(redirected).hostname != "codeload.github.com":
                        raise UnsafeSkillSource("skill source redirect target is unsafe")
                    current = redirected
                    current_headers = {
                        "Accept": "application/zip",
                        "User-Agent": headers["User-Agent"],
                    }
                    continue
                if response.status_code != 200:
                    raise SkillSourceFetchError(
                        f"skill source returned status {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        announced = int(content_length)
                    except ValueError as exc:
                        raise SkillSourceFetchError(
                            "skill source content length is invalid"
                        ) from exc
                    if announced > max_bytes:
                        raise SkillSourceFetchError("skill source response is too large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SkillSourceFetchError("skill source response is too large")
                    chunks.append(chunk)
                return b"".join(chunks), current
        raise UnsafeSkillSource("skill source redirect target is unsafe")

    async def _validate_target(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UnsafeSkillSource("skill source target is unsafe")
        addresses = await self._resolver.resolve(parsed.hostname)
        if not addresses:
            raise UnsafeSkillSource("skill source target is unsafe")
        for address in addresses:
            try:
                parsed_ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeSkillSource("skill source target is unsafe") from exc
            if not parsed_ip.is_global or parsed_ip.is_multicast:
                raise UnsafeSkillSource("skill source target is unsafe")


def _repository_component(value: str) -> bool:
    return bool(value) and len(value) <= 100 and re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None


def _commit_sha(payload: bytes) -> str:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillSourceFetchError("skill source commit response is invalid") from exc
    sha = parsed.get("sha") if isinstance(parsed, dict) else None
    if not isinstance(sha, str) or _COMMIT_SHA.fullmatch(sha.lower()) is None:
        raise SkillSourceFetchError("skill source commit response is invalid")
    return sha.lower()


def _filter_repository_archive(archive_bytes: bytes, subdirectory: str) -> bytes:
    try:
        source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillSourceFetchError("skill source archive is invalid") from exc
    selected: list[tuple[str, zipfile.ZipInfo]] = []
    expanded_bytes = 0
    requested_parts = PurePosixPath(subdirectory).parts if subdirectory else ()
    with source:
        for info in source.infolist():
            if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                continue
            mode = (info.external_attr >> 16) & 0o777777
            if stat.S_IFMT(mode) in {
                stat.S_IFLNK,
                stat.S_IFCHR,
                stat.S_IFBLK,
                stat.S_IFIFO,
                stat.S_IFSOCK,
            }:
                raise UnsafeSkillSource("skill source archive contains unsafe file types")
            path = _safe_archive_path(info.filename)
            parts = PurePosixPath(path).parts
            if len(parts) < 2:
                continue
            relative_parts = parts[1:]
            if requested_parts:
                if relative_parts[: len(requested_parts)] != requested_parts:
                    continue
                relative_parts = relative_parts[len(requested_parts) :]
            if not relative_parts:
                continue
            inner_path = PurePosixPath(*relative_parts).as_posix()
            expanded_bytes += info.file_size
            if expanded_bytes > _MAX_EXPANDED_BYTES:
                raise SkillSourceFetchError("skill source archive expands beyond the limit")
            selected.append((inner_path, info))
            if len(selected) > _MAX_ARCHIVE_ENTRIES:
                raise SkillSourceFetchError("skill source archive contains too many files")
        if not selected:
            reason = "skill source subdirectory was not found" if subdirectory else "skill source archive is empty"
            raise SkillSourceFetchError(reason)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for path, info in selected:
                target_info = zipfile.ZipInfo(path)
                target_info.external_attr = info.external_attr
                target.writestr(target_info, source.read(info.filename))
        return output.getvalue()


def _safe_archive_path(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith(("/", "../"))
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UnsafeSkillSource("skill source archive contains unsafe paths")
    return path.as_posix()


__all__ = [
    "GitHubSkillSourceFetcher",
    "SkillSourceFetchError",
    "SkillSourceFetchRequest",
    "SkillSourceSnapshot",
    "UnsafeSkillSource",
    "normalize_github_repository_url",
    "normalize_source_ref",
    "normalize_source_subdirectory",
]
