# Text Statistics Plugin

This first-party example proves the executable plugin path end to end. The
`text.statistics` capability runs as a signed Python package in the configured
`local_process` isolation backend and has no network, filesystem, or third-party
dependency requirements.

`plugin.json` contains a placeholder signature. Build and sign the distributable
archive with `agent_hub.plugins.package_builder.build_signed_plugin_archive`
before installation. Never install the source manifest directly.

## Package SDK 1.0 stdin/stdout

The package runner sends one UTF-8 JSON object on stdin and closes the pipe.
Its seven top-level fields are shown below. Configuration objects can be nonempty.
`schema_version` is the integer `1`; the manifest's `sdk_api_version` is `"1.0"`.

```json
{
  "schema_version": 1,
  "plugin_id": "text-statistics",
  "capability_id": "text.statistics",
  "arguments": {"text": "Cube Agent"},
  "resource_config": {"zone": "utc"},
  "capability_config": {"mode": "count"},
  "context": {
    "tenant_id": "00000000-0000-4000-8000-000000000001",
    "user_id": "00000000-0000-4000-8000-000000000002",
    "run_id": "00000000-0000-4000-8000-000000000003",
    "actor": "tester",
    "idempotency_key": "statistics-1"
  }
}
```

Dispatch on `capability_id` and apply the capability's business `input_schema`
only to `arguments`. The example rejects extra business arguments, while accepting
and ignoring unused metadata, including extra envelope or context fields. Do not
restrict the top-level key set to only `capability_id` and `arguments`.

Write exactly one business JSON object matching `output_schema` to stdout, without
a `result` wrapper, logs, Markdown, or surrounding prose. Send diagnostics to
stderr. These streams are local process pipes, not network requests. The canonical
stdin schema is `plugin_package_stdin_schema()` in
`src/agent_hub/plugins/contracts.py`; the same module supplies generation guidance
to the product's Direct and Crew prompt builders.
