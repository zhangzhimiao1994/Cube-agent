"""Synthetic operator files for offline validator tests, never browser acceptance."""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import uuid4

from PIL import Image, ImageDraw

from agent_hub.previews.cleanup import (
    BROKER_RESOURCES,
    DYNAMIC_UNOBSERVED,
    MANAGER_RESOURCES,
    CleanupReceiptV1,
    PreviewCleanupRecord,
    PreviewIdentityV1,
    PreviewOwnerScope,
    ResourceObservation,
)
from agent_hub.previews.provenance import PreviewProvenanceV1, snapshot_manifest_for_identity


@lru_cache(maxsize=2)
def _viewport_png(width: int, height: int) -> bytes:
    with Image.new("RGB", (width, height), "white") as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, 55), fill=(25, 55, 85))
        draw.rectangle((20, 80, 220, 120), fill=(30, 150, 110))
        draw.text((20, 150), "Synthetic record app", fill="black")
        output = BytesIO()
        image.save(output, format="PNG")
    return output.getvalue()


def build_device_bundle(
    root: Path, *, scope: Mapping[str, object],
    validated_manifest: Mapping[str, tuple[int, str]], device: str, stem: str = "case",
) -> dict[str, object]:
    """Write complete, independent synthetic evidence and return its descriptor."""
    width, height = {"desktop": (1440, 960), "mobile": (390, 844)}[device]
    directory = root / f"{stem}-{device}"
    directory.mkdir(parents=True, exist_ok=False)

    def write(name: str, value: object) -> dict[str, object]:
        data = value if isinstance(value, bytes) else json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
        ).encode()
        target = directory / name
        target.write_bytes(data)
        return {"path": target.relative_to(root).as_posix(), "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    bound = copy.deepcopy(dict(scope))
    execution = cast(Mapping[str, object], bound["execution_identity"])
    owner = PreviewOwnerScope(
        cast(str, execution["tenant_id"]), cast(str, execution["user_id"]),
        cast(str, bound["project_id"]), cast(str, bound["conversation_id"]),
        cast(str, bound["workspace_session_id"]),
    )
    identity = PreviewIdentityV1.create(
        str(uuid4()), owner, kind="dynamic", handle=uuid4().hex,
        digest=hashlib.sha256(b"synthetic dynamic source tree framing").hexdigest(),
    )
    base = datetime.now(UTC) - timedelta(minutes=1)

    def at(seconds: int) -> datetime:
        return base + timedelta(seconds=seconds)

    provenance = PreviewProvenanceV1(
        1, identity, snapshot_manifest_for_identity(validated_manifest, identity), at(0),
    )
    facts: list[ResourceObservation] = []
    for resource in BROKER_RESOURCES + MANAGER_RESOURCES:
        unit = resource.startswith("unit_") or resource == "mount_unit"
        result = ("inactive" if unit else "exited" if resource == "attachment"
                  else "revoked" if resource == "capability" else "absent")
        facts.append(ResourceObservation(
            resource, "broker" if resource in BROKER_RESOURCES else "manager", at(7),
            result, "observed", load_state="not-found" if unit else None,
            active_state="inactive" if unit else None,
            main_pid=0 if resource.startswith("unit_") else None,
            identity_match=True if unit else None,
        ))
    receipt = CleanupReceiptV1(
        1, identity, str(uuid4()), at(6), at(8), "explicit", "confirmed",
        "dynamic-systemd-tmpfs-v1", tuple(facts), DYNAMIC_UNOBSERVED,
    )
    cleanup_record = PreviewCleanupRecord(identity, receipt, at(3600))
    url_digest = hashlib.sha256(f"synthetic-owned-url:{identity.preview_id}".encode()).hexdigest()
    unique = f"operator-{uuid4()}"
    record = {"id": str(uuid4()), "value": unique}
    common = {"schema_version": 1, "scope": bound, "preview_identity": identity.to_wire()}
    viewport = {"width": width, "height": height}
    render = {
        **common, "device": device, "viewport": viewport, "observed_at": at(2).isoformat(),
        "content_url_sha256": url_digest, "title": "Synthetic record app",
        "content_text": "Records: create and inspect an operator record",
        "visible_controls": [{"selector": "#record-value", "text": "Record value",
                              "bounds": {"x": 20, "y": 80, "width": 200, "height": 40}}],
        "frame": None, "assets": [], "console_errors": [], "page_errors": [],
    }
    business = {
        **common, "unique_value": unique,
        "selectors": {"before_records": ["records"], "before_success": ["success"],
                      "mutation_record": ["record"],
                      "readback_record": ["record"], "success": ["success"],
                      "id_field": "id", "value_field": "value"},
        "before": {"method": "GET", "path": "api/records", "observed_at": at(3).isoformat(),
                   "status": 200, "body": {"success": True, "records": []}},
        "mutation": {"method": "POST", "path": "api/records", "observed_at": at(4).isoformat(),
                     "status": 201, "body": {"success": True, "record": record},
                     "input_value": unique, "control": {"selector": "#record-value", "visible": True}},
        "readback": {"method": "GET", "path": f"api/records/{record['id']}",
                     "observed_at": at(5).isoformat(), "status": 200,
                     "body": {"success": True, "record": record},
                     "cache": "no-store", "after_reload": True},
    }
    cleanup = {
        **common, "cleanup_record": cleanup_record.to_wire(),
        "revocation": {"method": "GET", "content_url_sha256": url_digest,
                       "observed_at": at(9).isoformat(), "status": 404},
    }
    files = {
        "viewport_png": write("viewport.png", _viewport_png(width, height)), "frame_png": None,
        "render": write("render.json", render), "business": write("business.json", business),
        "provenance": write("provenance.json", provenance.to_wire()),
        "cleanup": write("cleanup.json", cleanup),
    }
    bundle_file = write("bundle.json", {
        "schema_version": 2, "scope": bound, "device": device, "viewport": viewport,
        "preview_identity": identity.to_wire(), "captured_at": at(1).isoformat(), "files": files,
    })
    return {"schema_version": 2, "passed": True, "observed_at": at(10).isoformat(),
            "evidence_ref": bundle_file["path"], "bundle_file": bundle_file,
            "checks": {"preview_rendered": True, "preview_interaction": True, "preview_revoked": True}}
