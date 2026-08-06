from __future__ import annotations

import hashlib
import io
import stat
import zipfile

import pytest

from agent_hub.skills.package import InvalidSkillPackage, SkillPackageInspector


def test_valid_skill_package_is_inspected() -> None:
    archive = skill_zip(
        tools=("calculator.evaluate",),
        network_hosts=("api.example.com",),
        writable_paths=("tmp/output",),
        env_secret_refs=("deepseek_api_key",),
        requirements="pydantic==2.10.0\n",
    )

    inspection = SkillPackageInspector().inspect(archive)

    assert inspection.manifest.name == "demo_skill"
    assert inspection.manifest.version == "1.0.0"
    assert inspection.manifest.entry_point == "main.py"
    assert inspection.dependency_lock_hash == hashlib.sha256(b"pydantic==2.10.0\n").hexdigest()
    assert inspection.requested_capabilities == (
        "tool:calculator.evaluate",
        "network:api.example.com",
        "write:tmp/output",
        "secret:deepseek_api_key",
    )


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "C:/escape"])
def test_zip_path_traversal_is_rejected(name: str) -> None:
    with pytest.raises(InvalidSkillPackage):
        SkillPackageInspector().inspect(zip_with_members({name: b"x"}))


def test_duplicate_paths_are_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.yaml", valid_manifest())
        archive.writestr("main.py", b"print('a')\n")
        archive.writestr("main.py", b"print('b')\n")

    with pytest.raises(InvalidSkillPackage, match="duplicate"):
        SkillPackageInspector().inspect(buffer.getvalue())


def test_symlink_entries_are_rejected() -> None:
    link_info = zipfile.ZipInfo("link")
    link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(InvalidSkillPackage, match="links"):
        SkillPackageInspector().inspect(zip_with_infos({link_info: b"target"}))


def test_directory_entry_cannot_satisfy_entry_point() -> None:
    directory = zipfile.ZipInfo("main.py/")
    with pytest.raises(InvalidSkillPackage, match="entry point"):
        SkillPackageInspector().inspect(
            zip_with_infos(
                {
                    zipfile.ZipInfo("skill.yaml"): valid_manifest().encode(),
                    directory: b"",
                }
            )
        )


def test_nested_archives_are_rejected() -> None:
    with pytest.raises(InvalidSkillPackage, match="nested"):
        SkillPackageInspector().inspect(skill_zip(extra_files={"nested.zip": b"PK\x03\x04"}))


def test_forbidden_extensions_are_rejected() -> None:
    with pytest.raises(InvalidSkillPackage, match="forbidden"):
        SkillPackageInspector().inspect(skill_zip(extra_files={"native.so": b"x"}))


def test_zip_bomb_ratio_is_rejected() -> None:
    with pytest.raises(InvalidSkillPackage, match="compression ratio"):
        SkillPackageInspector(max_compression_ratio=1).inspect(
            skill_zip(extra_files={"large.txt": b"a" * 20_000})
        )


def test_archive_size_limit_is_rejected() -> None:
    archive = skill_zip()

    with pytest.raises(InvalidSkillPackage, match="size limit"):
        SkillPackageInspector(max_archive_bytes=len(archive) - 1).inspect(archive)


def test_uncompressed_size_limit_is_rejected() -> None:
    with pytest.raises(InvalidSkillPackage, match="uncompressed size"):
        SkillPackageInspector(max_uncompressed_bytes=1).inspect(skill_zip())


def test_excessive_file_count_is_rejected() -> None:
    with pytest.raises(InvalidSkillPackage, match="too many"):
        SkillPackageInspector(max_files=1).inspect(skill_zip())


@pytest.mark.parametrize(
    "requirement",
    [
        "requests>=2\n",
        "-e ./localpkg==1.0\n",
        "pkg @ https://example.com/pkg==1.0.tar.gz\n",
        "requests==2.*\n",
        "./localpkg==1.0\n",
        "git+https://example.com/repo==1.0\n",
    ],
)
def test_unpinned_dependencies_are_rejected(requirement: str) -> None:
    with pytest.raises(InvalidSkillPackage, match="pinned"):
        SkillPackageInspector().inspect(skill_zip(requirements=requirement))


def test_dependency_lock_hash_must_match() -> None:
    with pytest.raises(InvalidSkillPackage, match="lock hash"):
        SkillPackageInspector().inspect(skill_zip(requirements="requests==2.32.0\n", lock_hash="0" * 64))


def test_undeclared_executables_are_rejected() -> None:
    executable = zipfile.ZipInfo("helper.py")
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    with pytest.raises(InvalidSkillPackage, match="undeclared executables"):
        SkillPackageInspector().inspect(skill_zip(extra_infos={executable: b"print('helper')\n"}))


def zip_with_members(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def zip_with_infos(members: dict[zipfile.ZipInfo, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info, content in members.items():
            archive.writestr(info, content)
    return buffer.getvalue()


def skill_zip(
    *,
    tools: tuple[str, ...] = (),
    network_hosts: tuple[str, ...] = (),
    writable_paths: tuple[str, ...] = (),
    env_secret_refs: tuple[str, ...] = (),
    requirements: str = "",
    lock_hash: str | None = None,
    extra_files: dict[str, bytes] | None = None,
    extra_infos: dict[zipfile.ZipInfo, bytes] | None = None,
) -> bytes:
    dependency_hash = hashlib.sha256(requirements.encode()).hexdigest() if lock_hash is None else lock_hash
    manifest = valid_manifest(
        tools=tools,
        network_hosts=network_hosts,
        writable_paths=writable_paths,
        env_secret_refs=env_secret_refs,
        dependency_hash=dependency_hash,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill.yaml", manifest)
        archive.writestr("main.py", b"print('ok')\n")
        if requirements:
            archive.writestr("requirements.txt", requirements.encode())
        for name, content in (extra_files or {}).items():
            archive.writestr(name, content)
        for info, content in (extra_infos or {}).items():
            archive.writestr(info, content)
    return buffer.getvalue()


def valid_manifest(
    *,
    tools: tuple[str, ...] = (),
    network_hosts: tuple[str, ...] = (),
    writable_paths: tuple[str, ...] = (),
    env_secret_refs: tuple[str, ...] = (),
    dependency_hash: str | None = None,
) -> str:
    dependency_hash = dependency_hash or hashlib.sha256(b"").hexdigest()
    mode = "allowlist" if network_hosts else "none"
    hosts_yaml = "\n".join(f"    - {host}" for host in network_hosts)
    tools_yaml = "\n".join(f"  - {tool}" for tool in tools)
    writable_yaml = "\n".join(f"  - {path}" for path in writable_paths)
    secrets_yaml = "\n".join(f"  - {secret}" for secret in env_secret_refs)
    return f"""name: demo_skill
version: 1.0.0
entry_point: main.py
compatible_runtime: python3.12
declared_tools:
{tools_yaml or "  []"}
network_policy:
  mode: {mode}
  allow_hosts:
{hosts_yaml or "    []"}
writable_paths:
{writable_yaml or "  []"}
env_secret_refs:
{secrets_yaml or "  []"}
dependency_lock_hash: "{dependency_hash}"
"""
