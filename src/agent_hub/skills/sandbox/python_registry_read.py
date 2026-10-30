"""Fixed official registry metadata reads. No wheel download, registration or approval."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
from urllib.parse import SplitResult

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from agent_hub.capabilities.tools.http_read import HttpResponse, Resolver, SystemResolver, Transport
from agent_hub.security.network import canonical_ip
from agent_hub.skills.sandbox.python_input_fetch import (
    WheelDownload,
    _headers,
    _WheelTransport,
)
from agent_hub.skills.sandbox.python_registry_protocol import (
    DependencyNeed,
    RegistryCandidate,
    RegistryProvenance,
    ResolutionBudget,
    ResolutionError,
    need,
    requirement,
    target_environment,
)


class _RegistryTransport(_WheelTransport):
    async def _request_address(
        self,
        *,
        parsed: SplitResult,
        host: str,
        address: str,
        port: int,
    ) -> HttpResponse:
        need(host == "pypi.org" and port == 443 and parsed.scheme == "https")
        reader, writer = await asyncio.open_connection(
            address,
            port,
            ssl=self.context,
            server_hostname=host,
        )
        try:
            writer.write(
                (
                    f"GET {parsed.path} HTTP/1.1\r\nHost: pypi.org\r\n"
                    "Accept: application/vnd.pypi.simple.v1+json, application/json\r\n"
                    "Accept-Encoding: identity\r\nConnection: close\r\n"
                    "User-Agent: AgentHub-Registry/1\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            return await self._read_response(reader)
        finally:
            writer.close()
            try:
                async with asyncio.timeout_at(min(self.deadline, time.monotonic() + 0.25)):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                writer.transport.abort()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        need(key not in result, "registry_duplicate_key")
        result[key] = value
    return result


def _dict(value: object) -> dict[str, object]:
    need(type(value) is dict, "registry_shape")
    assert isinstance(value, dict)
    return value


def _text(value: object) -> str:
    need(type(value) is str and 0 < len(value) <= 2048, "registry_shape")
    assert isinstance(value, str)
    return value


class RegistryReader:
    """Caller establishes behavioral read authority; this module enforces network/data limits."""

    def __init__(self, *, resolver: Resolver | None = None, transport: Transport | None = None):
        self.resolver = resolver or SystemResolver()
        self.transport = transport

    async def read(
        self,
        name: str,
        version: str | None = None,
        *,
        budget: ResolutionBudget,
    ) -> tuple[dict[str, object], RegistryProvenance]:
        need(type(name) is str and re.fullmatch("[a-z0-9]+(?:-[a-z0-9]+)*", name))
        if version is not None:
            need(type(version) is str and re.fullmatch("[A-Za-z0-9.!+_-]{1,128}", version))
        url = (
            f"https://pypi.org/simple/{name}/"
            if version is None
            else f"https://pypi.org/pypi/{name}/{version}/json"
        )
        budget.consume("requests")
        try:
            async with asyncio.timeout_at(budget.deadline):
                answers = await self.resolver.resolve("pypi.org")
                need(type(answers) is list and 0 < len(answers) <= 16, "registry_dns")
                addresses: list[str] = []
                for answer in answers:
                    address = canonical_ip(answer) if type(answer) is str else None
                    need(address is not None, "registry_dns")
                    assert address is not None
                    ip = ipaddress.ip_address(address)
                    need(ip.is_global and not ip.is_multicast, "registry_dns")
                    addresses.append(address)
                budget.check()
                # Exactly one HTTP exchange; no hidden retry across DNS answers.
                response = await (
                    self.transport
                    or _RegistryTransport(
                        budget.limits.max_response_bytes,
                        budget.deadline,
                    )
                ).get(url, resolved_addresses=(addresses[0],))
                budget.check()
                need(response.status_code == 200, "registry_http")
                headers = _headers(response.headers, budget.limits.max_response_bytes)
                kind = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                expected = "application/json" if version else "application/vnd.pypi.simple.v1+json"
                need(kind == expected, "registry_content_type")
                need(
                    type(response.body) is bytes
                    and 0 < len(response.body) <= budget.limits.max_response_bytes,
                    "registry_response_limit",
                )
                budget.consume("metadata_bytes", len(response.body))
                if "content-length" in headers:
                    need(int(headers["content-length"]) == len(response.body), "registry_length")
                value = json.loads(
                    response.body,
                    object_pairs_hook=_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(
                        ResolutionError("registry_nonfinite")
                    ),
                )
                budget.check()
                return _dict(value), RegistryProvenance(
                    url,
                    hashlib.sha256(response.body).hexdigest(),
                )
        except ResolutionError:
            raise
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
            raise ResolutionError("registry_unavailable") from None

    async def candidates(
        self,
        query: DependencyNeed,
        *,
        environment: dict[str, str],
        budget: ResolutionBudget,
    ) -> tuple[RegistryCandidate, ...]:
        query.__post_init__()
        env = target_environment(environment)
        parsed = requirement(query.requirement)
        name = str(canonicalize_name(parsed.name))
        index, index_proof = await self.read(name, budget=budget)
        need(canonicalize_name(_text(index.get("name"))) == name, "registry_identity")
        meta = _dict(index.get("meta"))
        api_version = meta.get("api-version")
        need(type(api_version) is str and api_version.startswith("1."), "registry_api_version")
        versions = index.get("versions")
        need(
            type(versions) is list and len(versions) <= budget.limits.max_candidates,
            "registry_versions_limit",
        )
        assert isinstance(versions, list)
        eligible: list[tuple[Version, str]] = []
        for text in versions:
            budget.check()
            try:
                version = Version(_text(text))
            except InvalidVersion:
                continue
            if (
                not version.is_prerelease
                and not version.is_devrelease
                and not version.local
                and parsed.specifier.contains(version)
            ):
                eligible.append((version, _text(text)))
        for _, text in sorted(set(eligible), reverse=True):
            budget.consume("candidates")
            document, proof = await self.read(name, text, budget=budget)
            info = _dict(document.get("info"))
            need(
                canonicalize_name(_text(info.get("name"))) == name and info.get("version") == text,
                "registry_identity",
            )
            rows = document.get("urls")
            need(type(rows) is list and len(rows) <= 1000, "registry_files_limit")
            assert isinstance(rows, list)
            selected: list[RegistryCandidate] = []
            for raw in rows:
                budget.check()
                try:
                    row = _dict(raw)
                    if (
                        row.get("packagetype") != "bdist_wheel"
                        or row.get("yanked") is not False
                        or info.get("yanked") is not False
                    ):
                        continue
                    compatible = True
                    for constraint in (info.get("requires_python"), row.get("requires_python")):
                        if constraint is not None:
                            compatible &= SpecifierSet(_text(constraint)).contains(
                                env["python_full_version"],
                                prereleases=True,
                            )
                    if not compatible:
                        continue
                    size = row.get("size")
                    need(type(size) is int, "registry_size")
                    assert isinstance(size, int)
                    entry = WheelDownload(
                        _text(row.get("filename")),
                        name,
                        text,
                        _text(_dict(row.get("digests")).get("sha256")),
                        size,
                        _text(row.get("url")),
                    )
                    selected.append(RegistryCandidate(entry, proof, index_proof))
                except (ValueError, TypeError, KeyError):
                    continue
            need(len(selected) <= 1, "registry_ambiguous_wheel")
            if selected:
                return tuple(selected)
        raise ResolutionError("registry_no_candidate")
