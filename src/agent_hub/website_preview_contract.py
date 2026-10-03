"""Shared, side-effect-free generation guidance for website previews."""

WEBSITE_PREVIEW_API_GUIDANCE = (
    "Preview API contract: For an API-backed preview, use same-origin requests to the actual "
    "backend's root-relative routes (for example fetch('/tasks')); do not invent an /api prefix. "
    "Default API Base to an empty string, allow empty input, and never fall back to localhost, "
    "127.0.0.1, [::1], or any external API. Do not require users to edit API Base before the main "
    "flow works. Keep static previews offline with no network dependency."
)
