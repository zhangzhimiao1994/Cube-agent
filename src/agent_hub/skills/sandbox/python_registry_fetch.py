"""Fixed PyPI metadata in the existing non-Root fetch unit; Root only handles bytes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sys
import time
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_hub.capabilities.tools.http_read import HttpResponse, Resolver, Transport
from agent_hub.skills.sandbox.python_input_supply_worker import _run_fetch_unit
from agent_hub.skills.sandbox.python_registry_protocol import (
    RegistryProvenance,
    ResolutionBudget,
    ResolutionError,
    ResolutionLimits,
    need,
)
from agent_hub.skills.sandbox.python_registry_read import (
    RegistryReader,
    _dict,
    _object,
    _RegistryTransport,
)
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionRequest, encoded

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
    from agent_hub.skills.sandbox.python_project_backend import RootConfig


class MetadataReadRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    name: str = Field(min_length=1, max_length=128, pattern="^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str | None = Field(
        default=None, min_length=1, max_length=128, pattern="^[A-Za-z0-9.!+_-]+$"
    )
    maximum: int = Field(gt=0, le=2 * 1024**2)
    deadline: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        need(math.isfinite(self.deadline))
        return self

    @property
    def url(self) -> str:
        return (
            f"https://pypi.org/simple/{self.name}/"
            if self.version is None
            else f"https://pypi.org/pypi/{self.name}/{self.version}/json"
        )


def _nonfinite(value: str) -> object:
    raise ResolutionError("registry_nonfinite")


async def read_metadata_snapshot(
    request: MetadataReadRequest,
    *,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
) -> dict[str, object]:
    """One exchange in the child; original deadline, fixed TLS/DNS pin and JSON checks."""
    request = MetadataReadRequest.model_validate(request, strict=True)
    budget = ResolutionBudget(
        ResolutionLimits(
            max_requests=1, max_response_bytes=request.maximum, max_metadata_bytes=request.maximum
        ),
        request.deadline,
    )
    budget.check()
    bodies: list[bytes] = []

    class Capture:
        async def get(self, url: str, *, resolved_addresses: tuple[str, ...]) -> HttpResponse:
            response = await (
                transport or _RegistryTransport(request.maximum, request.deadline)
            ).get(url, resolved_addresses=resolved_addresses)
            bodies.append(response.body)
            return response

    reader = RegistryReader(resolver=resolver, transport=Capture())
    _, proof = await reader.read(request.name, request.version, budget=budget)
    need(len(bodies) == 1 and proof.url == request.url, "registry_worker_invalid")
    raw = bodies[0]
    need(hashlib.sha256(raw).hexdigest() == proof.sha256, "registry_worker_invalid")
    budget.check()
    return {
        "schema_version": 1,
        "url": request.url,
        "body_base64": base64.b64encode(raw).decode("ascii"),
    }


class BrokerRegistryReader(RegistryReader):
    """Inherit pure candidate selection, overriding the entire DNS/HTTP read boundary.

    Every request/byte is charged to the same parent ledger. Child stdout carries
    no actor, grant, receipt or authorization flag; Root recomputes the raw digest.
    """

    def __init__(
        self,
        request: ResolutionRequest,
        policy: BrokerPolicy,
        root: RootConfig,
        supply: RootSupplyConfig,
    ) -> None:
        self.request, self.policy, self.root, self.supply = request, policy, root, supply

    async def read(
        self, name: str, version: str | None = None, *, budget: ResolutionBudget
    ) -> tuple[dict[str, object], RegistryProvenance]:
        budget.consume("requests")
        remaining = budget.limits.max_metadata_bytes - dict(budget.usage).get("metadata_bytes", 0)
        need(remaining > 0, "resolution_metadata_bytes_limit")
        request = MetadataReadRequest(
            name=name,
            version=version,
            maximum=min(budget.limits.max_response_bytes, remaining),
            deadline=min(budget.deadline, time.monotonic() + self.supply.fetch_timeout_seconds),
        )
        budget.check()
        maximum = 4 * math.ceil(request.maximum / 3) + 4096
        result = await _run_fetch_unit(
            self.request.claim.planning.invocation,
            self.policy,
            self.root,
            self.supply,
            request.deadline,
            payload_raw=request.model_dump_json().encode("ascii"),
            maximum=maximum,
            registry=True,
        )
        budget.check()
        need(
            type(result.stdout) is str and 0 < len(result.stdout.encode("utf-8")) <= maximum,
            "registry_worker_invalid",
        )
        response = json.loads(result.stdout, object_pairs_hook=_object, parse_constant=_nonfinite)
        need(
            type(response) is dict
            and set(response) == {"schema_version", "url", "body_base64"}
            and type(response["schema_version"]) is int
            and response["schema_version"] == 1
            and response["url"] == request.url
            and type(response["body_base64"]) is str,
            "registry_worker_invalid",
        )
        raw = base64.b64decode(response["body_base64"], validate=True)
        need(0 < len(raw) <= request.maximum, "registry_response_limit")
        budget.consume("metadata_bytes", len(raw))
        document = _dict(json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite))
        budget.check()
        return document, RegistryProvenance(request.url, hashlib.sha256(raw).hexdigest())


def main() -> int:
    try:
        need(
            sys.flags.isolated
            and sys.dont_write_bytecode
            and not sys.flags.optimize
            and sys.platform == "linux"
            and os.geteuid() != 0,
            "registry_worker_invalid",
        )
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument("--metadata-worker", action="store_true", required=True)
        parser.parse_args()
        raw = sys.stdin.buffer.read(8193)
        need(0 < len(raw) <= 8192, "registry_worker_invalid")
        # Reject duplicate fields/nonfinite values before Pydantic may erase them.
        query = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        request = MetadataReadRequest.model_validate(query, strict=True)
        import asyncio

        response = asyncio.run(read_metadata_snapshot(request))
        print(encoded(response).decode("ascii"), flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - finite code only, no reflected metadata.
        from agent_hub.skills.sandbox.python_resolution_protocol import (
            resolution_failure_diagnostic,
        )

        diagnostic = resolution_failure_diagnostic(error, phase="registry_metadata", exit_code=2)
        print(diagnostic.model_dump_json(), flush=True)
        print("registry_worker_unavailable", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
