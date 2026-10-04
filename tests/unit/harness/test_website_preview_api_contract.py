from io import BytesIO
from zipfile import ZipFile

import pytest

from agent_hub.harness.project_scale import (
    PROJECT_SCALE_VERIFICATION_REPORT_GUIDANCE,
    PROJECT_ULTRA_LOAD_GUIDANCE,
)
from agent_hub.harness.project_scale_runner import (
    _deliverable_repair_body,
    _validate_requested_web_preview,
)


def bundle(files: dict[str, str]) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return stream.getvalue()


@pytest.mark.parametrize("entry", ("preview.html", "public/index.html", "dist/index.html"))
@pytest.mark.parametrize("fragment", (
    '<input id="apiBase" value="http://localhost:3000">',
    '<input name="api_base_url" value="http://127.0.0.2:3000/api">',
    '<input aria-label="API Base URL" value="http://[::1]:3000">',
    '<INPUT id="visibleAPIbase" value="http&#58;//LOCALHOST.:3000">',
    '<label for="endpoint">API Base</label><input id="endpoint" value="//localhost:3000">',
    '<script>const API_BASE = "http://localhost:3000"; fetch(API_BASE + "/tasks");</script>',
    "<script type='module'>let apiBase = 'http://127.0.0.1:3000';</script>",
    '<script>fetch("http://[::1]:3000/tasks");</script>',
    "<script>window.fetch('http://localhost:3000/tasks', {method: 'POST'});</script>",
))
def test_preview_guard_rejects_explicit_loopback_api_defaults(entry: str, fragment: str) -> None:
    result = _validate_requested_web_preview(
        bundle({entry: f"<!doctype html><html><body>{fragment}</body></html>"}),
        {"message": "Build a complete interactive website with a task API."},
    )

    assert result.passed is False
    assert len(result.reasons) == 1
    assert result.reasons[0].startswith("requirements: preview API default uses loopback")
    assert entry in result.reasons[0]
    assert "root-relative" in result.reasons[0]


@pytest.mark.parametrize("fragment", (
    '<input id="apiBase" value=""><script>fetch("/tasks");</script>',
    '<input id="apiBase" value="/backend"><script>fetch("/backend/tasks");</script>',
    '<button>Offline task list</button>',
    '<input id="website" type="url" value="http://localhost:3000">',
    '<input id="apiBase" placeholder="http://localhost:3000" value="">',
    '<input id="apiBase" value="http://localhost.example.test:3000">',
    '<p>Example: http://localhost:3000</p>',
    '<!-- <input id="apiBase" value="http://localhost:3000"> -->',
    '<pre><code><input id="apiBase" value="http://localhost:3000"></code></pre>',
    '<template><input id="apiBase" value="http://localhost:3000"></template>',
    '<pre><script>fetch("http://localhost:3000/tasks");</script></pre>',
    '<script type="application/json">{"apiBase":"http://localhost:3000"}</script>',
    '<script src="/app.js">fetch("http://localhost:3000/tasks");</script>',
    '<script>// Example: fetch("http://localhost:3000/tasks");\nfetch("/tasks");</script>',
    '<script>/* const API_BASE = "http://localhost:3000"; */ fetch("/tasks");</script>',
    "<script>const example = `fetch('http://localhost:3000/tasks')`;</script>",
    '<script>const sample = "const API_BASE = \'http://localhost:3000\';";</script>',
))
def test_preview_guard_ignores_offline_pages_and_display_examples(fragment: str) -> None:
    result = _validate_requested_web_preview(
        bundle({
            "preview.html": f"<!doctype html><html><body>{fragment}</body></html>",
            "README.md": 'fetch("http://localhost:3000/tasks");',
            "src/server.ts": "server.listen(3000, '127.0.0.1');",
        }),
        {"message": "Build a complete interactive website."},
    )
    assert result.passed is True


def test_preview_guard_is_scoped_to_requested_selected_entry() -> None:
    bad = '<!doctype html><html><input id="apiBase" value="http://localhost:3000"></html>'
    assert _validate_requested_web_preview(
        bundle({"preview.html": bad}), {"message": "Build a CLI with documentation examples."},
    ).passed
    assert _validate_requested_web_preview(
        bundle({"preview.html": bad, "dist/index.html": "<!doctype html><html>Offline</html>"}),
        {"message": "Build a complete interactive website."},
    ).passed


@pytest.mark.parametrize("case_id", ("small:auto", "medium:multi_agent", "ultra:multi_agent"))
@pytest.mark.parametrize("delivery", ("replacement", "incremental", "forced_replacement"))
def test_preview_repair_contract_survives_bounded_messages(case_id: str, delivery: str) -> None:
    source = bundle({
        "src/main.ts": "export const value = 1;\n" * 100,
        "preview.html": "<!doctype html><html>Offline</html>",
    })
    repair = _deliverable_repair_body(
        {"message": "Build a complete interactive website. " + "Original requirement. " * 400},
        case_id,
        benchmark_kind="capability",
        source_workspace_bundle=source if delivery != "replacement" else None,
        force_workspace_replacement=delivery == "forced_replacement",
        failed_reasons=("requirements: preview API default uses loopback " + "evidence " * 400,),
    )

    message = str(repair["message"])
    lean_repair = _deliverable_repair_body(
        {"message": "Build a complete interactive website."},
        case_id, benchmark_kind="capability",
        source_workspace_bundle=source if delivery != "replacement" else None,
        force_workspace_replacement=delivery == "forced_replacement",
    )
    # Only bounded external sections may grow beyond the complete fixed contract.
    assert len(message) <= max(6_000, len(str(lean_repair["message"]))) + 1_100 + 800
    assert repair["replace_workspace_files"] is (delivery != "incremental")
    for requirement in (
        "same-origin", "actual backend", "root-relative", "empty string", "allow empty",
        "localhost", "external API", "static previews offline",
    ):
        assert requirement in message
    assert " ".join(PROJECT_SCALE_VERIFICATION_REPORT_GUIDANCE.split()) in message
    assert "Previous failed evidence:" in message
    if case_id.startswith("medium:"):
        assert "Reference validation order is frozen" in message
        assert "before validating unrelated fields" in message
    if case_id.startswith("ultra:"):
        assert " ".join(PROJECT_ULTRA_LOAD_GUIDANCE.split()) in message
        assert "Dependency lifecycle is exact" in message
        assert "GET /portfolio/read-model" in message
    if case_id.endswith(":multi_agent"):
        assert "normalized agent_id values architect, implementer, tester, and synthesizer" in message


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
def test_preview_repair_contract_is_not_inferred_from_failure_text(benchmark_kind: str) -> None:
    repair = _deliverable_repair_body(
        {"message": "Build a command-line event processor."}, "small:direct",
        benchmark_kind=benchmark_kind,
        failed_reasons=("tests/preview.html failed",),
    )
    assert "Preview API contract:" not in str(repair["message"])


def test_fixture_preview_repair_includes_contract_before_bounded_original_request() -> None:
    repair = _deliverable_repair_body(
        {"message": "Build a complete interactive website. " + "requirement " * 500},
        "small:direct", benchmark_kind="fixture",
    )
    message = str(repair["message"])
    assert "same-origin" in message
    assert "root-relative" in message
    assert "static previews offline" in message
    assert len(message) <= 6_000
