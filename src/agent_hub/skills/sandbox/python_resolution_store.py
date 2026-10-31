"""Append-only run overlays in the existing Root supply state, never global catalog edits."""

from __future__ import annotations

import os
import sys
import time
from contextlib import ExitStack
from dataclasses import replace
from typing import Literal, cast

from agent_hub.skills.sandbox import python_dependency_index_store as files
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_worker import _once
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionDescriptor,
    digest,
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
    current.require_data_origin(original)
    current = current.model_copy(update={"monotonic_deadline": min(current.monotonic_deadline, original.monotonic_deadline)})
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
            or suffix.startswith(("plan-", "publish-", "result-", "next-"))
        )
        if suffix.startswith(("plan-", "publish-", "result-", "next-")):
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

    def data_original(self, key: str, current: ResolutionClaim) -> ResolutionClaim:
        result = ResolutionClaim.model_validate_json(
            encoded(self._read(self._name(key, "intent"), current.deadline)), strict=True)
        current.require_data_origin(result)
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
            original = self.data_original(key, current)
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
            original=self.data_original(key, current),
            current=current,
            overlay_sha256=effective.digest,
        )
        write_deadline = min(current.deadline,plan.claim.monotonic_deadline)
        name = self._name(key, "plan-" + plan.sha256)
        try:
            self._create(name, plan.model_dump(mode="json"), write_deadline)
        except FileExistsError:
            stored = ResolutionPlan.model_validate_json(
                encoded(self._read(name, write_deadline)), strict=True
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
        current.require_data_origin(stored.claim)
        need(stored.claim.approval_payload() == current.approval_payload(), "resolution_source_changed")
        self.data_original(key, current)
        return stored

    def _publication_claim(self, key: str, current: ResolutionClaim, plan: ResolutionPlan) -> None:
        """Append-only per-dataset chain: a different source cannot escape UNKNOWN."""
        deadline = min(current.deadline, plan.claim.monotonic_deadline)
        name = self._name(key, "published")
        seen: set[str] = set()
        while True:
            try:
                self._create(name, {"plan_sha256": plan.sha256}, deadline)
                return
            except FileExistsError:
                selected = self._read(name, deadline)
            need(type(selected) is dict and set(selected) == {"plan_sha256"}, "resolution_publication_inspect_only")
            previous = cast(dict[str, str], selected)["plan_sha256"]
            require_sha(previous)
            need(previous not in seen, "resolution_publication_inspect_only")
            seen.add(previous)
            if previous == plan.sha256:
                return
            # Missing intent/result, a partial record, or an expired deadline
            # keeps this entire dataset inspect-only. Never replace the pointer.
            try:
                prior = ResolutionPlan.model_validate_json(encoded(self._read(self._name(key, "plan-" + previous), deadline)), strict=True)
                need(prior.sha256 == previous and prior.resolution_key == key, "resolution_publication_inspect_only")
                need(self.publication_status(key, current, prior) == "published", "resolution_publication_inspect_only")
            except FileNotFoundError:
                raise ValueError("resolution_publication_inspect_only") from None
            name = self._name(key, "next-" + previous)

    def _check_publication_history(
        self,
        key: str,
        original: ResolutionClaim,
        plan: ResolutionPlan,
        *,
        deadline: float,
    ) -> None:
        """Root FD evidence, including legacy sources outside the new once chain."""
        files._native()
        prefix = "resolution-" + key + "-"
        seen: set[str] = set()
        name_bytes = evidence_bytes = 0
        with ExitStack() as stack:
            pins: list[tuple[int, str, int, tuple[int, ...]]] = []
            parent = files._state(self.supply, stack, pins, exclusive=False, deadline=deadline)

            def read(name: str) -> object:
                nonlocal evidence_bytes
                need(evidence_bytes < MAX_CONTROL, "resolution_history_limit")
                with ExitStack() as reading:
                    raw, pin = files._read_file(
                        parent, name, MAX_CONTROL - evidence_bytes, reading, deadline
                    )
                    evidence_bytes += len(raw)
                    value = decode(raw)
                    pin.check()
                    files._directories(pins, deadline)
                    return value

            # Never list an untrusted path or materialize an unbounded directory.
            # Every entry and byte consumes a finite format/work budget; timeout
            # or incomplete history rejects before creating any new once record.
            with os.scandir(parent) as entries:
                for entry in entries:
                    files._check(deadline)
                    name_bytes += len(entry.name.encode("utf-8", errors="surrogateescape"))
                    need(name_bytes <= MAX_CONTROL, "resolution_history_limit")
                    if not entry.name.startswith(prefix):
                        continue
                    suffix = entry.name[len(prefix) :]
                    if not suffix.startswith(("publish-", "result-")):
                        continue
                    need(suffix.endswith(".json"), "resolution_publication_inspect_only")
                    expected = suffix.split("-", 1)[1][:-5]
                    require_sha(expected)
                    if expected in seen:
                        continue
                    seen.add(expected)
                    try:
                        intent = read(self._name(key, "publish-" + expected))
                        need(
                            intent == {"state": "unknown", "plan_sha256": expected},
                            "resolution_publication_inspect_only",
                        )
                        if expected == plan.sha256:
                            raise ValueError("resolution_consumed")
                        stored = ResolutionPlan.model_validate_json(
                            encoded(read(self._name(key, "plan-" + expected))), strict=True
                        )
                        legacy = digest(
                            {
                                **stored.model_dump(mode="json"),
                                "claim": stored.claim.effect_payload(),
                            }
                        )
                        need(
                            expected in {stored.sha256, legacy}
                            and stored.resolution_key == key
                            and stored.claim.data_identity() == original.data_identity()
                            and stored.claim.original_deadline <= original.original_deadline
                            and stored.claim.monotonic_deadline <= original.monotonic_deadline
                            and stored.descriptor_sha256 == plan.descriptor_sha256
                            and stored.overlay_sha256 == plan.overlay_sha256
                            and stored.artifacts == plan.artifacts,
                            "resolution_publication_inspect_only",
                        )
                        result = read(self._name(key, "result-" + expected))
                    except FileNotFoundError:
                        raise ValueError("resolution_publication_inspect_only") from None
                    need(
                        type(result) is dict
                        and set(result) == {"plan_sha256", "index_sha256"}
                        and result["plan_sha256"] == expected,
                        "resolution_publication_inspect_only",
                    )
                    require_sha(cast(dict[str, str], result)["index_sha256"])
                    files._directories(pins, deadline)
            files._directories(pins, deadline)

    def publication_begin(self, key: str, current: ResolutionClaim, plan: ResolutionPlan) -> None:
        original = self.data_original(key, current)
        deadline = min(current.deadline, plan.claim.monotonic_deadline, original.monotonic_deadline)
        self._check_publication_history(key, original, plan, deadline=deadline)
        self._publication_claim(key, current, plan)
        try:
            self._create(
                self._name(key, "publish-" + plan.sha256),
                {"state": "unknown", "plan_sha256": plan.sha256},
                deadline,
            )
        except FileExistsError:
            raise ValueError("resolution_consumed") from None

    def put_wheel(self, key: str, sha: str, raw: bytes, *, deadline: float) -> None:
        require_sha(key)
        require_sha(sha)
        need(type(raw) is bytes and 0 < len(raw) <= 64 * 1024**2)
        files._native()
        if sys.platform != "linux":
            raise ValueError("resolution_unavailable")
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
            min(current.deadline, plan.claim.monotonic_deadline),
        )

    def inspect_publication(self, key: str, current: ResolutionClaim, expected: str) -> ResolutionStatus:
        """Opaque read-only lookup; no old private plan or grant leaves Root."""
        require_sha(expected)
        stored = ResolutionPlan.model_validate_json(
            encoded(self._read(self._name(key,"plan-"+expected),current.deadline)),strict=True)
        legacy = digest({**stored.model_dump(mode="json"),"claim":stored.claim.effect_payload()})
        need(expected in {stored.sha256,legacy} and stored.resolution_key == key,"resolution_plan_changed")
        original = ResolutionClaim.model_validate_json(
            encoded(self._read(self._name(key,"intent"),current.deadline)),strict=True)
        need(current.data_identity() == original.data_identity() == stored.claim.data_identity()
            and current.planning.draft == stored.claim.planning.draft,"resolution_source_changed")
        try:
            intent=self._read(self._name(key,"publish-"+expected),current.deadline)
        except FileNotFoundError:
            return ResolutionStatus(state="frozen",key=key)
        need(intent == {"state":"unknown","plan_sha256":expected},"resolution_plan_changed")
        try:
            result=self._read(self._name(key,"result-"+expected),current.deadline)
        except FileNotFoundError:
            return ResolutionStatus(state="unknown",key=key)
        need(type(result) is dict and set(result) == {"plan_sha256","index_sha256"}
            and result["plan_sha256"] == expected,"resolution_plan_changed")
        require_sha(cast(dict[str,str],result)["index_sha256"])
        return ResolutionStatus(state="published",key=key)

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
