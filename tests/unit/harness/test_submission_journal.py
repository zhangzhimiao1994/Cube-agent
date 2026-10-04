from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from typing import cast

import pytest

from agent_hub.harness.submission_journal import SubmissionJournal


def _identity() -> dict[str, object]:
    return {
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "execution_id": "matrix-1",
        "base_url": "https://example.invalid",
    }


def _context() -> dict[str, object]:
    return {
        "case_id": "auto-small",
        "attempt": 1,
        "project_id": "project-1",
        "conversation_id": "conversation-1",
        "workspace_session_id": "workspace-1",
    }


def _body() -> dict[str, object]:
    return {
        "messages": [{"role": "user", "content": "private-message-\u4e2d"}],
        "decision_token": "private-token",
        "model_config": {"temperature": 0.5, "model": "model-a"},
    }


def _response() -> dict[str, object]:
    return {
        "id": "run-1",
        "tenant_id": "tenant-1",
        "status": "queued",
        "project_id": "project-1",
        "conversation_id": "conversation-1",
        "workspace_session_id": "workspace-1",
        "version": 1,
        "mode": "direct",
        "requested_mode": "auto",
        "effective_mode": "direct",
        "effective_scale": "small",
        "route_reason": "small task",
        "mode_source": "router",
    }


def _noop() -> None:
    pass


def _must_not_persist() -> None:
    pytest.fail("this operation must not persist")


def _prepare(
    journal: SubmissionJournal,
    *,
    path: str = "/api/v1/runs",
    key: str | None = "key-1",
    body: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
    persist: Callable[[], None] = _noop,
) -> tuple[int, dict[str, object] | None]:
    return journal.prepare(
        path=path,
        body=_body() if body is None else body,
        idempotency_key=key,
        context=_context() if context is None else context,
        persist=persist,
    )


def _confirmed() -> SubmissionJournal:
    journal = SubmissionJournal(_identity())
    index, _ = _prepare(journal)
    journal.confirm(index, _response(), _noop)
    return journal


def test_prepare_persists_full_body_digest_before_returning_permission_to_submit() -> None:
    journal = SubmissionJournal(_identity())
    saved: list[dict[str, object]] = []
    assert journal.snapshot() == {"schema_version": 1, "identity": _identity(), "records": []}
    assert not journal.has_unresolved

    result = _prepare(journal, persist=lambda: saved.append(journal.snapshot()))

    canonical = (
        '{"decision_token":"private-token","messages":[{"content":"private-message-\u4e2d",'
        '"role":"user"}],"model_config":{"model":"model-a","temperature":0.5}}'
    )
    expected: dict[str, object] = {
        "path": "/api/v1/runs",
        "idempotency_key": "key-1",
        "request_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
        "context": _context(),
        "state": "unresolved",
        "response": None,
    }
    assert result == (0, None)
    assert journal.records == (expected,)
    assert saved == [{"schema_version": 1, "identity": _identity(), "records": [expected]}]
    assert journal.has_unresolved
    assert "private-message" not in repr(saved)
    assert "private-token" not in repr(saved)


def test_confirm_persists_only_safe_response_and_reuses_without_persistence() -> None:
    journal = SubmissionJournal(_identity())
    index, _ = _prepare(journal)
    unsafe = {
        **_response(),
        "decision_token": "private-token",
        "proposals": [{"content": "private-proposal"}],
        "messages": [{"content": "private-message"}],
        "fullmessage": "private-fullmessage",
        "body": {"password": "private-password"},
    }
    saved: list[dict[str, object]] = []
    journal.confirm(index, unsafe, lambda: saved.append(journal.snapshot()))

    assert not journal.has_unresolved
    assert journal.records[0]["state"] == "confirmed"
    assert journal.records[0]["response"] == _response()
    assert saved == [journal.snapshot()]
    assert "private-" not in repr(saved)
    reordered_body: dict[str, object] = {
        "model_config": {"model": "model-a", "temperature": 0.5},
        "decision_token": "private-token",
        "messages": [{"content": "private-message-\u4e2d", "role": "user"}],
    }
    assert _prepare(journal, body=reordered_body, persist=_must_not_persist) == (0, _response())


@pytest.mark.parametrize("field", ["messages", "decision_token", "model_config", "new_field"])
def test_same_key_rejects_any_full_body_change_without_mutation(field: str) -> None:
    journal = _confirmed()
    before = journal.snapshot()
    changed = _body()
    changed[field] = "changed"
    with pytest.raises(ValueError):
        _prepare(journal, body=changed, persist=_must_not_persist)
    assert journal.snapshot() == before


@pytest.mark.parametrize("field", list(_context()))
def test_same_key_rejects_any_context_change_without_mutation(field: str) -> None:
    journal = _confirmed()
    before = journal.snapshot()
    changed = _context()
    changed[field] = 2 if field == "attempt" else "changed"
    with pytest.raises(ValueError):
        _prepare(journal, context=changed, persist=_must_not_persist)
    assert journal.snapshot() == before


@pytest.mark.parametrize("key", ["key-1", "key-2"])
def test_any_unresolved_blocks_prepare_before_reuse_or_new_record(key: str) -> None:
    journal = _confirmed()
    assert _prepare(journal, key="key-2") == (1, None)
    before = journal.snapshot()
    with pytest.raises(ValueError, match="unresolved submission"):
        _prepare(journal, key=key, persist=_must_not_persist)
    assert journal.snapshot() == before


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["prepare", "confirm"])
def test_persistence_failure_propagates_and_leaves_blocking_unresolved_record(
    phase: str, failure_type: type[BaseException]
) -> None:
    journal = SubmissionJournal(_identity())
    failure = failure_type("persistence interrupted")
    persisted: list[dict[str, object]] = []
    sent: list[str] = []

    def fail_persist() -> None:
        persisted.append(journal.snapshot())
        raise failure

    with pytest.raises(failure_type) as raised:
        if phase == "prepare":
            _prepare(journal, persist=fail_persist)
        else:
            index, _ = _prepare(journal)
            journal.confirm(index, _response(), fail_persist)
        sent.append("transport may now run")

    assert raised.value is failure
    assert sent == []
    assert len(persisted) == 1
    persisted_record = cast(list[dict[str, object]], persisted[0]["records"])[0]
    assert persisted_record["state"] == ("unresolved" if phase == "prepare" else "confirmed")
    assert journal.has_unresolved
    assert len(journal.records) == 1
    assert journal.records[0]["state"] == "unresolved"
    assert journal.records[0]["response"] is None
    with pytest.raises(ValueError, match="unresolved submission"):
        _prepare(journal, key="later", persist=_must_not_persist)


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
def test_repair_parent_path_and_key_jointly_identify_records(prefix: str) -> None:
    journal = SubmissionJournal(_identity())
    for index, (path, key) in enumerate(
        [
            (f"{prefix}/runs", "key-1"),
            (f"{prefix}/runs", "key-2"),
            (f"{prefix}/runs/parent-1/accept-repair", None),
            (f"{prefix}/runs/parent-2/accept-repair", None),
            (f"{prefix}/runs/parent-2/accept-repair", "repair-key"),
        ]
    ):
        assert _prepare(journal, path=path, key=key) == (index, None)
        journal.confirm(index, _response(), _noop)
        assert _prepare(journal, path=path, key=key, persist=_must_not_persist) == (
            index, _response()
        )
    assert len(journal.records) == 5


def test_inputs_snapshots_records_and_reused_responses_are_isolated_copies() -> None:
    identity = _identity()
    identity["model_profile"] = {"model_chain": ["model-a", "model-a"]}
    original_identity = deepcopy(identity)
    context = _context()
    body = _body()
    response = _response()
    journal = SubmissionJournal(identity)
    index, _ = _prepare(journal, context=context, body=body)
    journal.confirm(index, response, _noop)
    expected = journal.snapshot()
    cast(dict[str, object], identity["model_profile"])["model_chain"] = []
    context["case_id"] = "changed"
    body["messages"] = []
    response["id"] = "changed"

    exported = journal.snapshot()
    cast(dict[str, object], exported["identity"])["user_id"] = "changed"
    cast(list[dict[str, object]], exported["records"])[0]["state"] = "unresolved"
    record = journal.records[0]
    cast(dict[str, object], record["context"])["case_id"] = "changed"
    cast(dict[str, object], record["response"])["id"] = "changed"
    _, reused = _prepare(journal, persist=_must_not_persist)
    assert reused is not None
    reused["id"] = "changed"
    assert journal.snapshot() == expected
    assert journal.snapshot()["identity"] == original_identity


@pytest.mark.parametrize("state", ["confirmed", "unresolved"])
def test_snapshot_round_trip_and_loaded_records_are_isolated(state: str) -> None:
    original = SubmissionJournal(_identity())
    index, _ = _prepare(original)
    if state == "confirmed":
        original.confirm(index, _response(), _noop)
    exported = original.snapshot()
    loaded = SubmissionJournal(_identity(), exported)
    assert loaded.snapshot() == original.snapshot()
    cast(list[dict[str, object]], exported["records"])[0]["state"] = "invalid"
    cast(dict[str, object], exported["identity"])["tenant_id"] = "changed"
    assert loaded.snapshot() == original.snapshot()
    if state == "unresolved":
        assert loaded.has_unresolved
        with pytest.raises(ValueError, match="unresolved submission"):
            _prepare(loaded, persist=_must_not_persist)
    else:
        assert _prepare(loaded, persist=_must_not_persist) == (0, _response())


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize(
    ("suffix", "key"),
    [
        ("/runs", None),
        ("/runs", ""),
        ("/runs", "  "),
        ("/runs", 7),
        ("/runs/parent/accept-repair", ""),
        ("/runs/parent/accept-repair", False),
        ("/runs//accept-repair", None),
        ("/runs/ /accept-repair", None),
        ("/runs/parent/child/accept-repair", None),
        ("/runs/parent/accept-repair?token=secret", None),
        ("/runs?token=secret", "key"),
        ("/runs/", "key"),
        ("/projects", "key"),
    ],
)
def test_bad_path_or_key_rejected_before_mutation_and_when_loaded(
    prefix: str, suffix: str, key: object
) -> None:
    journal = SubmissionJournal(_identity())
    before = journal.snapshot()
    with pytest.raises(ValueError):
        _prepare(journal, path=prefix + suffix, key=cast(str | None, key),
                 persist=_must_not_persist)
    assert journal.snapshot() == before
    saved = _confirmed().snapshot()
    record = cast(list[dict[str, object]], saved["records"])[0]
    record["path"] = prefix + suffix
    record["idempotency_key"] = key
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("field", list(_context()))
@pytest.mark.parametrize("stage", ["prepare", "load"])
def test_missing_context_fields_are_rejected(field: str, stage: str) -> None:
    context = _context()
    del context[field]
    _assert_context_rejected(context, stage)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", None), ("case_id", 1),
        ("attempt", True), ("attempt", 1.0), ("attempt", "1"), ("attempt", 0),
        ("attempt", -1), ("attempt", None),
        ("project_id", ""), ("project_id", "  "), ("project_id", None),
        ("conversation_id", ""), ("conversation_id", 1),
        ("workspace_session_id", ""), ("workspace_session_id", False),
        ("messages", "private-message"),
    ],
)
@pytest.mark.parametrize("stage", ["prepare", "load"])
def test_bad_context_types_or_extra_fields_are_rejected(
    field: str, value: object, stage: str
) -> None:
    context = _context()
    context[field] = value
    _assert_context_rejected(context, stage)


def _assert_context_rejected(context: dict[str, object], stage: str) -> None:
    journal = SubmissionJournal(_identity())
    before = journal.snapshot()
    if stage == "prepare":
        with pytest.raises(ValueError):
            _prepare(journal, context=context, persist=_must_not_persist)
    else:
        saved = _confirmed().snapshot()
        cast(list[dict[str, object]], saved["records"])[0]["context"] = context
        with pytest.raises(ValueError):
            SubmissionJournal(_identity(), saved)
    assert journal.snapshot() == before


def test_case_id_scope_derivation_is_left_to_caller() -> None:
    journal = SubmissionJournal(_identity())
    context = {**_context(), "case_id": ""}
    assert _prepare(journal, context=context) == (0, None)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object()])
def test_non_json_body_is_rejected_before_persistence(value: object) -> None:
    journal = SubmissionJournal(_identity())
    before = journal.snapshot()
    with pytest.raises(ValueError):
        _prepare(journal, body={"nested": [value]}, persist=_must_not_persist)
    assert journal.snapshot() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""), ("id", "  "), ("id", 1),
        ("tenant_id", "other-tenant"), ("tenant_id", None),
        ("project_id", "other-project"), ("conversation_id", "other-conversation"),
        ("workspace_session_id", "other-workspace"),
        ("status", ""), ("status", None),
        ("version", True), ("version", 0), ("version", -1), ("version", 1.0),
        ("version", "1"), ("version", None),
        ("mode", {"decision_token": "private-token"}),
        ("requested_mode", []), ("effective_mode", False), ("effective_scale", 1),
        ("route_reason", {}), ("mode_source", []),
    ],
)
@pytest.mark.parametrize("stage", ["confirm", "load"])
def test_bad_response_types_and_scope_are_rejected(
    field: str, value: object, stage: str
) -> None:
    response = _response()
    response[field] = value
    _assert_response_rejected(response, stage)


@pytest.mark.parametrize(
    "field", ["id", "tenant_id", "status", "project_id", "conversation_id", "workspace_session_id"]
)
@pytest.mark.parametrize("stage", ["confirm", "load"])
def test_missing_response_scope_is_rejected(field: str, stage: str) -> None:
    response = _response()
    del response[field]
    _assert_response_rejected(response, stage)


def _assert_response_rejected(response: dict[str, object], stage: str) -> None:
    journal = SubmissionJournal(_identity())
    index, _ = _prepare(journal)
    before = journal.snapshot()
    if stage == "confirm":
        with pytest.raises(ValueError):
            journal.confirm(index, response, _must_not_persist)
    else:
        saved = _confirmed().snapshot()
        cast(list[dict[str, object]], saved["records"])[0]["response"] = response
        with pytest.raises(ValueError):
            SubmissionJournal(_identity(), saved)
    assert journal.snapshot() == before


def test_response_version_and_routing_metadata_are_optional() -> None:
    journal = SubmissionJournal(_identity())
    index, _ = _prepare(journal)
    response = {key: value for key, value in _response().items() if key in {
        "id", "tenant_id", "status", "project_id", "conversation_id", "workspace_session_id"
    }}
    response["route_reason"] = None
    journal.confirm(index, response, _noop)
    loaded = SubmissionJournal(_identity(), journal.snapshot())
    assert _prepare(loaded, persist=_must_not_persist) == (index, response)


@pytest.mark.parametrize("index", [-1, 1, True, 0.0, "0"])
def test_confirm_requires_valid_exact_integer_index(index: object) -> None:
    journal = SubmissionJournal(_identity())
    _prepare(journal)
    before = journal.snapshot()
    with pytest.raises(ValueError):
        journal.confirm(cast(int, index), _response(), _must_not_persist)
    assert journal.snapshot() == before


def test_confirm_cannot_replace_an_already_confirmed_response() -> None:
    journal = _confirmed()
    before = journal.snapshot()
    with pytest.raises(ValueError):
        journal.confirm(0, {**_response(), "id": "other-run"}, _must_not_persist)
    assert journal.snapshot() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True), ("schema_version", 1.0), ("schema_version", "1"),
        ("schema_version", 2), ("identity", None), ("identity", {}),
        ("records", {}), ("records", ()), ("records", None), ("records", [None]),
        ("extra", "private-data"),
    ],
)
def test_loaded_root_rejects_bad_fields(field: str, value: object) -> None:
    saved = _confirmed().snapshot()
    saved[field] = value
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("field", ["schema_version", "identity", "records"])
def test_loaded_root_rejects_missing_fields(field: str) -> None:
    saved = _confirmed().snapshot()
    del saved[field]
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("snapshot", [[], (), 0, "", False])
def test_only_none_means_no_loaded_snapshot(snapshot: object) -> None:
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "pending"), ("state", ""), ("state", None), ("state", []),
        ("request_sha256", "a" * 63), ("request_sha256", "A" * 64),
        ("request_sha256", "g" * 64), ("request_sha256", "a" * 64 + "\n"),
        ("request_sha256", 1), ("path", None), ("path", []),
        ("context", []), ("context", None), ("response", None), ("response", []),
        ("body", {"messages": "private-message"}),
    ],
)
def test_loaded_record_rejects_bad_fields(field: str, value: object) -> None:
    saved = _confirmed().snapshot()
    cast(list[dict[str, object]], saved["records"])[0][field] = value
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize(
    "field", ["path", "idempotency_key", "request_sha256", "context", "state", "response"]
)
def test_loaded_record_rejects_missing_fields(field: str) -> None:
    saved = _confirmed().snapshot()
    del cast(list[dict[str, object]], saved["records"])[0][field]
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("field", ["decision_token", "proposals", "messages", "unknown"])
def test_loaded_response_rejects_non_whitelisted_fields(field: str) -> None:
    saved = _confirmed().snapshot()
    record = cast(list[dict[str, object]], saved["records"])[0]
    cast(dict[str, object], record["response"])[field] = "private-data"
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


def test_loaded_unresolved_response_must_be_none() -> None:
    saved = _confirmed().snapshot()
    cast(list[dict[str, object]], saved["records"])[0]["state"] = "unresolved"
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("path", ["/api/v1/runs", "/api/v1/runs/parent/accept-repair"])
def test_loaded_duplicate_path_key_is_rejected(path: str) -> None:
    journal = SubmissionJournal(_identity())
    _prepare(journal, path=path, key="key" if path == "/api/v1/runs" else None)
    saved = journal.snapshot()
    records = cast(list[dict[str, object]], saved["records"])
    records.append(deepcopy(records[0]))
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("field", list(_identity()))
def test_loaded_identity_must_match_every_supplied_identity_field(field: str) -> None:
    saved = _confirmed().snapshot()
    cast(dict[str, object], saved["identity"])[field] = "different"
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), saved)


@pytest.mark.parametrize("field", list(_identity()))
@pytest.mark.parametrize("value", [None, "", " ", 1])
def test_identity_requires_nonempty_strings(field: str, value: object) -> None:
    identity = _identity()
    identity[field] = value
    with pytest.raises(ValueError):
        SubmissionJournal(identity)


@pytest.mark.parametrize("field", list(_identity()))
def test_identity_requires_all_fields(field: str) -> None:
    identity = _identity()
    del identity[field]
    with pytest.raises(ValueError):
        SubmissionJournal(identity)


def test_identity_rejects_unknown_fields_even_when_snapshot_matches() -> None:
    identity = {**_identity(), "access_token": "private-token"}
    with pytest.raises(ValueError):
        SubmissionJournal(identity, {"schema_version": 1, "identity": identity, "records": []})


def test_repeated_model_keys_survive_resume_and_are_not_replaced_by_defaults() -> None:
    identity = {
        **_identity(),
        "model_profile": {"direct_model": "custom-model", "model_chain": ["model-a", "model-a"]},
    }
    journal = SubmissionJournal(identity)
    index, _ = _prepare(journal)
    journal.confirm(index, _response(), _noop)
    loaded = SubmissionJournal(deepcopy(identity), journal.snapshot())
    assert loaded.snapshot()["identity"] == identity
    assert _prepare(loaded, persist=_must_not_persist) == (index, _response())


@pytest.mark.parametrize(
    "profile",
    [None, {}, {"models": ["model-a"]}, {"models": ["model-a", "model-a"], "value": True},
     {"models": ["model-a", "model-a"], "value": 1.0}],
)
def test_loaded_model_profile_requires_exact_structure_and_types(profile: object) -> None:
    identity = {
        **_identity(),
        "model_profile": {"models": ["model-a", "model-a"], "value": 1},
    }
    saved = {"schema_version": 1, "identity": {**identity, "model_profile": profile}, "records": []}
    with pytest.raises(ValueError):
        SubmissionJournal(identity, saved)


@pytest.mark.parametrize("profile_on_expected_identity", [True, False])
def test_model_profile_presence_must_match(profile_on_expected_identity: bool) -> None:
    expected = _identity()
    stored = _identity()
    (expected if profile_on_expected_identity else stored)["model_profile"] = {"model": "custom"}
    with pytest.raises(ValueError):
        SubmissionJournal(expected, {"schema_version": 1, "identity": stored, "records": []})


@pytest.mark.parametrize(
    "value", [("model-a", "model-a"), {1: "model-a"}, float("nan"), object()]
)
def test_model_profile_rejects_non_json_structure_in_identity_and_snapshot(value: object) -> None:
    identity = {**_identity(), "model_profile": {"models": value}}
    with pytest.raises(ValueError):
        SubmissionJournal(identity)
    with pytest.raises(ValueError):
        SubmissionJournal(_identity(), {"schema_version": 1, "identity": identity, "records": []})


def test_model_profile_cannot_use_non_string_keys_as_string_key_equivalents() -> None:
    identity = {**_identity(), "model_profile": {"models": {"1": "model-a"}}}
    stored = {**_identity(), "model_profile": {"models": {1: "model-a"}}}
    with pytest.raises(ValueError):
        SubmissionJournal(identity, {"schema_version": 1, "identity": stored, "records": []})


def test_model_profile_cannot_use_tuple_as_list_equivalent() -> None:
    identity = {**_identity(), "model_profile": {"models": ["model-a", "model-a"]}}
    stored = {**_identity(), "model_profile": {"models": ("model-a", "model-a")}}
    with pytest.raises(ValueError):
        SubmissionJournal(identity, {"schema_version": 1, "identity": stored, "records": []})


@pytest.mark.parametrize("value", [{1: "model-a"}, ("model-a", "model-a")])
def test_nested_body_cannot_silently_coerce_non_json_types(value: object) -> None:
    journal = SubmissionJournal(_identity())
    before = journal.snapshot()
    with pytest.raises(ValueError):
        _prepare(journal, body={"nested": value}, persist=_must_not_persist)
    assert journal.snapshot() == before
