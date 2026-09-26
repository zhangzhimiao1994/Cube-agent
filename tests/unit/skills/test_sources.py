from __future__ import annotations

import hashlib
import io
import json
import zipfile

import httpx
import pytest

from agent_hub.skills.sources import (
    GitHubSkillSourceFetcher,
    SkillSourceFetchError,
    SkillSourceFetchRequest,
    UnsafeSkillSource,
    normalize_github_repository_url,
)


class FakeResolver:
    def __init__(self, addresses: dict[str, list[str]] | None = None) -> None:
        self.addresses = addresses or {
            "api.github.com": ["140.82.114.6"],
            "codeload.github.com": ["140.82.112.10"],
        }

    async def resolve(self, host: str) -> list[str]:
        return self.addresses.get(host, [])


def _repository_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("demo-abc123/README.md", "root")
        archive.writestr("demo-abc123/skills/research/SKILL.md", "# Research\n")
        archive.writestr("demo-abc123/skills/research/references/guide.md", "guide")
        archive.writestr("demo-abc123/other/ignored.txt", "ignore")
    return buffer.getvalue()


def test_normalize_github_repository_url_accepts_only_repository_root() -> None:
    assert (
        normalize_github_repository_url("https://github.com/NousResearch/hermes-agent.git")
        == "https://github.com/NousResearch/hermes-agent"
    )
    for unsafe in (
        "http://github.com/owner/repo",
        "https://user@github.com/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://github.example/owner/repo",
        "https://127.0.0.1/owner/repo",
    ):
        with pytest.raises(UnsafeSkillSource):
            normalize_github_repository_url(unsafe)


@pytest.mark.asyncio
async def test_fetch_pins_commit_filters_subdirectory_and_verifies_hash() -> None:
    archive_bytes = _repository_archive()
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if request.url.path.endswith(f"/zipball/{'a' * 40}"):
            return httpx.Response(
                302,
                headers={
                    "Location": f"https://codeload.github.com/acme/demo/legacy.zip/{'a' * 40}"
                },
            )
        return httpx.Response(200, content=archive_bytes)

    fetcher = GitHubSkillSourceFetcher(
        resolver=FakeResolver(),
        transport=httpx.MockTransport(handler),
    )
    snapshot = await fetcher.fetch(
        SkillSourceFetchRequest(
            repository_url="https://github.com/acme/demo",
            ref="main",
            subdirectory="skills",
            expected_archive_sha256=archive_sha256,
            credential="github-token",
        )
    )

    assert snapshot.commit_sha == "a" * 40
    assert snapshot.archive_sha256 == archive_sha256
    assert snapshot.source_archive_bytes == len(archive_bytes)
    with zipfile.ZipFile(io.BytesIO(snapshot.skill_archive)) as archive:
        assert sorted(archive.namelist()) == [
            "research/SKILL.md",
            "research/references/guide.md",
        ]
    assert seen[0][1] == "Bearer github-token"
    assert seen[-1][1] is None


@pytest.mark.asyncio
async def test_fetch_rejects_private_dns_resolution_before_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"sha": "a" * 40})

    fetcher = GitHubSkillSourceFetcher(
        resolver=FakeResolver({"api.github.com": ["127.0.0.1"]}),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnsafeSkillSource, match="unsafe"):
        await fetcher.fetch(
            SkillSourceFetchRequest(repository_url="https://github.com/acme/demo", ref="main")
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_fetch_rejects_cross_domain_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "b" * 40})
        return httpx.Response(302, headers={"Location": "https://evil.example/archive.zip"})

    fetcher = GitHubSkillSourceFetcher(
        resolver=FakeResolver(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnsafeSkillSource, match="redirect"):
        await fetcher.fetch(
            SkillSourceFetchRequest(repository_url="https://github.com/acme/demo", ref="main")
        )


@pytest.mark.asyncio
async def test_fetch_rejects_hash_mismatch_and_oversized_response() -> None:
    archive_bytes = _repository_archive()

    def mismatch_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "c" * 40})
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://codeload.github.com/a/b.zip"})
        return httpx.Response(200, content=archive_bytes)

    mismatch = GitHubSkillSourceFetcher(
        resolver=FakeResolver(),
        transport=httpx.MockTransport(mismatch_handler),
    )
    with pytest.raises(SkillSourceFetchError, match="hash"):
        await mismatch.fetch(
            SkillSourceFetchRequest(
                repository_url="https://github.com/acme/demo",
                ref="main",
                expected_archive_sha256="0" * 64,
            )
        )

    def oversized_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "d" * 40})
        return httpx.Response(
            200,
            headers={"Content-Length": "1025"},
            content=b"x" * 1025,
        )

    oversized = GitHubSkillSourceFetcher(
        resolver=FakeResolver(),
        transport=httpx.MockTransport(oversized_handler),
        max_archive_bytes=1024,
    )
    with pytest.raises(SkillSourceFetchError, match="too large"):
        await oversized.fetch(
            SkillSourceFetchRequest(repository_url="https://github.com/acme/demo", ref="main")
        )


@pytest.mark.asyncio
async def test_fetch_rejects_missing_subdirectory_without_importing_root() -> None:
    archive_bytes = _repository_archive()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, content=json.dumps({"sha": "e" * 40}).encode())
        return httpx.Response(200, content=archive_bytes)

    fetcher = GitHubSkillSourceFetcher(
        resolver=FakeResolver(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SkillSourceFetchError, match="subdirectory"):
        await fetcher.fetch(
            SkillSourceFetchRequest(
                repository_url="https://github.com/acme/demo",
                ref="main",
                subdirectory="missing",
            )
        )
