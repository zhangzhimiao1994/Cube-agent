from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.capability_installer.environment import CapabilityEnvironmentManager
from agent_hub.capability_installer.install_gate import sdk_install_gate


@pytest.mark.parametrize("error", [ConnectionError("commit acknowledgement lost"), OSError("write failed")])
async def test_sdk_gate_does_not_reclassify_operation_oserror(
    tmp_path: Path, error: OSError,
) -> None:
    manager = CapabilityEnvironmentManager(tmp_path / "sdk")
    tenant_id = uuid4()
    with pytest.raises(type(error)) as raised:
        async with sdk_install_gate(manager, tenant_id, "fixture-plugin"):
            raise error
    assert raised.value is error
    async with sdk_install_gate(manager, tenant_id, "fixture-plugin"):
        pass
