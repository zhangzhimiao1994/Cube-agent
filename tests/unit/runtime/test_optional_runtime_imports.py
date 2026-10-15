"""API imports must not initialize an unused optional discussion SDK."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_api_import_does_not_import_legacy_discussion_sdk() -> None:
    source = Path(__file__).resolve().parents[3] / "src"
    program = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(source)!r})
class BlockUnusedSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'autogen_agentchat', 'autogen_core'}}:
            raise RuntimeError('unused_discussion_sdk_imported')
sys.meta_path.insert(0, BlockUnusedSDK())
import agent_hub.app
print('API_IMPORT_WITHOUT_DISCUSSION_SDK')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "API_IMPORT_WITHOUT_DISCUSSION_SDK"
