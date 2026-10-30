"""Append-only run overlays in the existing Root supply state, never global catalog edits."""

from __future__ import annotations

import os
import time
from contextlib import ExitStack
from dataclasses import replace
from typing import Literal, cast

from agent_hub.skills.sandbox import python_dependency_index_store as files
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_worker import _once
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionDescriptor,
    need,
    require_sha,
)
from agent_hub.skills.sandbox.python_resolution_protocol import (
    MAX_CONTROL,
    ResolutionClaim,
    ResolutionPlan,
    ResolutionStatus,
    decode,
    decode_descriptor,
    encoded,
    resolution_key,
)


def overlay_supply(base: RootSupplyConfig, descriptor: ResolutionDescriptor) -> RootSupplyConfig:
    descriptor.__post_init__()
    entries = {(e.name, e.version): e for e in base.catalog}
    names = {e.filename: e for e in base.catalog}
    for wheel in descriptor.wheels:
        e = wheel.candidate.download
        need(
            entries.get((e.name, e.version), e) == e and names.get(e.filename, e) == e,
            "resolution_catalog_conflict",
        )
        entries[e.name, e.version] = e
        names[e.filename] = e
    need(
        sum(w.candidate.download.size_bytes for w in descriptor.wheels) <= base.max_total_bytes,
        "resolution_quota",
    )
    return replace(base, catalog=tuple(entries[k] for k in sorted(entries)))


def freeze_plan(
    descriptor: ResolutionDescriptor,
    *,
    original: ResolutionClaim,
    current: ResolutionClaim,
    overlay_sha256: str,
) -> ResolutionPlan:
    current.require_continuity(original)
    need(descriptor.scope == original.resolution_scope(), "resolution_scope_changed")
    return ResolutionPlan(
        claim=current,
        resolution_key=resolution_key(original, descriptor.needs, dict(descriptor.environment)),
        descriptor_sha256=descriptor.sha256,
        overlay_sha256=overlay_sha256,
        artifacts=tuple(
            sorted(
                (
                    w.candidate.download.filename,
                    w.candidate.download.sha256,
                    w.candidate.download.size_bytes,
                )
                for w in descriptor.wheels
            )
        ),
    )


class ResolutionStore:
    """Only Root call sites may persist values. Every operation reopens pinned Root FDs.

    The existing directory flock, private-file reader and O_EXCL writer implement
    ownership/no-links/fsync. No test-only permission switch exists here.
    """

    def __init__(self, supply: RootSupplyConfig) -> None:
        self.supply = replace(supply)

    def _create(self, name: str, value: object, deadline: float) -> None:
        files._native()
        with ExitStack() as stack:
            pins: list[tuple[int, str, int, tuple[int, ...]]] = []
            parent = files._state(self.supply, stack, pins, exclusive=True, deadline=deadline)
            _once(parent, name, value)
            files._directories(pins, deadline)

    def _read(self, name: str, deadline: float) -> object:
        files._native()
        with ExitStack() as stack:
            pins: list[tuple[int, str, int, tuple[int, ...]]] = []
            parent = files._state(self.supply, stack, pins, exclusive=False, deadline=deadline)
            raw, pin = files._read_file(parent, name, MAX_CONTROL, stack, deadline)
            value = decode(raw)
            pin.check()
            files._directories(pins, deadline)
            return value

    @staticmethod
    def _name(key: str, suffix: str) -> str:
        require_sha(key)
        need(
            suffix in {"intent", "receipt", "published"}
            or suffix.startswith(("plan-", "publish-", "result-"))
        )
        if suffix.startswith(("plan-", "publish-", "result-")):
            require_sha(suffix.split("-", 1)[1])
        return "resolution-" + key + "-" + suffix + ".json"

    def begin(self, key: str, claim: ResolutionClaim) -> None:
        try:
            self._create(self._name(key, "intent"), claim.model_dump(mode="json"), claim.deadline)
        except FileExistsError:
            raise ValueError("resolution_consumed") from None

    def original(self, key: str, current: ResolutionClaim) -> ResolutionClaim:
        result = ResolutionClaim.model_validate_json(
            encoded(self._read(self._name(key, "intent"), current.deadline)), strict=True
        )
        current.require_continuity(result)
        return result

    def complete(
        self, key: str, current: ResolutionClaim, descriptor: ResolutionDescriptor
    ) -> None:
        original = self.original(key, current)
        need(descriptor.scope == original.resolution_scope(), "resolution_scope_changed")
        need(key == resolution_key(original, descriptor.needs, dict(descriptor.environment)))
        self._create(self._name(key, "receipt"), descriptor.to_payload(), current.deadline)

    def inspect(self, key: str, current: ResolutionClaim) -> ResolutionStatus:
        try:
            original = self.original(key, current)
        except FileNotFoundError:
            return ResolutionStatus(state="absent", key=key)
        try:
            descriptor = decode_descriptor(self._read(self._name(key, "receipt"), current.deadline))
        except FileNotFoundError:
            return ResolutionStatus(state="unknown", key=key)
        need(
            descriptor.scope == original.resolution_scope()
            and key == resolution_key(original, descriptor.needs, dict(descriptor.environment)),
            "resolution_scope_changed",
        )
        return ResolutionStatus(state="resolved", key=key, descriptor=descriptor.to_payload())

    def freeze(self, key: str, current: ResolutionClaim) -> ResolutionPlan:
        status = self.inspect(key, current)
        need(
            status.state == "resolved" and status.descriptor is not None, "resolution_inspect_only"
        )
        descriptor = decode_descriptor(status.descriptor)
        effective = overlay_supply(self.supply, descriptor)
        plan = freeze_plan(
            descriptor,
            original=self.original(key, current),
            current=current,
            overlay_sha256=effective.digest,
        )
        name = self._name(key, "plan-" + plan.sha256)
        try:
            self._create(name, plan.model_dump(mode="json"), current.deadline)
        except FileExistsError:
            stored = ResolutionPlan.model_validate_json(
                encoded(self._read(name, current.deadline)), strict=True
            )
            need(stored.sha256 == plan.sha256, "resolution_plan_changed")
            return stored
        return plan

    def require_plan(self, key: str, current: ResolutionClaim, expected: str) -> ResolutionPlan:
        require_sha(expected)
        stored = ResolutionPlan.model_validate_json(
            encoded(self._read(self._name(key, "plan-" + expected), current.deadline)), strict=True
        )
        need(stored.sha256 == expected, "resolution_plan_changed")
        # Data continuity permits source changes, effect authority does not.
        current.require_continuity(stored.claim)
        need(stored.claim.effect_payload() == current.effect_payload(), "resolution_source_changed")
        self.original(key, current)
        return stored

    def publication_begin(self, key: str, current: ResolutionClaim, plan: ResolutionPlan) -> None:
        try:
            self._create(
                self._name(key, "publish-" + plan.sha256),
                {"state": "unknown", "plan_sha256": plan.sha256},
                current.deadline,
            )
        except FileExistsError:
            raise ValueError("resolution_consumed") from None

    def put_wheel(self, key: str, sha: str, raw: bytes, *, deadline: float) -> None:
        require_sha(key)
        require_sha(sha)
        need(type(raw) is bytes and 0 < len(raw) <= 64 * 1024**2)
        files._native()
        with ExitStack() as stack:
            pins: list[tuple[int, str, int, tuple[int, ...]]] = []
            parent = files._state(self.supply, stack, pins, exclusive=True, deadline=deadline)
            fd = os.open(
                "resolution-" + key + "-" + sha + ".whl",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(raw):
                    need(time.monotonic() < deadline, "resolution_deadline")
                    count = os.write(fd, raw[offset : offset + 65536])
                    need(count > 0)
                    offset += count
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)
            files._directories(pins, deadline)

    def wheel(self, key: str, sha: str, size: int, *, deadline: float) -> bytes:
        require_sha(key)
        require_sha(sha)
        need(type(size) is int and 0 < size <= 64 * 1024**2)
        files._native()
        with ExitStack() as stack:
            pins: list[tuple[int, str, int, tuple[int, ...]]] = []
            parent = files._state(self.supply, stack, pins, exclusive=False, deadline=deadline)
            raw, pin = files._read_file(
                parent,
                "resolution-" + key + "-" + sha + ".whl",
                size,
                stack,
                deadline,
                expected_size=size,
            )
            pin.check()
            files._directories(pins, deadline)
            return raw

    def publication_finish(
        self, key: str, current: ResolutionClaim, plan: ResolutionPlan, index_sha256: str
    ) -> None:
        require_sha(index_sha256)
        self._create(
            self._name(key, "result-" + plan.sha256),
            {"plan_sha256": plan.sha256, "index_sha256": index_sha256},
            current.deadline,
        )

    def publication_status(
        self, key: str, current: ResolutionClaim, plan: ResolutionPlan
    ) -> Literal["frozen", "unknown", "published"]:
        try:
            intent = self._read(self._name(key, "publish-" + plan.sha256), current.deadline)
        except FileNotFoundError:
            return "frozen"
        need(intent == {"state": "unknown", "plan_sha256": plan.sha256}, "resolution_plan_changed")
        try:
            receipt = self._read(self._name(key, "result-" + plan.sha256), current.deadline)
        except FileNotFoundError:
            return "unknown"
        need(
            type(receipt) is dict
            and set(receipt) == {"plan_sha256", "index_sha256"}
            and receipt["plan_sha256"] == plan.sha256
        )
        require_sha(cast(dict[str, str], receipt)["index_sha256"])
        return "published"
