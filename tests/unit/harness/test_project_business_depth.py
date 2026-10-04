from __future__ import annotations

import csv
import importlib
import io
import json
import shutil
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

_FIXTURES = Path(__file__).resolve().parents[2] / 'fixtures' / 'project_business'
_MODULES = ['milestones', 'budgets', 'staffing', 'risks']


def _application(tmp_path: Path, scale: str, fault: str = '') -> Path:
    if shutil.which('node') is None or shutil.which('npm') is None:
        pytest.skip('real HTTP fixture requires Node and npm')
    root = tmp_path / f'{scale} project with spaces'
    root.mkdir()
    (root / 'package.json').write_text(
        json.dumps({'private': True, 'scripts': {'start': 'node server.cjs'}}), encoding='utf-8'
    )
    (root / 'server.cjs').write_text(
        (_FIXTURES / f'{scale}.cjs').read_text(encoding='utf-8'), encoding='utf-8'
    )
    (root / 'fault.txt').write_text(fault, encoding='utf-8')
    return root


def _assert_stopped(root: Path) -> list[dict[str, object]]:
    launches = [
        json.loads(line)
        for line in (root / 'launches.jsonl').read_text(encoding='utf-8').splitlines()
    ]
    assert launches, 'npm must actually launch the trusted HTTP fixture'
    deadline = time.monotonic() + 2
    for launch in launches:
        assert not Path(str(launch['data'])).exists(), 'temporary DATA_DIR leaked'
        for key in ('port', 'childPort'):
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, f'{key} still accepts connections after validation'
                with socket.socket() as connection:
                    connection.settimeout(min(0.1, remaining))
                    if connection.connect_ex(('127.0.0.1', int(str(launch[key])))) != 0:
                        break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return launches


def _validate(root: Path, scale: str) -> tuple[str, ...]:
    module = importlib.import_module('agent_hub.harness.project_requirements')
    try:
        if scale == 'large':
            result = module._validate_large_order_ops_api(root, timeout_seconds=25)
        else:
            result = module._validate_ultra_portfolio_api(root, timeout_seconds=25)
    finally:
        _assert_stopped(root)
    assert isinstance(result, tuple)
    assert all(isinstance(reason, str) and reason for reason in result)
    return result


def _requests(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (root / 'requests.jsonl').read_text(encoding='utf-8').splitlines()
    ]


def _assert_large_trace(root: Path) -> None:
    requests = _requests(root)
    reservations = [row for row in requests if row['url'] == '/inventory/reservations']
    exhausted = [row for row in reservations if row['body']['sku'] == reservations[0]['body']['sku']]
    assert [(row['body']['quantity'], row['status']) for row in exhausted] == [
        (1, 201), (99, 409), (1, 201), (1, 409), (1, 201), (1, 409), (1, 201),
    ]
    remaining = [row for row in reservations if row['body']['sku'] != reservations[0]['body']['sku']]
    assert [(row['body']['quantity'], row['status']) for row in remaining] == [
        (1, 201), (1, 201), (1, 409),
    ]
    assert remaining[0]['pid'] != remaining[1]['pid']
    order_index = next(index for index, row in enumerate(requests)
                       if row['url'] == '/orders' and row['status'] == 201)
    assert requests[order_index]['body']['lines'][0]['sku'] != reservations[0]['body']['sku']
    before_order = [row['payload'] for row in requests[:order_index]
                    if row['url'] == '/admin/reports/summary']
    assert len(before_order) >= 2, 'measure explicit reservations before order side effects'
    assert (before_order[-1]['inventory']['reserved_units']
            == before_order[0]['inventory']['reserved_units'] + 4)
    reports = [row for row in requests if row['url'] == '/admin/reports/summary']
    original = [row for row in reports if row['pid'] == reports[0]['pid']]
    restarted = [row for row in reports if row['pid'] != reports[0]['pid']]
    assert original[-1]['payload'] == restarted[0]['payload']
    assert (restarted[-1]['payload']['inventory']['reserved_units']
            == restarted[0]['payload']['inventory']['reserved_units'] + 2)
    orders = [row['payload']['id'] for row in requests
              if row['url'] == '/orders' and row['status'] == 201]
    assert len(set(orders)) == 2
    for order_id in orders:
        audits = [row for row in requests if row['url'] == f'/audit?entity_id={order_id}']
        assert len({row['pid'] for row in audits}) == 2
        assert all(item['entity_id'] == order_id for row in audits for item in row['payload']['items'])


def _assert_ultra_trace(root: Path, launches: list[dict[str, object]]) -> None:
    requests = _requests(root)
    programs = [row['payload'] for row in requests
                if row['method'] == 'POST' and row['url'] == '/programs']
    projects = [row['payload'] for row in requests
                if row['method'] == 'POST' and row['url'] == '/projects']
    assert len(programs) == 2 and len(projects) == 3
    assert sorted(sum(project['program_id'] == program['id'] for project in projects)
                  for program in programs) == [1, 2]
    assert any(',' in project['name'] and '"' in project['name'] for project in projects)
    pids = {launch['pid'] for launch in launches}
    for module in _MODULES:
        writes = [row for row in requests
                  if row['method'] == 'POST' and row['url'].endswith('/' + module)]
        assert writes, f'{module}: no business write was exercised'
        for write in writes:
            expected = {**write['body'], 'id': write['payload']['id'],
                        'project_id': write['url'].split('/')[2]}
            reads = [row for row in requests
                     if row['method'] == 'GET' and row['url'] == write['url']]
            assert {row['pid'] for row in reads} == pids, f'{module}: missing restart readback'
            for read in reads:
                items = read['payload']['items']
                assert all(item['project_id'] == expected['project_id'] for item in items)
                assert any(all(item.get(key) == value for key, value in expected.items())
                           for item in items), f'{module}: submitted fields did not persist'
    for program in programs:
        expected_projects = {project['id']: project for project in projects
                             if project['program_id'] == program['id']}
        for endpoint in ('/analytics/portfolio.csv', '/portfolio/read-model'):
            reads = [row for row in requests if row['method'] == 'GET'
                     and urlsplit(row['url']).path == endpoint
                     and parse_qs(urlsplit(row['url']).query).get('program_id') == [program['id']]]
            assert {row['pid'] for row in reads} == pids, 'both programs need reads after restart'
            for read in reads:
                items = (list(csv.DictReader(io.StringIO(read['payload']), strict=True))
                         if endpoint.endswith('.csv') else read['payload']['items'])
                assert len(items) == len(expected_projects)
                assert {item['project_id'] for item in items} == set(expected_projects)
                for item in items:
                    assert item['program_id'] == program['id']
                    assert item['name'] == expected_projects[item['project_id']]['name']


@pytest.mark.parametrize('fault', ['', 'implicit_order_reservation'], ids=['explicit', 'implicit'])
def test_large_persistent_business_api_passes(tmp_path: Path, fault: str) -> None:
    root = _application(tmp_path, 'large', fault)

    assert _validate(root, 'large') == ()

    launches = _assert_stopped(root)
    assert len(launches) == 2, 'must actually restart npm to verify persistence'
    assert len({launch['data'] for launch in launches}) == 1
    assert len({launch['pid'] for launch in launches}) == 2
    _assert_large_trace(root)


@pytest.mark.parametrize('fault', [
    'no_deduction', 'failed_request_deducts', 'volatile_inventory', 'static_report',
    'lost_inventory', 'failed_empty_deducts', 'audit_unfiltered',
    'audit_wrong_entity', 'audit_no_action', 'audit_missing_created', 'audit_missing_payment',
    'report_value:orders.total:true', 'report_value:orders.authorized_count:""',
    'report_value:inventory.reserved_units:-1', 'report_value:fulfillment.total:0.5',
    'report_value:fulfillment.cancelled_count:null',
])
def test_large_business_fault_is_rejected(tmp_path: Path, fault: str) -> None:
    root = _application(tmp_path, 'large', fault)

    failures = _validate(root, 'large')

    assert failures, f'{fault} incorrectly passed large business acceptance'
    assert not any('cleanup failed' in reason or 'startup:' in reason for reason in failures)
    assert not any('timeout' in reason.lower() for reason in failures)
    context = ('audit' if fault.startswith('audit_') else 'admin report'
               if fault == 'static_report' or fault.startswith('report_value:') else 'inventory')
    assert context in ' '.join(failures).lower(), failures
    if fault in {'volatile_inventory', 'lost_inventory'}:
        assert len(_assert_stopped(root)) == 2, 'inventory loss must be detected after restart'


def test_ultra_persistent_business_api_passes(tmp_path: Path) -> None:
    root = _application(tmp_path, 'ultra')

    assert _validate(root, 'ultra') == ()

    launches = _assert_stopped(root)
    assert len(launches) == 2, 'must actually restart npm to verify module persistence'
    assert len({launch['data'] for launch in launches}) == 1
    assert len({launch['pid'] for launch in launches}) == 2
    _assert_ultra_trace(root, launches)


@pytest.mark.parametrize('module', _MODULES)
@pytest.mark.parametrize('mutation', ['drop', 'volatile', 'wrong_project', 'wrong_fields'])
def test_ultra_module_fault_is_rejected(tmp_path: Path, module: str, mutation: str) -> None:
    fault = f'{mutation}_{module}'
    root = _application(tmp_path, 'ultra', fault)

    failures = _validate(root, 'ultra')

    assert failures, f'{fault} incorrectly passed ultra business acceptance'
    assert not any('cleanup failed' in reason or 'startup:' in reason for reason in failures)
    assert not any('timeout' in reason.lower() for reason in failures)
    assert module in ' '.join(failures), failures
    requests = _requests(root)
    write = next(row for row in requests
                 if row['method'] == 'POST' and row['url'].endswith('/' + module))
    assert write['status'] == 201, 'module mutation must acknowledge the business write'
    reads = [row for row in requests if row['method'] == 'GET' and row['url'] == write['url']]
    assert reads and all(row['status'] == 200 for row in reads)
    if mutation == 'volatile':
        assert len(_assert_stopped(root)) == 2, 'volatile faults must reach the real restart'
        assert reads[0]['payload']['items'], 'volatile record must exist before restart'
        assert reads[-1]['payload']['items'] == []
        assert reads[0]['pid'] != reads[-1]['pid']


@pytest.mark.parametrize('fault', [
    'csv_static', 'csv_unfiltered', 'csv_duplicate', 'csv_bad_quote',
    'read_model_static', 'read_model_unfiltered', 'read_model_fake_aggregate',
    'read_model_first_budget', 'read_model_first_staffing',
    'read_model_presence_risks', 'read_model_presence_milestones',
])
def test_ultra_analytics_fault_is_rejected(tmp_path: Path, fault: str) -> None:
    root = _application(tmp_path, 'ultra', fault)

    failures = _validate(root, 'ultra')

    assert failures, f'{fault} incorrectly passed ultra analytics acceptance'
    assert not any('cleanup failed' in reason or 'startup:' in reason for reason in failures)
    assert not any('timeout' in reason.lower() for reason in failures)
    csv_fault = fault.startswith('csv_')
    assert ('portfolio CSV' if csv_fault else 'portfolio read model') in ' '.join(failures)
    last_request = _requests(root)[-1]
    assert last_request['status'] == 200, 'analytics mutation must return an HTTP success'
    assert urlsplit(last_request['url']).path == (
        '/analytics/portfolio.csv' if csv_fault else '/portfolio/read-model'
    )
