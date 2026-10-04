"""Stdlib-only, Python-owned observations of the large-module-v1 contract.

Node is an untrusted participant: its result frames are observations, never a
verdict. Only accepted state transitions and real HTTP responses earn counts.
Importing this leaf neither imports the transport nor starts child processes.
"""

from __future__ import annotations

import http.client
import io
import json
import math
import runpy
import secrets
import socket
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol, cast

_COUNTS = {
    "inventory_stores": 2, "inventory_instances": 3, "inventory_calls": 14,
    "inventory_commits": 14, "inventory_successes": 10, "inventory_conflicts": 4,
    "concurrency_pairs": 1, "reporting_instances": 2, "reporting_snapshots": 6,
    "reporting_reads": 6, "composition_http_requests": 9, "composition_markers": 5,
    "starts": 1, "stops": 1,
}
_MAX_HTTP = 65536
_Map = dict[str, Any]


class _Process(Protocol):
    isolated: bool

    def request(
        self, command: dict[str, object],
        on_event: Callable[[dict[str, object]], dict[str, object]] | None = None,
    ) -> object: ...

    def close(self) -> None: ...


def _require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("large-module validation deadline exceeded")
    return remaining


def _object(value: object, context: str) -> _Map:
    _require(isinstance(value, dict), f"{context}: expected an object")
    return cast(_Map, value)


def _equal(actual: object, expected: object) -> bool:
    # Python's bool/int equality must never validate a typed JSON contract.
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ValueError(f"nonfinite JSON number: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"nonfinite JSON number: {value}")
    return number


def _json(raw: str | bytes) -> object:
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite,
                      parse_float=_finite_float)


def _entry(root: Path) -> str:
    package = root / "package.json"
    _require(package.stat().st_size <= _MAX_HTTP, "locator: package.json is too large")
    data = _object(_json(package.read_text(encoding="utf-8")), "locator package")
    locator = _object(data.get("agent_hub_acceptance"), "locator")
    _require(type(locator.get("version")) is int and locator["version"] == 1,
             "locator: version must be integer 1")
    entry = locator.get("entry")
    _require(isinstance(entry, str) and bool(entry.strip()), "locator: missing entry")
    entry = cast(str, entry)
    windows = PureWindowsPath(entry)
    _require(not windows.drive and not windows.root and "\\" not in entry,
             "locator: entry must be a contained relative path")
    path = (root / entry).resolve()
    _require(path.is_relative_to(root) and path.is_file(), "locator: entry escapes root or is absent")
    return "./" + path.relative_to(root).as_posix()


def _created(response: object, sku: str, context: str) -> _Map:
    response = _object(response, context)
    _require(type(response.get("status")) is int and response["status"] == 201,
             f"{context}: expected 201")
    body = _object(response.get("body"), context)
    identifier = body.get("id")
    _require((type(identifier) is int) or (isinstance(identifier, str) and identifier.strip()),
             f"{context}: id must be a nonempty string or integer, never bool")
    _require(body.get("sku") == sku, f"{context}: sku mismatch")
    return body


def _conflict(response: object, context: str) -> None:
    response = _object(response, context)
    _require(type(response.get("status")) is int and response["status"] == 409,
             f"{context}: expected 409")
    error = _object(_object(response.get("body"), context).get("error"), context)
    _require(all(isinstance(error.get(key), str) and error[key].strip()
                 for key in ("code", "message")), f"{context}: missing conflict error")


class _Call:
    def __init__(self, instance: str, method: str, sku: str, quantity: int) -> None:
        self.id = secrets.token_hex(16)
        self.instance = instance
        self.store = instance[0]
        self.method = method
        self.args: _Map = {"sku": sku, "quantity": quantity}
        if method == "reserve":
            self.args["reason"] = secrets.token_hex(16)
        self.token: str | None = None
        self.response: _Map | None = None
        self.began = False
        self.committed = False

    def command(self) -> dict[str, object]:
        return {"op": "inventory_call", "call": self.id, "instance": self.instance,
                "method": self.method, "args": self.args}


class _Inventory:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.states: dict[str, _Map] = {
            "A": {"stock": {}, "reservations": []},
            "B": {"stock": {}, "reservations": []},
        }
        self.calls: dict[str, _Call] = {}
        self.locks: dict[str, str] = {}

    def event(self, event: dict[str, object]) -> dict[str, object]:
        call_id = event.get("call")
        _require(isinstance(call_id, str) and call_id in self.calls,
                 "inventory: event for unknown call")
        call = self.calls[cast(str, call_id)]
        _require(event.get("store") == call.store, "inventory: wrong store (shared state)")
        kind = event.get("type")
        if kind == "store_begin":
            _require(not call.began and call.store not in self.locks,
                     "inventory: duplicate or overlapping transaction")
            call.began = True
            call.token = secrets.token_hex(16)
            self.locks[call.store] = call.id
            return {"state": deepcopy(self.states[call.store]), "token": call.token}
        _require(kind == "store_commit", "inventory: aborted or invalid transaction event")
        _require(call.began and not call.committed and self.locks.get(call.store) == call.id
                 and event.get("token") == call.token, "inventory: stale transaction proposal")
        self._commit(call, event)
        del self.locks[call.store]
        call.committed = True
        self.counts["inventory_commits"] += 1
        return {"accepted": True}

    def _commit(self, call: _Call, event: dict[str, object]) -> None:
        state = self.states[call.store]
        proposal = _object(event.get("state"), "inventory state")
        response = _object(event.get("response"), "inventory response")
        sku, quantity = call.args["sku"], call.args["quantity"]
        available = state["stock"].get(sku, 0)
        expected = deepcopy(state)
        if call.method == "reserve" and available < quantity:
            _conflict(response, "inventory conflict")
        else:
            body = _created(response, sku, "inventory success")
            expected["stock"][sku] = available + (quantity if call.method == "addStock" else -quantity)
            if call.method == "reserve":
                rows = proposal.get("reservations")
                _require(isinstance(rows, list) and len(rows) == len(state["reservations"]) + 1,
                         "inventory: reservation was not appended atomically")
                row = _object(cast(list[object], rows)[-1], "inventory reservation")
                _require(set(row) in ({"id", "sku", "quantity"}, {"id", "sku", "quantity", "reason"}),
                         "inventory: unexpected reservation fields")
                expected_row = {"id": body["id"], "sku": sku, "quantity": quantity}
                if "reason" in row:
                    expected_row["reason"] = call.args["reason"]
                _require(_equal(row, expected_row), "inventory: corrupt reservation state")
                _require(all(not _equal(prior["id"], row["id"]) for prior in state["reservations"]),
                         "inventory: duplicate reservation id")
                expected["reservations"].append(expected_row)
            stock = _object(proposal.get("stock"), "inventory stock")
            _require(all(_equal(stock.get(key, 0), expected["stock"].get(key, 0))
                         for key in stock.keys() | expected["stock"].keys()),
                     "inventory: stock quantity mismatch")
            # Keep the accepted zero-key representation so later conflicts cannot
            # hide writes; every quantity still comes from Python's calculation.
            expected["stock"] = {key: expected["stock"].get(key, 0) for key in stock}
        _require(_equal(proposal, expected), "inventory: entire transaction state mismatch")
        # Commit only Python's independently computed state, never the child proposal.
        self.states[call.store] = expected
        call.response = deepcopy(response)

    def accepted(self, call: _Call, result: object) -> None:
        _require(call.committed and _equal(result, call.response),
                 "inventory: return value bypassed the single committed transaction")
        self.counts["inventory_calls"] += 1
        assert call.response is not None
        key = "inventory_successes" if call.response["status"] == 201 else "inventory_conflicts"
        self.counts[key] += 1


def _request(
    process: _Process, command: dict[str, object], deadline: float,
    on_event: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> object:
    _remaining(deadline)
    value = process.request(command, on_event=on_event)
    _remaining(deadline)
    if isinstance(value, dict) and "bridge_error" in value:
        raise ValueError(f"{command['op']}: bridge rejected operation: {value['bridge_error']}")
    return value


def _inventory(process: _Process, counts: dict[str, int], deadline: float) -> None:
    model = _Inventory(counts)
    for instance in ("A1", "A2", "B1"):
        _require(_request(process, {"op": "inventory_create", "instance": instance,
                                    "store": instance[0]}, deadline) is None,
                 "inventory: invalid creation acknowledgment")
        counts["inventory_instances"] += 1
        if instance in ("A1", "B1"):
            counts["inventory_stores"] += 1
    sku = "SKU-" + secrets.token_hex(12)
    sequence = [("A1", "addStock", 2), ("A2", "addStock", 3), ("B1", "reserve", 1),
                ("B1", "addStock", 2), ("A1", "reserve", 2), ("A2", "reserve", 4),
                ("A2", "reserve", 3), ("A1", "reserve", 1), ("B1", "reserve", 1),
                ("A2", "addStock", 1), ("A1", "reserve", 1), ("A1", "addStock", 1)]
    for instance, method, quantity in sequence:
        call = _Call(instance, method, sku, quantity)
        model.calls = {call.id: call}
        result = _request(process, call.command(), deadline, model.event)
        model.accepted(call, result)
    pair = [_Call(instance, "reserve", sku, 1) for instance in ("A1", "A2")]
    model.calls = {call.id: call for call in pair}
    results = _request(process, {"op": "inventory_pair", "calls": [c.command() for c in pair]},
                       deadline, model.event)
    _require(isinstance(results, list) and len(results) == 2, "inventory: missing concurrent results")
    for call, result in zip(pair, cast(list[object], results), strict=True):
        model.accepted(call, result)
    _require(sorted(cast(_Map, call.response)["status"] for call in pair) == [201, 409],
             "inventory: concurrent reservations must have exactly one success")
    counts["concurrency_pairs"] += 1


def _rows(kind: str) -> _Map:
    if kind == "empty":
        return {"orders": [], "reservations": [], "fulfillment": []}
    seed = secrets.randbelow(5) + (2 if kind == "mixed" else 9)
    return {
        "orders": [{"payment_state": "authorized" if index % 2 else "pending"}
                   for index in range(seed)],
        "reservations": [{"quantity": 2}, {"quantity": 3}] + [{"quantity": 1}] * seed,
        "fulfillment": [{"status": "cancelled" if index % 3 else "completed"}
                        for index in range(seed + 1)],
    }


def _metrics(rows: _Map) -> _Map:
    return {"orders": {"total": len(rows["orders"]), "authorized_count": sum(
        row["payment_state"] == "authorized" for row in rows["orders"])},
        "inventory": {"reserved_units": sum(row["quantity"] for row in rows["reservations"])},
        "fulfillment": {"total": len(rows["fulfillment"]), "cancelled_count": sum(
            row["status"] == "cancelled" for row in rows["fulfillment"])}}


def _report(actual: object, expected: _Map, context: str) -> None:
    actual = _object(actual, context)
    for section, fields in expected.items():
        values = _object(actual.get(section), context)
        for metric, value in fields.items():
            _require(type(values.get(metric)) is int and values[metric] >= 0
                     and values[metric] == value, f"{context}: {section}.{metric} mismatch")


def _reporting(process: _Process, counts: dict[str, int], deadline: float) -> None:
    for instance in ("R1", "R2"):
        _require(_request(process, {"op": "reporting_create", "instance": instance}, deadline) is None,
                 "reporting: invalid creation acknowledgment")
        counts["reporting_instances"] += 1
    for instance, kind in [("R1", "empty"), ("R2", "mixed"), ("R1", "mixed"),
                           ("R1", "replaced"), ("R2", "replaced"), ("R1", "empty")]:
        rows = _rows(kind)
        reads = 0

        def on_event(
            event: dict[str, object], *, instance: str = instance, rows: _Map = rows,
        ) -> dict[str, object]:
            nonlocal reads
            _require(event.get("type") == "report_read" and event.get("instance") == instance,
                     "reporting: wrong reader event")
            _require(reads == 0 and _equal(event.get("rows"), rows),
                     "reporting: reader input mutated or read more than once")
            reads += 1
            counts["reporting_reads"] += 1
            return {"accepted": True}

        result = _request(process, {"op": "reporting_snapshot", "instance": instance, "rows": rows},
                          deadline, on_event)
        _require(reads == 1, "reporting: snapshot did not read current rows")
        _report(result, _metrics(rows), "reporting snapshot")
        counts["reporting_snapshots"] += 1


class _ResponseSocket:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO(self.raw)


def _http(port: int, method: str, path: str, body: _Map | None, deadline: float) -> tuple[int, object]:
    payload = b"" if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    head = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n").encode("ascii")
    # Receiving bounded raw chunks keeps a slow response under the same absolute
    # deadline, including status line and headers. Parse HTTP only after EOF.
    with socket.create_connection(("127.0.0.1", port), timeout=_remaining(deadline)) as connection:
        connection.sendall(head + payload)
        raw = bytearray()
        while True:
            connection.settimeout(_remaining(deadline))
            chunk = connection.recv(min(8192, _MAX_HTTP + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            _require(len(raw) <= _MAX_HTTP, "composition: HTTP response exceeds limit")
    response = http.client.HTTPResponse(cast(Any, _ResponseSocket(bytes(raw))))
    try:
        response.begin()
        content = response.read(_MAX_HTTP + 1)
        _require(len(content) <= _MAX_HTTP, "composition: HTTP body exceeds limit")
        return response.status, _json(content)
    finally:
        response.close()


def _composition(process: _Process, counts: dict[str, int], deadline: float, port: int) -> None:
    sku = "HTTP-" + secrets.token_hex(12)

    def request(method: str, path: str, body: _Map | None, status: int) -> object:
        actual_status, actual = _http(port, method, path, body, deadline)
        _require(actual_status == status, f"composition: {path} expected {status}, got {actual_status}")
        counts["composition_http_requests"] += 1
        return actual

    def challenge(method: str | None, response: object = None) -> None:
        payload = None if method is None else {"method": method, "response": response}
        _require(_request(process, {"op": "app_challenge", "challenge": payload}, deadline) is None,
                 "composition: invalid challenge acknowledgment")

    item = request("POST", "/catalog/items", {"sku": sku, "name": "Module Widget", "price": 1200}, 201)
    _created({"status": 201, "body": item}, sku, "composition catalog")
    for path, quantity in (("/inventory/stock", 2), ("/inventory/reservations", 1)):
        result = request("POST", path, {"sku": sku, "quantity": quantity, "reason": "ordinary"}, 201)
        _created({"status": 201, "body": result}, sku, "composition ordinary inventory")
    for method, path in (("addStock", "/inventory/stock"), ("reserve", "/inventory/reservations")):
        marker = {"id": secrets.token_hex(24), "sku": sku}
        challenge(method, {"status": 201, "body": marker})
        result = request("POST", path, {"sku": sku, "quantity": 1, "reason": secrets.token_hex(8)}, 201)
        result = _created({"status": 201, "body": result}, sku, "composition marker")
        _require(_equal(result["id"], marker["id"]), "composition: inventory injection discarded")
        counts["composition_markers"] += 1
    conflict = {"error": {"code": secrets.token_hex(16), "message": secrets.token_hex(24)}}
    challenge("reserve", {"status": 409, "body": conflict})
    result = request("POST", "/inventory/reservations", {"sku": sku, "quantity": 1,
                                                        "reason": secrets.token_hex(8)}, 409)
    _conflict({"status": 409, "body": result}, "composition conflict")
    _require(_equal(_object(result, "composition conflict")["error"], conflict["error"]),
             "composition: conflict marker discarded")
    counts["composition_markers"] += 1
    challenge(None)
    ordinary = request("GET", "/admin/reports/summary", None, 200)
    _report(ordinary, {"orders": {"total": 0, "authorized_count": 0},
                       "inventory": {"reserved_units": 1},
                       "fulfillment": {"total": 0, "cancelled_count": 0}}, "composition report")
    for kind in ("mixed", "replaced"):
        marker = _metrics(_rows(kind))
        challenge("snapshot", marker)
        result = request("GET", "/admin/reports/summary", None, 200)
        _report(result, marker, "composition report marker")
        counts["composition_markers"] += 1


def _port_closed(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) != 0


def validate_large_modules(root: Path, timeout_seconds: float) -> dict[str, object]:
    """Observe fixed module and composition contracts, without npm-start binding credit."""
    started = time.monotonic()
    counts = dict.fromkeys(_COUNTS, 0)
    reasons: list[str] = []
    status = "failed"
    process: _Process | None = None
    port: int | None = None
    isolated = False
    cleanup_ok = True
    app_attempted = False
    app_closed = False
    deadline = started
    try:
        _require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                 and timeout_seconds > 0, "timeout must be finite and positive")
        deadline = started + timeout_seconds
        root = root.resolve(strict=True)
        entry = _entry(root)
        transport = Path(__file__).with_name("project_validation_module_process.py")
        if not transport.is_file():
            status = "unknown"
            raise RuntimeError("module process isolation transport unavailable")
        factory = cast(Callable[[Path, float], _Process], runpy.run_path(str(transport))["ModuleProcess"])
        try:
            process = factory(root, deadline)
        except RuntimeError:
            status = "unknown"
            raise
        isolated = process.isolated is True
        _require(_request(process, {"op": "load", "entry": entry}, deadline) is None,
                 "locator: invalid module load acknowledgment")
        _inventory(process, counts, deadline)
        _reporting(process, counts, deadline)
        app_attempted = True
        address = _object(_request(process, {"op": "app_start"}, deadline), "composition listener")
        candidate_port = address.get("port")
        _require(type(candidate_port) is int and 0 < candidate_port < 65536,
                 "composition: invalid listener port")
        port = cast(int, candidate_port)
        counts["starts"] += 1
        _composition(process, counts, deadline, port)
        _require(_request(process, {"op": "app_close"}, deadline) is None,
                 "app_close: invalid close acknowledgment")
        app_closed = True
        status = "passed" if isolated else "unknown"
        if not isolated:
            reasons.append("isolation unavailable: trusted Windows fixture behavior only")
    except Exception as exc:  # noqa: BLE001 - invalid child observations always fail closed
        reasons.append(f"{type(exc).__name__}: {exc}")
    finally:
        if process is not None:
            if app_attempted and not app_closed:
                try:
                    _require(_request(process, {"op": "app_close"}, deadline) is None,
                             "app_close: invalid close acknowledgment")
                    app_closed = True
                except Exception as exc:  # noqa: BLE001 - tree cleanup must still run
                    cleanup_ok = False
                    reasons.append(f"app_close failed: {exc}")
            try:
                process.close()
            except Exception as exc:  # noqa: BLE001 - preserve cleanup failure in the result
                cleanup_ok = False
                reasons.append(f"process close failed: {exc}")
            if port is not None:
                try:
                    closed = _port_closed(port)
                    if not closed:
                        cleanup_ok = False
                        reasons.append("cleanup: app listener still accepts connections")
                    elif app_closed:
                        counts["stops"] = 1
                except OSError as exc:
                    cleanup_ok = False
                    reasons.append(f"cleanup port verification failed: {exc}")
        if not cleanup_ok:
            status = "failed"
    if status == "passed" and counts != _COUNTS:
        status = "failed"
        reasons.append("incomplete large-module observations")
    return {
        "schema_version": 1, "profile": "large-module-v1", "scale": "large",
        "status": status, "reasons": reasons, "cleanup_ok": cleanup_ok,
        "measurements": {**counts, "elapsed_seconds": max(0.0, time.monotonic() - started)},
        "isolation_verified": isolated, "npm_start_module_binding": "unknown",
    }
