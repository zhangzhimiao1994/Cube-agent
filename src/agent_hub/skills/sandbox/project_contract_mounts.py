"""Sealed, read-only candidate sources for the native Node bwrap boundary.

The caller supplies a ProjectCheckContract payload, its bound contract digest,
and the already-bound candidate source digest, and owns all
scope/recipe approvals. No contract parsing or editable-path authority lives
here. Verification never receives writable source, including editable app code.

Only caller-created node_modules/dist/build directories may be writable. Their
budget is preflight-checked here; the native artifact workspace's quota/tmpfs and
cgroup must enforce it during execution. A bind mount is NOT a disk quota.
Use build_contract_mounts as a context manager and pass plan.pass_fds to Popen;
keep the context open until bwrap exits. No project code is loaded or executed.
"""

import hashlib
import json
import os
import re
import runpy
import stat
import sys
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from time import monotonic
from typing import Protocol, cast

_ERROR = "project_contract_mounts_rejected"
_OUTPUTS = frozenset({"node_modules", "dist", "build"})
_MAX_FILES = 10000
_MAX_BYTES = 100 * 1024**2
_MAX_OUTPUT_BYTES = 2 * 1024**3
_SHA = re.compile(r"[a-f0-9]{64}")
_CLOSE_FDS_AND_EXEC = """import errno, os, sys
for value in sys.argv[1].split(','):
    try:
        os.close(int(value))
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
os.execv(sys.argv[2], sys.argv[2:])
"""


class ContractMountError(ValueError):
    """Fixed diagnostic; never a permission grant or successful check receipt."""


class _CheckContract(Protocol):
    protected_files: Mapping[str, str]
    protected_sha256: str
    input_source_sha256: str

    def require_current(self, source_files: Mapping[str, str]) -> None: ...


@cache
def _contract_loader() -> Callable[[object], _CheckContract]:
    # The broker's isolated stdlib runner has no importable app package. Load
    # the exact shared pure source, never a second copy of its validation rules.
    source = Path(__file__).resolve().parents[2] / "capabilities/project_contracts.py"
    namespace = runpy.run_path(str(source))
    return cast(Callable[[object], _CheckContract], namespace["ProjectCheckContract"].from_payload)


def _need(condition: object) -> None:
    if not condition:
        raise ContractMountError(_ERROR)


def _parts(value: str) -> tuple[str, ...]:
    _need(type(value) is str and 0 < len(value.encode("utf-8")) <= 4096)
    _need(not any(char in value for char in "\\:\x00")
          and not any(ord(char) < 32 or ord(char) == 127 for char in value))
    parts = tuple(value.split("/"))
    _need(len(parts) <= 64 and all(
        part not in {"", ".", ".."} and not part.endswith((" ", "."))
        and not PureWindowsPath(part).is_reserved()
        and unicodedata.normalize("NFC", part) == part for part in parts
    ))
    return parts


def _identity(info: os.stat_result, *, ancestor: bool = False) -> tuple[int, ...]:
    base = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return base if ancestor else (*base, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_info(info: os.stat_result, *, directory: bool, immutable: bool = False) -> None:
    _need(not stat.S_ISLNK(info.st_mode)
          and not getattr(info, "st_file_attributes", 0) & 0x400)
    _need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    if immutable and (os.name == "posix" or not directory):
        _need(not info.st_mode & 0o222)


@dataclass(frozen=True, slots=True)
class _File:
    path: str
    data: bytes = field(repr=False)
    executable: bool = False

    @property
    def sha256(self) -> str:
        return hashlib.sha256((b"x\0" if self.executable else b"f\0") + self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class ContractWork:
    source_sha256: str
    contract_sha256: str
    protected_paths: tuple[str, ...]
    readonly_paths: tuple[str, ...]
    ephemeral_dirs: tuple[str, ...]
    ephemeral_budget_bytes: int
    _files: tuple[_File, ...] = field(repr=False)
    _directories: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ContractMountPlan:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]
    source_sha256: str
    contract_sha256: str
    ephemeral_budget_bytes: int


@dataclass(frozen=True, slots=True)
class _Directory:
    relative: str
    fd: int | None
    info: os.stat_result
    parent_fd: int | None
    name: str
    ancestor: bool = False


class _Tree:
    def __init__(self, root: Path, stack: ExitStack, deadline: float, *, immutable: bool) -> None:
        _need(root.is_absolute() and ".." not in root.parts and root.resolve(strict=True) == root)
        self.root, self.stack, self.deadline, self.immutable = root, stack, deadline, immutable
        self.native = sys.platform == "linux" and os.open in os.supports_dir_fd
        self.directories: dict[str, _Directory] = {}
        self.ancestors: list[_Directory] = []
        self.entries: list[tuple[str, os.stat_result]] = []
        self.names: dict[str, tuple[str, ...]] = {}
        self.bytes = 0
        self.visited = 0
        parent: int | None = None
        for index in range(len(root.parts)):
            path = Path(*root.parts[:index + 1])
            before = path.lstat()
            final = index == len(root.parts) - 1
            _safe_info(before, directory=True, immutable=immutable and final)
            fd: int | None = None
            if self.native:
                name = root.anchor if index == 0 else root.parts[index]
                fd = os.open(name, _directory_flags(),
                             dir_fd=parent)
                stack.callback(os.close, fd)
                _need(_identity(os.fstat(fd), ancestor=not final) == _identity(before, ancestor=not final))
            directory = _Directory("", fd, before, parent, str(path) if parent is None else path.name, not final)
            self.ancestors.append(directory)
            parent = fd
        self.directories[""] = self.ancestors[-1]

    def check(self) -> None:
        _need(monotonic() < self.deadline)

    def info(self, relative: str) -> os.stat_result:
        parent, _, name = relative.rpartition("/")
        descriptor = self.directories[parent].fd
        return (os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if self.native else (self.root / relative).lstat())

    def directory(self, relative: str, before: os.stat_result) -> _Directory:
        _safe_info(before, directory=True, immutable=self.immutable)
        parent, _, name = relative.rpartition("/")
        parent_fd = self.directories[parent].fd
        fd: int | None = None
        if self.native:
            fd = os.open(name, _directory_flags(),
                         dir_fd=parent_fd)
            self.stack.callback(os.close, fd)
            _need(_identity(os.fstat(fd)) == _identity(before))
        result = _Directory(relative, fd, before, parent_fd, name)
        self.directories[relative] = result
        return result

    def listing(self, relative: str) -> tuple[str, ...]:
        directory = self.directories[relative]
        names: list[str] = []
        with os.scandir(directory.fd if directory.fd is not None else self.root / relative) as entries:
            for entry in entries:
                self.check()
                _parts(entry.name)
                _need(len(names) < _MAX_FILES * 10)
                names.append(entry.name)
        _need(len({name.casefold() for name in names}) == len(names))
        return tuple(sorted(names))

    def read(self, relative: str, before: os.stat_result) -> _File:
        self.check()
        _safe_info(before, directory=False, immutable=self.immutable)
        _need(before.st_size <= _MAX_BYTES - self.bytes)
        parent, _, name = relative.rpartition("/")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        fd = (os.open(name, flags, dir_fd=self.directories[parent].fd)
              if self.native else os.open(self.root / relative, flags))
        try:
            opened = os.fstat(fd)
            _safe_info(opened, directory=False, immutable=self.immutable)
            if self.native:
                _need(_identity(opened) == _identity(before))
            else:
                # Windows validation is structural only; native execution has
                # no path-based fallback and requires Linux FD-relative capture.
                _need((opened.st_size, opened.st_mtime_ns) == (before.st_size, before.st_mtime_ns))
            chunks: list[bytes] = []
            count = 0
            while True:
                self.check()
                chunk = os.read(fd, min(65536, before.st_size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                _need(count <= before.st_size)
                chunks.append(chunk)
            _need(count == before.st_size and _identity(os.fstat(fd)) == _identity(opened)
                  and _identity(self.info(relative)) == _identity(before))
            self.bytes += count
        finally:
            os.close(fd)
        return _File(relative, b"".join(chunks), bool(before.st_mode & 0o111))

    def scan(self, *, outputs: frozenset[str] = frozenset(), output_limit: int = 0,
             output_files: int = 0) -> dict[str, _File]:
        files: dict[str, _File] = {}
        output_bytes = output_count = 0

        def walk(relative: str, *, derived: bool = False) -> None:
            nonlocal output_bytes, output_count
            self.names[relative] = names = self.listing(relative)
            for name in names:
                self.check()
                self.visited += 1
                _need(self.visited <= _MAX_FILES * 10)
                member = relative + "/" + name if relative else name
                _parts(member)
                before = self.info(member)
                self.entries.append((member, before))
                is_output = derived or member in outputs
                if is_output:
                    output_count += 1
                    _need(output_count <= output_files)
                if stat.S_ISLNK(before.st_mode) and derived:
                    # npm .bin links are derived, never source authority. They
                    # may resolve only inside the same explicitly owned output.
                    target = (self.root / member).resolve(strict=True)
                    _need(target.is_relative_to(self.root / member.split("/", 1)[0]))
                elif stat.S_ISDIR(before.st_mode):
                    self.directory(member, before)
                    walk(member, derived=is_output)
                else:
                    _safe_info(before, directory=False, immutable=self.immutable)
                    if is_output:
                        output_bytes += before.st_size
                        _need(output_bytes <= output_limit)
                    else:
                        _need(len(files) < _MAX_FILES)
                        files[member] = self.read(member, before)

        walk("")
        return files

    def unchanged(self) -> None:
        for relative, before in self.entries:
            self.check()
            _need(_identity(self.info(relative)) == _identity(before))
        for relative, before_names in self.names.items():
            _need(self.listing(relative) == before_names)
        for directory in (*self.ancestors, *self.directories.values()):
            self.check()
            if self.native:
                if directory.fd is None:
                    raise ContractMountError(_ERROR)
                current = os.stat(directory.name, dir_fd=directory.parent_fd, follow_symlinks=False)
                _need(_identity(os.fstat(directory.fd), ancestor=directory.ancestor)
                      == _identity(directory.info, ancestor=directory.ancestor)
                      == _identity(current, ancestor=directory.ancestor))
            else:
                path = self.root / directory.relative if directory.relative else self.root
                if not directory.ancestor:
                    _need(_identity(path.lstat()) == _identity(directory.info))


def _directory_flags() -> int:
    flags = os.O_RDONLY
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        flags |= cast(int, getattr(os, name))
    return flags


def _source_digest(files: dict[str, _File]) -> str:
    manifest = {path: item.sha256 for path, item in sorted(files.items())}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode("ascii")).hexdigest()


def _capture(stack: ExitStack, *, contract_payload: object, expected_contract_sha256: str,
             original_root: Path, work_root: Path, expected_source_sha256: str, ephemeral_dirs: Sequence[str],
             ephemeral_budget_bytes: int, ephemeral_file_limit: int) -> tuple[ContractWork, _Tree]:
    contract = _contract_loader()(contract_payload)
    _need(type(expected_contract_sha256) is str and contract.protected_sha256 == expected_contract_sha256)
    protected = contract.protected_files
    _need(type(expected_source_sha256) is str and _SHA.fullmatch(expected_source_sha256) is not None)
    _need(type(ephemeral_dirs) in (tuple, list) and len(set(ephemeral_dirs)) == len(ephemeral_dirs))
    outputs = tuple(sorted(ephemeral_dirs))
    _need(all(type(path) is str and path in _OUTPUTS for path in outputs))
    _need(type(ephemeral_budget_bytes) is int and 0 <= ephemeral_budget_bytes <= _MAX_OUTPUT_BYTES)
    _need(type(ephemeral_file_limit) is int and 0 < ephemeral_file_limit <= 100000)
    _need(not outputs or ephemeral_budget_bytes > 0)
    _need(isinstance(original_root, Path) and isinstance(work_root, Path))
    _need(original_root != work_root and not original_root.is_relative_to(work_root)
          and not work_root.is_relative_to(original_root))
    deadline = monotonic() + 10
    original = _Tree(original_root, stack, deadline, immutable=True)
    original_files = original.scan()
    _need(_source_digest(original_files) == contract.input_source_sha256)
    contract.require_current({path: item.sha256 for path, item in original_files.items()})
    _need(not any(path in original.directories or path in original_files for path in outputs))
    work = _Tree(work_root, stack, deadline, immutable=False)
    files = work.scan(outputs=frozenset(outputs), output_limit=ephemeral_budget_bytes,
                      output_files=ephemeral_file_limit)
    _need(all(path in work.directories for path in outputs))
    source_parents = {str(parent) for path in files for parent in PurePosixPath(path).parents}
    _need(all(path in original.directories or path in source_parents
              or path.split("/", 1)[0] in outputs for path in work.directories))
    contract.require_current({path: item.sha256 for path, item in files.items()})
    actual = _source_digest(files)
    _need(actual == expected_source_sha256)
    original.unchanged()
    work.unchanged()
    directories = tuple(sorted((name for name in work.directories if name
                                and not any(name.startswith(output + "/") for output in outputs)),
                               key=lambda name: (name.count("/"), name)))
    return ContractWork(actual, contract.protected_sha256, tuple(sorted(protected)), tuple(sorted(files)), outputs,
                        ephemeral_budget_bytes, tuple(files[path] for path in sorted(files)), directories), work


def validate_contract_work(*, contract_payload: object, expected_contract_sha256: str, original_root: Path,
                           work_root: Path, expected_source_sha256: str,
                           ephemeral_dirs: Sequence[str] = (), ephemeral_budget_bytes: int = 0,
                           ephemeral_file_limit: int = 10000) -> ContractWork:
    """Read-only preflight. Expected digest covers all candidate source, not outputs.

    Both original and candidate manifests use the shared pure ProjectCheckContract
    validator. No editable-path or contract-schema policy is duplicated here.
    Windows supports structural validation only, never native execution fallback.
    """
    try:
        with ExitStack() as stack:
            view, _ = _capture(stack, contract_payload=contract_payload,
                expected_contract_sha256=expected_contract_sha256, original_root=original_root,
                work_root=work_root, expected_source_sha256=expected_source_sha256,
                ephemeral_dirs=ephemeral_dirs, ephemeral_budget_bytes=ephemeral_budget_bytes,
                ephemeral_file_limit=ephemeral_file_limit)
            return view
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError, AttributeError):
        raise ContractMountError(_ERROR) from None


_ARITY = {"--die-with-parent": 0, "--new-session": 0, "--cap-drop": 1,
          "--unshare-user": 0, "--unshare-pid": 0, "--unshare-ipc": 0, "--unshare-uts": 0,
          "--unshare-net": 0, "--ro-bind": 2, "--bind": 2, "--symlink": 2,
          "--proc": 1, "--dev": 1, "--tmpfs": 1, "--dir": 1, "--chdir": 1,
          "--clearenv": 0, "--setenv": 2}


def _command_prefix(argv: Sequence[str], work_root: Path) -> tuple[list[str], tuple[str, ...]]:
    _need(type(argv) in (list, tuple) and len(argv) > 2
          and all(type(item) is str and "\x00" not in item for item in argv))
    _need(argv[0] == "/usr/bin/bwrap")
    prefix, index, workspace, masks = [argv[0]], 1, 0, 0
    while index < len(argv) and argv[index] != "--":
        option = argv[index]
        _need(option in _ARITY)
        count = _ARITY[option]
        values = list(argv[index + 1:index + count + 1])
        _need(len(values) == count)
        if option in {"--bind", "--ro-bind", "--symlink", "--proc", "--dev", "--tmpfs", "--dir"}:
            destination = values[-1]
            _need(destination.startswith("/") and str(PurePosixPath(destination)) == destination
                  and ".." not in PurePosixPath(destination).parts)
            if destination == "/workspace":
                _need(option == "--bind" and values[0] == str(work_root))
                workspace += 1
                index += count + 1
                continue
            if destination == "/workspace/.npmrc":
                _need(option == "--ro-bind" and values[0] == "/dev/null")
                masks += 1
                _need(masks == 1)
            else:
                _need(destination != "/" and not destination.startswith("/workspace/"))
            _need(option != "--bind")
        prefix.extend((option, *values))
        index += count + 1
    _need(workspace == 1 and index < len(argv) - 1 and argv[index] == "--")
    return prefix, tuple(argv[index:])


def _contract_argv(argv: Sequence[str], work_root: Path, mounts: Sequence[str]) -> tuple[str, ...]:
    prefix, suffix = _command_prefix(argv, work_root)
    result, mask, index = [prefix[0]], [], 1
    while index < len(prefix):
        count = _ARITY[prefix[index]] + 1
        option = prefix[index:index + count]
        if option == ["--ro-bind", "/dev/null", "/workspace/.npmrc"]:
            mask.extend(option)
        else:
            result.extend(option)
        index += count
    # A mask before --tmpfs /workspace would be hidden by the reconstructed tree.
    return (*result, *mounts, *mask, *suffix)


def _seal(file: _File, stack: ExitStack) -> int:
    if sys.platform != "linux":
        raise ContractMountError(_ERROR)
    import fcntl

    fd = os.memfd_create("project-contract-source", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    stack.callback(os.close, fd)
    remaining = memoryview(file.data)
    while remaining:
        count = os.write(fd, remaining)
        _need(count > 0)
        remaining = remaining[count:]
    fcntl.fcntl(fd, 1033, 15)
    _need(fcntl.fcntl(fd, 1034) & 15 == 15 and not os.get_inheritable(fd))
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _mount_arguments(view: ContractWork, descriptors: tuple[int, ...],
                     output_sources: Mapping[str, str]) -> tuple[str, ...]:
    _need(len(descriptors) == len(view._files) and len(set(descriptors)) == len(descriptors)
          and all(type(fd) is int and fd >= 0 for fd in descriptors)
          and set(output_sources) == set(view.ephemeral_dirs))
    mounts = ["--tmpfs", "/workspace"]
    for relative in view._directories:
        mounts.extend(("--dir", "/workspace/" + relative))
    for file, fd in zip(view._files, descriptors, strict=True):
        mounts.extend(("--perms", "0555" if file.executable else "0444",
                       "--ro-bind-data", str(fd), "/workspace/" + file.path))
    mounts.extend(("--remount-ro", "/workspace"))
    for output in view.ephemeral_dirs:
        mounts.extend(("--bind", output_sources[output], "/workspace/" + output))
    return tuple(mounts)


@contextmanager
def build_contract_mounts(argv: Sequence[str], *, contract_payload: object, expected_contract_sha256: str,
                         original_root: Path, work_root: Path, expected_source_sha256: str,
                         ephemeral_dirs: Sequence[str] = (), ephemeral_budget_bytes: int = 0,
                         ephemeral_file_limit: int = 10000) -> Iterator[ContractMountPlan]:
    """Seal all candidate files; expose only explicitly owned derived directories rw.

    The original broad workspace bind is replaced by a sealed tmpfs tree,
    remounted readonly. Output roots are pinned with Linux directory FDs held by
    this context and passed to trusted bwrap. A trusted isolated launcher closes
    them before project execution. Caller must pass only pass_fds,
    enforce the native workspace quota, and reap bwrap before leaving the context.
    Conflicting workspace submounts fail closed; only the existing exact .npmrc
    /dev/null mask is preserved after the reconstructed read-only source tree.
    """
    with ExitStack() as stack:
        try:
            _need(sys.platform == "linux" and os.open in os.supports_dir_fd
                  and hasattr(os, "memfd_create"))
            _, executable = _command_prefix(argv, work_root)
            view, work = _capture(stack, contract_payload=contract_payload,
                expected_contract_sha256=expected_contract_sha256, original_root=original_root,
                work_root=work_root, expected_source_sha256=expected_source_sha256,
                ephemeral_dirs=ephemeral_dirs, ephemeral_budget_bytes=ephemeral_budget_bytes,
                ephemeral_file_limit=ephemeral_file_limit)
            descriptors = tuple(_seal(file, stack) for file in view._files)
            output_sources: dict[str, str] = {}
            output_descriptors: list[int] = []
            for output in view.ephemeral_dirs:
                directory = work.directories[output]
                _need(directory.fd is not None)
                assert directory.fd is not None
                output_descriptors.append(directory.fd)
                # Cross-user-namespace access to the parent's proc FD is denied.
                output_sources[output] = f"/proc/self/fd/{directory.fd}"
            command = _contract_argv(argv, work_root, _mount_arguments(view, descriptors, output_sources))
            inherited = (*descriptors, *output_descriptors)
            if inherited:
                # bwrap consumes data FDs but does not close ordinary bind FDs.
                command = (*command[:-len(executable)], "--", "/usr/bin/python3", "-I", "-S", "-B",
                           "-c", _CLOSE_FDS_AND_EXEC, ",".join(map(str, inherited)), *executable[1:])
            work.unchanged()
            plan = ContractMountPlan(command, inherited,
                                     view.source_sha256, view.contract_sha256, view.ephemeral_budget_bytes)
        except (OSError, ValueError, TypeError, RuntimeError, ImportError, RecursionError, AttributeError):
            raise ContractMountError(_ERROR) from None
        yield plan
