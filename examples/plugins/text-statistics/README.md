# Text Statistics Plugin

This first-party example proves the executable plugin path end to end. The
`text.statistics` capability runs as a signed Python package in the configured
`local_process` isolation backend and has no network, filesystem, or third-party
dependency requirements.

`plugin.json` contains a placeholder signature. Build and sign the distributable
archive with `agent_hub.plugins.package_builder.build_signed_plugin_archive`
before installation. Never install the source manifest directly.
