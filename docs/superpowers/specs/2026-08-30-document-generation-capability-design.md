# Document Generation Capability Design

## Status

Approved for implementation under the user's standing instruction to continue without repeated confirmation.

## Problem

Mofang Agent can currently produce text artifacts and can accept `.docx`/`.pptx` as attachments, but it cannot reliably generate real Word or PowerPoint files. A Markdown answer with a filename is not sufficient. Users need downloadable Office files that work after a fresh clone or Docker image build, including offline use.

## Scope

Phase 18 adds a first production-grade document generation slice:

- Generate real `.docx` files.
- Generate real `.pptx` files.
- Include professional PPT template support instead of only a blank default deck.
- Expose generated files as run artifacts with filename, MIME type, size, SHA-256, and download URL.
- Let main-agent and sub-agent runs call the same capability through the existing capability gateway.
- Keep the implementation deterministic and offline-capable.

Out of scope for this slice:

- Importing arbitrary user PPTX templates.
- Editing existing Office documents.
- Native Google Docs/Slides export.
- Rich chart/image generation inside slides.
- Cloud template marketplace integration.

## Architecture

Document generation is a built-in runtime tool capability, not a model capability. The model decides content and can call the tool; the backend owns validation, file creation, storage, artifact metadata, and download.

The system adds:

1. `agent_hub.documents`: deterministic OOXML builders and template definitions.
2. Runtime capability names:
   - `document.generate_docx`
   - `presentation.generate_pptx`
3. A tenant/run-scoped generated artifact file store under the configured runtime data directory.
4. Run artifact projection fields for downloadable files.
5. Frontend file artifact cards in chat, run detail, and sub-agent work detail views.

## PPT Template Model

PPT generation must not rely on a free-form model writing raw PPTX XML. The model or child agent provides a structured blueprint:

- `template_id`
- `title`
- optional `subtitle`
- `slides[]`
- optional theme hints

The renderer selects a validated template and produces the deck.

First built-in templates:

- `consulting-clean`: white consulting style, strong headings, section dividers, status grids.
- `technical-blueprint`: technical/product style, blue accents, architecture/process slides.
- `dark-launch`: dark launch/roadmap style, strong cover, high-contrast summary slides.

Each template is represented by deterministic tokens: colors, fonts, title/body sizes, margins, footer style, and allowed slide layouts. The default template is `consulting-clean`.

## DOCX Model

DOCX generation receives:

- `title`
- optional `subtitle`
- `sections[]`, each with a heading and paragraphs/bullets
- optional `metadata`

The first slice uses a compact business-report style with explicit Word styles, headings, bullets, and document properties. It produces a real OOXML package using only Python standard library `zipfile` and XML escaping.

## Storage and Security

Generated binary files are not stored inside artifact JSON. Artifact JSON stores safe metadata:

- `filename`
- `mime_type`
- `size_bytes`
- `sha256`
- `storage_key`
- `download_url`

The file store resolves storage keys under a tenant/run/artifact scoped root and rejects path traversal. Download endpoints enforce run ownership and tenant scoping before returning `FileResponse`.

## API and UI

Admin run detail artifacts gain optional fields:

- `filename`
- `mime_type`
- `size_bytes`
- `sha256`
- `download_url`

The frontend renders these as file cards:

- badge: `DOCX` or `PPTX`
- title
- filename and size
- download button/link

Text artifacts continue to render as chat text. File artifacts do not pretend to be message text.

## Routing

Do not add `docx_generation` or `pptx_generation` to `ModelCapability`. Office generation is a tool capability. Document/presentation roles should request the tool when the user asks for Word, DOCX, PPT, PPTX, PowerPoint, presentation, slides, deck, 汇报材料, 文档, or 演示文稿.

Models used by those roles still need normal `tool_calling` support when the runtime calls tools.

## Verification

Required local gates:

- Unit tests for DOCX and PPTX builders open the output as zip packages and verify required OOXML parts, visible text, template markers, MIME types, and no unsafe paths.
- Capability gateway tests verify successful DOCX/PPTX generation, bad input rejection, replay-safe availability, and safe artifact metadata.
- API tests verify artifact metadata projection and tenant/run-scoped download.
- Frontend tests verify DOCX/PPTX cards and download URLs in chat/run detail/workforce drawer.
- Existing backend unit/API/contract tests, frontend lint/test/build, and `git diff --check`.

Production verification:

- Submit one direct DOCX generation task and one PPTX generation task.
- Download both files from the production API.
- Verify each file is a valid zip package and contains expected Office content types.
- Clean probe runs and temporary files after verification.
