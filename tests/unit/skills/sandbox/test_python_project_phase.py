from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_TEST_INTERPRETER = sys.executable


def test_phase_script_can_be_loaded_with_only_stdlib_dependencies():
    path = (Path(__file__).parents[4] / 'src/agent_hub/skills/sandbox'
            / 'python_project_phase.py')
    assert path.is_file(), 'isolated Python phase script is missing'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split('.')[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module.split('.')[0] in sys.stdlib_module_names


@pytest.fixture
def phase_module():
    path = (Path(__file__).parents[4] / 'src/agent_hub/skills/sandbox'
            / 'python_project_phase.py')
    assert path.is_file(), 'isolated Python phase script is missing'
    spec = importlib.util.spec_from_file_location('isolated_phase_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated(phase_module, tmp_path, monkeypatch):
    module = phase_module
    for name in ('RUNTIME', 'ENVIRONMENT', 'WHEELHOUSE', 'WORKSPACE', 'OUTPUT'):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setattr(module, '_' + name, path)
    (module._WORKSPACE / 'requirements.lock').write_text('# captured lock\n')
    (module._WORKSPACE / 'tests').mkdir()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setenv('AGENT_HUB_PYTHON_PROJECT_ISOLATED', '1')
    monkeypatch.setattr(module.os, 'getuid', lambda: module._OUTPUT.stat().st_uid,
                        raising=False)
    monkeypatch.setattr(module.sys, 'executable', str(module._ENVIRONMENT / 'bin/python'))
    monkeypatch.setattr(module.sys, 'prefix', str(module._ENVIRONMENT))
    monkeypatch.setattr(module.subprocess, 'run', Mock())
    if os.name == 'nt':
        # Windows stat/fstat expose inconsistent creation times; these are not
        # Linux ctime. Keep this adaptation in the mock fixture, not production.
        stamp = module._stamp
        monkeypatch.setattr(module, '_stamp', lambda info: stamp(info)[:-1])
    return module


@pytest.mark.parametrize(('platform', 'marker'), [
    ('win32', '1'), ('darwin', '1'), ('linux', None), ('linux', 'true'), ('linux', '0'),
])
def test_unisolated_entry_has_no_filesystem_import_or_process_side_effects(
    phase_module, monkeypatch, platform, marker,
):
    module = phase_module
    monkeypatch.setattr(module.sys, 'platform', platform)
    if marker is None:
        monkeypatch.delenv('AGENT_HUB_PYTHON_PROJECT_ISOLATED', raising=False)
    else:
        monkeypatch.setenv('AGENT_HUB_PYTHON_PROJECT_ISOLATED', marker)
    forbidden = Mock(side_effect=AssertionError('unisolated side effect'))
    monkeypatch.setattr(module.Path, 'lstat', forbidden)
    monkeypatch.setattr(module.importlib, 'import_module', forbidden)
    monkeypatch.setattr(module.subprocess, 'run', forbidden)
    with pytest.raises(module.PhaseError):
        module.run_phase('install', import_module='sample')
    forbidden.assert_not_called()
    assert os.environ.get('AGENT_HUB_PYTHON_PROJECT_ISOLATED') == marker


@pytest.mark.parametrize('name', ['', '../sample', 'a/b', 'a..b', 'a;id', 'a-b',
                                  'https://example.invalid', 'class', 'a.else', 'a\n'])
def test_module_selection_rejects_paths_and_non_identifiers(isolated, name):
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('install', import_module=name)
    isolated.subprocess.run.assert_not_called()


def test_phase_and_api_cannot_select_commands(isolated):
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('shell', import_module='sample')
    with pytest.raises(TypeError):
        isolated.run_phase('install', import_module='sample', cwd='/other')
    isolated.subprocess.run.assert_not_called()


def test_mount_roots_cannot_overlap(isolated, monkeypatch):
    nested = isolated._WORKSPACE / 'nested-output'
    nested.mkdir()
    monkeypatch.setattr(isolated, '_OUTPUT', nested)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('install', import_module='sample')
    isolated.subprocess.run.assert_not_called()


def test_install_bootstraps_then_installs_hash_locked_binary_closure(isolated):
    module = isolated
    result = module.run_phase('install', import_module='sample.api')
    commands = [call.args[0] for call in module.subprocess.run.call_args_list]
    python = str(module._ENVIRONMENT / 'bin/python')
    assert commands == [
        [str(module._RUNTIME / 'bin/python3.12'), '-I', '-B', '-m', 'venv',
         '--copies', str(module._ENVIRONMENT)],
        [python, '-I', '-B', '-m', 'pip', '--isolated', 'install', '--no-index',
         f'--find-links={module._WHEELHOUSE}', '--require-hashes', '--only-binary=:all:',
         '--no-deps', '--no-cache-dir', '--disable-pip-version-check', '--no-compile',
         '-r', str(module._WORKSPACE / 'requirements.lock')],
        [python, '-I', '-B', '-m', 'pip', '--isolated', 'check'],
    ]
    for call in module.subprocess.run.call_args_list:
        assert call.kwargs['check'] is True
        assert call.kwargs['stdin'] == subprocess.DEVNULL
        assert call.kwargs.get('shell', False) is False
        assert not {'env', 'capture_output', 'timeout', 'start_new_session'} & call.kwargs.keys()
    assert result['evidence'] == 'native_process_only'
    assert result['phase'] == 'install'


def test_reused_environment_is_checked_without_venv_or_install(isolated):
    result = isolated.run_phase('check_environment', import_module='sample.api')
    assert result == {'phase': 'check_environment', 'evidence': 'native_process_only',
        'requires_dist_check': 'completed'}
    assert [call.args[0] for call in isolated.subprocess.run.call_args_list] == [
        [str(isolated._ENVIRONMENT / 'bin/python'), '-I', '-B', '-m', 'pip', '--isolated', 'check']]


def test_reused_environment_cannot_check_with_an_unrelated_interpreter(isolated, monkeypatch):
    monkeypatch.setattr(isolated.sys, 'prefix', '/unrelated')
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('check_environment', import_module='sample')
    isolated.subprocess.run.assert_not_called()


@pytest.mark.parametrize('target', ['environment', 'output'])
def test_dirty_owned_directories_are_not_reused(isolated, target):
    module = isolated
    root = getattr(module, '_' + target.upper())
    (root / 'stale').write_text('do not delete')
    with pytest.raises(module.PhaseError):
        module.run_phase('install' if target == 'environment' else 'build', import_module='sample')
    assert (root / 'stale').read_text() == 'do not delete'
    module.subprocess.run.assert_not_called()


def test_mount_alias_is_rejected_before_execution(isolated, monkeypatch):
    monkeypatch.setattr(isolated, '_ENVIRONMENT', isolated._OUTPUT)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('install', import_module='sample')
    isolated.subprocess.run.assert_not_called()


def test_owned_environment_is_required(isolated, monkeypatch):
    monkeypatch.setattr(isolated.os, 'getuid', lambda: -1)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('install', import_module='sample')
    isolated.subprocess.run.assert_not_called()


def test_installer_failure_stops_phase_without_fallback(isolated):
    isolated.subprocess.run.side_effect = subprocess.CalledProcessError(1, ['private/path'])
    with pytest.raises(module_error := isolated.PhaseError) as caught:
        isolated.run_phase('install', import_module='sample')
    assert isinstance(caught.value, module_error)
    assert 'private/path' not in str(caught.value)
    assert isolated.subprocess.run.call_count == 1


def test_dependency_closure_failure_is_not_install_success(isolated):
    isolated.subprocess.run.side_effect = [None, None, subprocess.CalledProcessError(1, ['pip'])]
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('install', import_module='sample')
    assert isolated.subprocess.run.call_count == 3


def fake_backend(module, monkeypatch, producer=None):
    def build_wheel(directory):
        assert Path.cwd() == module._OUTPUT / 'source'
        assert directory == str(module._OUTPUT / 'wheels')
        if producer:
            return producer(Path(directory))
        (Path(directory) / 'sample-1-py3-none-any.whl').write_bytes(b'actual wheel')
        return 'sample-1-py3-none-any.whl'

    backend = SimpleNamespace(build_wheel=build_wheel)
    loader = Mock(return_value=backend)
    monkeypatch.setattr(module.importlib, 'import_module', loader)
    return loader


def test_build_copies_snapshot_and_installs_only_validated_wheel(isolated, monkeypatch):
    module = isolated
    (module._WORKSPACE / 'sample.py').write_text('not executed')
    loader = fake_backend(module, monkeypatch)
    previous = Path.cwd()
    result = module.run_phase('build', import_module='sample')
    assert Path.cwd() == previous
    assert (module._OUTPUT / 'source/sample.py').read_text() == 'not executed'
    loader.assert_called_once_with('setuptools.build_meta')
    wheel = module._OUTPUT / 'wheels/sample-1-py3-none-any.whl'
    assert result['wheel'] == {'filename': wheel.name, 'sha256': hashlib.sha256(
        b'actual wheel').hexdigest(), 'size': 12}
    assert module.subprocess.run.call_args.args[0] == [
        str(module._ENVIRONMENT / 'bin/python'), '-I', '-B', '-m', 'pip', '--isolated',
        'install', '--no-index', '--no-deps', '--no-cache-dir', '--no-compile',
        '--target', str(module._OUTPUT / 'project-site'), str(wheel),
    ]


@pytest.mark.parametrize('phase', ['build', 'test'])
def test_build_and_test_require_environment_interpreter(isolated, monkeypatch, phase):
    monkeypatch.setattr(isolated.sys, 'executable', '/runtime/bin/python3.12')
    loader = Mock(side_effect=AssertionError('backend must not import'))
    monkeypatch.setattr(isolated.importlib, 'import_module', loader)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase(phase, import_module='sample')
    loader.assert_not_called()


@pytest.mark.parametrize('returned', ['../escape.whl', '/escape.whl', 'x\\y.whl',
                                     'sample.tar.gz', '', None])
def test_backend_cannot_select_arbitrary_artifact(isolated, monkeypatch, returned):
    def produce(directory):
        (directory / 'sample-1-py3-none-any.whl').write_bytes(b'wheel')
        return returned
    fake_backend(isolated, monkeypatch, produce)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('build', import_module='sample')
    isolated.subprocess.run.assert_not_called()


@pytest.mark.parametrize('artifact', ['extra', 'directory', 'hardlink', 'symlink'])
def test_build_rejects_extra_or_aliased_wheel_outputs(isolated, monkeypatch, artifact):
    def produce(directory):
        wheel = directory / 'sample-1-py3-none-any.whl'
        if artifact == 'directory':
            wheel.mkdir()
        else:
            wheel.write_bytes(b'wheel')
            if artifact == 'extra':
                (directory / 'extra.txt').write_text('extra')
            elif artifact == 'hardlink':
                os.link(wheel, isolated._OUTPUT / 'alias')
            else:
                wheel.unlink()
                # Mock link metadata, since Windows symlink privileges vary.
                original = Path.lstat
                def lstat(path, *args, **kwargs):
                    if path == wheel:
                        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_nlink=1)
                    return original(path, *args, **kwargs)
                wheel.write_bytes(b'wheel')
                monkeypatch.setattr(Path, 'lstat', lstat)
        return wheel.name
    fake_backend(isolated, monkeypatch, produce)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('build', import_module='sample')
    isolated.subprocess.run.assert_not_called()


def test_source_symlink_is_rejected_instead_of_dereferenced(isolated, monkeypatch):
    source = isolated._WORKSPACE / 'link.py'
    source.write_text('private content')
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == source:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_nlink=1)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    loader = fake_backend(isolated, monkeypatch)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('build', import_module='sample')
    loader.assert_not_called()
    assert not (isolated._OUTPUT / 'source/link.py').exists()


def test_readonly_sealed_source_may_be_unlinked_memfd_but_wheel_outputs_may_not(isolated, monkeypatch):
    path = isolated._WORKSPACE / 'requirements.lock'
    real_lstat = Path.lstat
    def lstat(item, *args, **kwargs):
        if item == path:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_nlink=0)
        return real_lstat(item, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert isolated._regular(path, sealed_input=True).st_nlink == 0
    with pytest.raises(isolated.PhaseError):
        isolated._regular(path)
    outside = isolated._WHEELHOUSE / 'unlinked.whl'
    def unlinked(item, *args, **kwargs):
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_nlink=0)
    monkeypatch.setattr(Path, 'lstat', unlinked)
    with pytest.raises(isolated.PhaseError):
        isolated._regular(outside, sealed_input=True)


@pytest.mark.parametrize('output', ['raw_fd', 'child_process'])
@pytest.mark.parametrize('fail', [False, True])
def test_backend_native_output_cannot_corrupt_final_json(isolated, monkeypatch, capfd, output, fail):
    marker = b'backend-native-progress\n'
    def produce(directory):
        if output == 'raw_fd':
            os.write(1, marker)
        else:
            # Fixed stdlib output probe, never project code or hooks on the host.
            with subprocess.Popen([_TEST_INTERPRETER, '-I', '-S', '-B', '-c',
                'import os;os.write(1,b"backend-native-progress\\n")']) as child:
                assert child.wait(timeout=10) == 0
        if fail:
            raise RuntimeError('controlled failure')
        (directory / 'sample-1-py3-none-any.whl').write_bytes(b'wheel')
        return 'sample-1-py3-none-any.whl'
    fake_backend(isolated, monkeypatch, produce)
    assert isolated.main(['build', '--import-module', 'sample']) == int(fail)
    captured = capfd.readouterr()
    assert json.loads(captured.out)['status'] == ('failed' if fail else 'completed')
    assert 'backend-native-progress' in captured.err


@pytest.mark.parametrize('kind', ['hardlink', 'fifo'])
def test_source_rejects_shared_files_and_special_nodes(isolated, monkeypatch, kind):
    source = isolated._WORKSPACE / 'shared'
    source.write_bytes(b'fixture')
    if kind == 'hardlink':
        os.link(source, isolated._WHEELHOUSE / 'alias')
    else:
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == source:
                return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_nlink=1)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'lstat', lstat)
    loader = fake_backend(isolated, monkeypatch)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('build', import_module='sample')
    loader.assert_not_called()


def test_backend_exception_restores_cwd_and_does_not_install(isolated, monkeypatch):
    loader = fake_backend(isolated, monkeypatch)
    loader.return_value.build_wheel = Mock(side_effect=RuntimeError('private build details'))
    before = Path.cwd()
    with pytest.raises(isolated.PhaseError) as caught:
        isolated.run_phase('build', import_module='sample')
    assert Path.cwd() == before
    assert 'private' not in str(caught.value)
    isolated.subprocess.run.assert_not_called()


def fake_tests(module, monkeypatch, *, count=2, success=True, origin=None):
    site = module._OUTPUT / 'project-site'
    site.mkdir()
    (site / 'sample').mkdir()
    (site / 'sample/__init__.py').write_text('# fixture, never imported\n')
    origin = origin or str(site / 'sample/__init__.py')
    imported = SimpleNamespace(__spec__=SimpleNamespace(origin=origin))
    loader = Mock(return_value=imported)
    monkeypatch.setattr(module.importlib, 'import_module', loader)
    suite = SimpleNamespace(countTestCases=lambda: count)
    discovery = Mock(return_value=suite)
    monkeypatch.setattr(module.unittest, 'TestLoader', lambda: SimpleNamespace(discover=discovery))
    run = Mock(return_value=SimpleNamespace(testsRun=count, wasSuccessful=lambda: success))
    monkeypatch.setattr(module.unittest, 'TextTestRunner', lambda **kwargs: SimpleNamespace(run=run))
    return loader, discovery, run


def test_tests_use_original_readonly_snapshot_and_import_from_installed_site(isolated, monkeypatch):
    module = isolated
    loader, discovery, run = fake_tests(module, monkeypatch)
    before = list(module.sys.path)
    result = module.run_phase('test', import_module='sample')
    loader.assert_called_once_with('sample')
    discovery.assert_called_once_with(start_dir=str(module._WORKSPACE / 'tests'),
                                      pattern='test*.py', top_level_dir=str(module._WORKSPACE / 'tests'))
    run.assert_called_once()
    assert module.sys.path == before
    assert result['test_count'] == 2
    assert result['import'] == {'module': 'sample', 'origin': 'sample/__init__.py'}
    assert result['evidence'] == 'native_process_only'


@pytest.mark.parametrize(('count', 'success'), [(0, True), (2, False)])
def test_empty_or_failing_suite_is_a_failed_phase(isolated, monkeypatch, count, success):
    fake_tests(isolated, monkeypatch, count=count, success=success)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('test', import_module='sample')


@pytest.mark.parametrize('origin', ['/workspace/sample.py', '/output/project-site-other/a.py',
                                   'built-in', None])
def test_import_requires_actual_site_origin(isolated, monkeypatch, origin):
    loader, discovery, _ = fake_tests(isolated, monkeypatch)
    loader.return_value.__spec__.origin = origin
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('test', import_module='sample')
    discovery.assert_not_called()


def test_test_import_is_fresh_and_site_precedes_snapshot(isolated, monkeypatch):
    loader, discovery, _ = fake_tests(isolated, monkeypatch)
    def imported(name):
        assert isolated.sys.path[0] == str(isolated._OUTPUT / 'project-site')
        return loader.return_value
    loader.side_effect = imported
    isolated.run_phase('test', import_module='sample')
    discovery.assert_called_once()


def test_cached_selected_module_is_rejected_without_reusing_old_import(isolated, monkeypatch):
    loader, discovery, _ = fake_tests(isolated, monkeypatch)
    monkeypatch.setitem(isolated.sys.modules, 'sample', loader.return_value)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('test', import_module='sample')
    loader.assert_not_called()
    discovery.assert_not_called()


def test_skipped_execution_cannot_report_discovered_tests_as_run(isolated, monkeypatch):
    _, _, run = fake_tests(isolated, monkeypatch)
    run.return_value.testsRun = 0
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('test', import_module='sample')


def test_entrypoint_failure_emits_only_sanitized_json(phase_module, monkeypatch, capsys):
    module = phase_module
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setenv('AGENT_HUB_PYTHON_PROJECT_ISOLATED', '1')
    monkeypatch.setattr(module, 'run_phase', Mock(side_effect=RuntimeError('C:/private/secret')))
    assert module.main(['test', '--import-module', 'sample']) != 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {'status': 'failed', 'error': 'phase_failed'}
    assert 'private' not in captured.out + captured.err


def test_cli_install_success_reports_native_process_evidence(isolated, capsys):
    assert isolated.main(['install', '--import-module', 'sample']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {'status': 'completed', 'phase': 'install',
                      'evidence': 'native_process_only', 'requires_dist_check': 'completed'}


def test_unisolated_cli_never_dispatches(phase_module, monkeypatch, capsys):
    monkeypatch.setattr(phase_module.sys, 'platform', 'win32')
    dispatch = Mock(side_effect=AssertionError('must not run'))
    monkeypatch.setattr(phase_module, 'run_phase', dispatch)
    assert phase_module.main(['build', '--import-module', 'sample']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    dispatch.assert_not_called()


def test_backend_stdout_does_not_corrupt_json_report(isolated, monkeypatch, capsys):
    def produce(directory):
        print('backend progress')
        (directory / 'sample-1-py3-none-any.whl').write_bytes(b'wheel')
        return 'sample-1-py3-none-any.whl'
    fake_backend(isolated, monkeypatch, produce)
    assert isolated.main(['build', '--import-module', 'sample']) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['wheel']['sha256'] == hashlib.sha256(b'wheel').hexdigest()
    assert 'backend progress' in captured.err


def test_source_open_rejects_changed_file_identity(isolated, monkeypatch):
    original = isolated.os.fstat
    def fstat(descriptor):
        info = original(descriptor)
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1,
                               st_mode=info.st_mode, st_nlink=info.st_nlink,
                               st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                               st_ctime_ns=info.st_ctime_ns)
    monkeypatch.setattr(isolated.os, 'fstat', fstat)
    loader = fake_backend(isolated, monkeypatch)
    with pytest.raises(isolated.PhaseError):
        isolated.run_phase('build', import_module='sample')
    loader.assert_not_called()


@pytest.mark.parametrize('args', [
    ['shell', '--import-module', 'sample'], ['test', '--import-module', '../sample'],
    ['test', '--import-module', 'sample', '--cwd', '/host'],
    ['install', '--import-module', 'sample', '--command', 'id'],
])
def test_cli_rejects_extra_controls_without_running(phase_module, monkeypatch, capsys, args):
    module = phase_module
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setenv('AGENT_HUB_PYTHON_PROJECT_ISOLATED', '1')
    execute = Mock()
    monkeypatch.setattr(module, 'run_phase', execute)
    assert module.main(args) != 0
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    execute.assert_not_called()
