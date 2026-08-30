# Skills and MCP

Skills are uploaded, scanned, quarantined, then approved. Requested permissions are displayed before enabling.

MCP servers expose health and allowed tools in the admin console. Dangerous tools should require explicit approval before use.

Built-in runtime tools are governed through the same capability boundary, but they are not model capability tags. Office generation uses `document.generate_docx` for Word/DOCX files and `presentation.generate_pptx` for PowerPoint/PPTX files. These tools use deterministic standard-library OOXML builders and store generated files under `generated_artifact_dir`, where run details, conversation output, and child-agent work seats can render authenticated download file cards.
