"""Current SQL key data, not creator authorization or an install capability.

The caller owns explicit PostgreSQL BEGIN and the preceding plan/registration/
grant/current-user/run locks. This SHARE lock remains in that same transaction.
After its last lock wait, the caller must sample one clock and invoke the package
verifier: this loader validates window structure, never window liveness. No
commit, flush, fallback, filesystem operation or generated-code execution.
"""

from __future__ import annotations

import base64
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.db.models import AdminResourceRow
from agent_hub.plugins.handover_grants import _transaction
from agent_hub.plugins.handover_storage import StorageUnavailable, _canonical
from agent_hub.plugins.handover_verification import FrozenSigningKey


async def load_locked_signing_key(
    session: AsyncSession, *, tenant_id: UUID, key_id: str
) -> FrozenSigningKey:
    """Read one explicitly trusted tenant key; caller verifies after the last wait.

    Missing, untrusted, inconsistent or unreadable rows all fail closed with a safe
    storage error. Returning a frozen value neither releases the lock nor grants
    permission; it cannot be reused as proof of a later transaction's current key.
    """
    try:
        _transaction(session)
        if (
            type(tenant_id) is not UUID
            or type(key_id) is not str
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", key_id) is None
        ):
            raise ValueError("invalid key lookup identity")
        row = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == tenant_id,
                AdminResourceRow.kind == "plugin_signing_key",
                AdminResourceRow.resource_id == key_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if (
            not isinstance(row, AdminResourceRow)
            or not isinstance(row.id, UUID)
            or not isinstance(row.tenant_id, UUID)
            or row.tenant_id != tenant_id
            or row.kind != "plugin_signing_key"
            or row.resource_id != key_id
        ):
            raise ValueError("key row identity mismatch")
        raw = row.payload
        required = {"key_id", "algorithm", "public_key", "trusted"}
        if (
            type(raw) is not dict
            or not required <= raw.keys()
            or raw.keys() - required - {"not_before", "not_after"}
            or raw["trusted"] is not True
            or raw["key_id"] != key_id
        ):
            raise ValueError("invalid trusted key envelope")

        from agent_hub.api.routers.admin import PluginSigningKeyRequest, _decode_base64url_bytes

        # Persisted JSON timestamps must use the existing aware-window contract.
        # Never use the response DTO, whose trust field has a permissive default.
        parsed = PluginSigningKeyRequest.model_validate_json(
            _canonical({name: value for name, value in raw.items() if name != "trusted"}),
            strict=True,
        )
        public = _decode_base64url_bytes(parsed.public_key, expected_length=32, label="public_key")
        if base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii") != parsed.public_key:
            raise ValueError("noncanonical signing key encoding")
        return FrozenSigningKey(
            key_id=parsed.key_id,
            algorithm=parsed.algorithm,
            public_key=public,
            trusted=True,
            not_before=parsed.not_before,
            not_after=parsed.not_after,
        )
    except (
        StorageUnavailable,
        SQLAlchemyError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
    ):
        raise StorageUnavailable("current signing key unavailable") from None
