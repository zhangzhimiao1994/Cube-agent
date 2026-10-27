"""Private marker producer contracts; standalone native Root target, no build/model."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

from agent_hub.skills.sandbox import project
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project_contract_broker import _stamp
from agent_hub.skills.sandbox.python_project_product_export import (
    open_python_product,
    product_descriptor,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)


class MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(prefix="execution-marker-")
        self.addCleanup(self.folder.cleanup)
        self.stage = Path(self.folder.name)
        self.stage.chmod(0o700)
        self.invocation = project.ProjectInvocation(
            execution_id="metadata_contract", tenant_id=UUID(int=1), run_id=UUID(int=2),
            project_id="demo", session_id="session-a", sandbox_profile="restricted",
            timeout_seconds=60,
        )
        self.job = project._Execution(self.invocation, None, stage=self.stage)
        self.broker = project.ProjectExecutionBroker(BrokerPolicy())

    def write(self, state: str = "RUNNING") -> None:
        self.broker._metadata(self.job, state, "c" * 64)

    def test_node_marker_fields_and_unknown_transition_are_unchanged(self) -> None:
        self.write("STAGED")
        self.write("RUNNING")
        self.broker._metadata(self.job, "UNKNOWN")
        self.assertEqual(json.loads((self.stage / "execution.json").read_bytes()), {
            "state": "UNKNOWN", "unit": "agent-hub-project-metadata_contract.service",
            "execution_id": self.invocation.execution_id, "tenant_id": str(UUID(int=1)),
            "run_id": str(UUID(int=2)), "project_id": "demo", "session_id": "session-a",
            "source_sha256": "c" * 64,
        })
        self.assertEqual(list(self.stage.iterdir()), [self.stage / "execution.json"])

    def test_writer_uses_exclusive_private_temporary_inode(self) -> None:
        created: list[tuple[int, str]] = []
        real = tempfile.mkstemp
        def create(*args: Any, **kwargs: Any) -> tuple[int, str]:
            fd, name = real(*args, **kwargs)
            created.append((fd, name))
            self.assertEqual(Path(name).parent, self.stage)
            return fd, name
        with patch.object(tempfile, "mkstemp", side_effect=create):
            self.write()
        self.assertEqual(len(created), 1, "producer must create an exclusive private inode")
        fd, name = created[0]
        self.assertFalse(Path(name).exists())
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_existing_fixed_temporary_file_is_not_consumed_or_modified(self) -> None:
        foreign = self.stage / "execution.json.tmp"
        foreign.write_bytes(b"existing foreign temporary")
        self.write()
        self.assertTrue(foreign.is_file(), "an unrelated temporary must remain untouched")
        self.assertEqual(foreign.read_bytes(), b"existing foreign temporary")
        self.assertEqual(json.loads((self.stage / "execution.json").read_bytes())["state"], "RUNNING")

    def test_existing_fixed_temporary_symlink_is_not_followed(self) -> None:
        victim = self.stage / "victim"
        victim.write_bytes(b"do not overwrite")
        foreign = self.stage / "execution.json.tmp"
        try:
            foreign.symlink_to(victim)
        except OSError:
            self.skipTest("host does not permit symlink fixture")
        self.write()
        self.assertEqual(victim.read_bytes(), b"do not overwrite")
        self.assertTrue(foreign.is_symlink())
        self.assertFalse((self.stage / "execution.json").is_symlink())

    def test_atomic_replace_does_not_write_through_destination_symlink(self) -> None:
        victim = self.stage / "victim"
        victim.write_bytes(b"outside existing marker")
        try:
            (self.stage / "execution.json").symlink_to(victim)
        except OSError:
            self.skipTest("host does not permit symlink fixture")
        self.write("UNKNOWN")
        self.assertEqual(victim.read_bytes(), b"outside existing marker")
        self.assertFalse((self.stage / "execution.json").is_symlink())

    def test_atomic_replace_does_not_mutate_destination_hardlink(self) -> None:
        victim = self.stage / "victim"
        victim.write_bytes(b"original linked marker")
        os.link(victim, self.stage / "execution.json")
        self.write("UNKNOWN")
        self.assertEqual(victim.read_bytes(), b"original linked marker")
        self.assertEqual((self.stage / "execution.json").stat().st_nlink, 1)

    def test_fsync_failure_preserves_unknown_and_removes_only_own_temporary(self) -> None:
        self.write("UNKNOWN")
        marker = self.stage / "execution.json"
        before = marker.read_bytes()
        foreign = self.stage / "unrelated.tmp"
        foreign.write_bytes(b"unrelated")
        with (
            patch.object(os, "fsync", side_effect=OSError("injected flush failure")),
            self.assertRaises(OSError),
        ):
            self.write("RUNNING")
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(foreign.read_bytes(), b"unrelated")
        self.assertEqual({path.name for path in self.stage.iterdir()}, {"execution.json", "unrelated.tmp"})

    def test_replace_failure_preserves_unknown_and_cleans_own_temporary(self) -> None:
        self.write("UNKNOWN")
        marker = self.stage / "execution.json"
        before = marker.read_bytes()
        with (
            patch.object(os, "replace", side_effect=OSError("injected rename failure")),
            self.assertRaises(OSError),
        ):
            self.write("RUNNING")
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(list(self.stage.iterdir()), [marker])

    def test_full_json_is_flushed_before_atomic_publication(self) -> None:
        self.write("UNKNOWN")
        marker = self.stage / "execution.json"
        before = marker.read_bytes()
        real_replace, real_fsync = os.replace, os.fsync
        calls: list[str] = []
        def flushed(fd: int) -> None:
            calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)
        def replaced(source: Any, target: Any) -> None:
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual(json.loads(Path(source).read_bytes())["state"], "RUNNING")
            self.assertEqual(calls, ["file"])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            calls.append("replace")
            real_replace(source, target)
        with patch.object(os, "replace", side_effect=replaced), \
                patch.object(os, "fsync", side_effect=flushed):
            self.write()
        self.assertEqual(calls, ["file", "replace"] + (["directory"] if os.name == "posix" else []))

    @unittest.skipUnless(sys.platform == "linux", "native Linux permissions required")
    def test_linux_real_producer_mode_is_0600_under_all_umasks(self) -> None:
        for mask in (0o022, 0o000, 0o077, 0o777):
            with self.subTest(mask=oct(mask)):
                previous = os.umask(mask)
                try:
                    self.write()
                finally:
                    os.umask(previous)
                info = (self.stage / "execution.json").stat()
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                if sys.platform == "linux":
                    self.assertEqual(info.st_uid, os.geteuid())
                self.assertEqual(info.st_nlink, 1)

    def product_fixture(self) -> tuple[PythonProjectInvocation, PythonProjectBrokerResponse, bytes]:
        invocation = PythonProjectInvocation(
            **self.invocation.model_dump(), source_kind="original",
            original_reference=project.OriginalProjectReference(
                schema_version=1, contract_sha256="f" * 64,
                input_source_sha256="c" * 64, file_count=1),
            recipe_sha256="a" * 64, environment_input_sha256="b" * 64,
        )
        raw = b"opaque measured product fixture" * 10000
        report = {
            "schema_version": 2, "kind": "python_project", "evidence": "native_process_only",
            "status": "passed", "execution_id": invocation.execution_id,
            "source_sha256": "c" * 64, "recipe_sha256": "a" * 64,
            "checks": [{"phase": phase, "status": "passed", "exit_code": 0, "timed_out": False}
                       for phase in ("install", "build", "test")],
            "wheel": {"filename": "fixture-1.0-py3-none-any.whl",
                      "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)},
        }
        descriptor = product_descriptor(invocation, report)
        product = self.stage / "product"
        product.mkdir(mode=0o700)
        wheel = product / descriptor.filename
        wheel.write_bytes(raw)
        wheel.chmod(0o444)
        product.chmod(0o555)
        control = self.stage / "product.json"
        control.write_bytes(json.dumps({
            "descriptor": descriptor.model_dump(), "deadline": time.monotonic() + 60,
            "invocation_sha256": hashlib.sha256(invocation.model_dump_json().encode()).hexdigest(),
            "file_identity": list(_stamp(wheel.stat(), full=True)),
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii"))
        control.chmod(0o400)
        return invocation, PythonProjectBrokerResponse(
            action="run_python_project", execution_id=invocation.execution_id, ok=True,
            result=SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(report), stderr=""),
        ), raw

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "native Linux Root required")
    def test_root_real_marker_under_0022_opens_product_without_reader_relaxation(self) -> None:
        invocation, response, raw = self.product_fixture()
        previous = os.umask(0o022)
        try:
            self.write()
        finally:
            os.umask(previous)
        # On old producer: actual reader rejects the actual 0644 marker here.
        with open_python_product(self.stage, response, invocation) as opened:
            self.assertIsNotNone(opened)
            assert opened is not None
            fd, descriptor = opened
            self.assertEqual(os.read(fd, len(raw) + 1), raw)
            self.assertEqual(descriptor.size, len(raw))
        self.assertEqual(stat.S_IMODE((self.stage / "execution.json").stat().st_mode), 0o600)
        with self.assertRaises(OSError):
            os.fstat(fd)

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "native Linux Root required")
    def test_root_public_marker_is_still_rejected_by_unchanged_reader(self) -> None:
        invocation, response, _ = self.product_fixture()
        self.write()
        (self.stage / "execution.json").chmod(0o644)
        with (
            self.assertRaisesRegex(ValueError, "python_project_product_rejected"),
            open_python_product(self.stage, response, invocation),
        ):
            pass

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "native Linux Root required")
    def test_root_unknown_marker_still_rejects_product(self) -> None:
        invocation, response, _ = self.product_fixture()
        previous = os.umask(0o022)
        try:
            self.write("UNKNOWN")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE((self.stage / "execution.json").stat().st_mode), 0o600)
        with (
            self.assertRaisesRegex(ValueError, "python_project_product_rejected"),
            open_python_product(self.stage, response, invocation),
        ):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
