"""Prompt-only coverage of immutable original-source previews; never execution proof."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from agent_hub.runtime.contracts import Artifact

_NOTE = (
    "Source preview may omit files or clip text. source_context.files[].sha256/source_file_sha256 identifies the "
    "original mode-tagged snapshot file, not shown text. Shown/displayed text digests cover "
    "displayed UTF-8 only. Coverage is not execution or verification evidence."
)


def _source(artifact: Artifact) -> Mapping[str, object] | None:
    value = artifact.content.get('source_context')
    return value if artifact.type == 'original_project_observation' and isinstance(value, Mapping) else None


def _complete(source: Mapping[str, object]) -> bool:
    files = source.get('files')
    return (isinstance(files, (tuple, list)) and source.get('truncated') is False
            and type(source.get('file_count')) is int and source['file_count'] == len(files)
            and type(source.get('included_count')) is int and source['included_count'] == len(files))


def _notice(complete: bool, kind: str) -> dict[str, object]:
    return {'schema_version': 1, 'coverage': 'complete' if complete else 'partial',
            'displayed_kind': kind, 'snapshot_file_hash_semantics': 'mode_tagged_snapshot_file',
            'note': _NOTE}


def project_source_preview(
    artifact: Artifact, projected: object,
) -> tuple[object, dict[str, object] | None]:
    """Annotate AFTER leaf clipping, without changing the persisted v1 object."""
    source = _source(artifact)
    if source is None or not isinstance(projected, Mapping):
        return projected, None
    shown = projected.get('source_context')
    if not isinstance(shown, Mapping):
        return projected, _notice(False, 'source_context_files')
    original_files, shown_files = source.get('files'), shown.get('files')
    if not isinstance(original_files, (tuple, list)) or not isinstance(shown_files, (tuple, list)):
        return projected, _notice(False, 'source_context_files')
    complete = _complete(source) and len(original_files) == len(shown_files)
    files: list[object] = []
    for index, value in enumerate(shown_files):
        original = original_files[index] if index < len(original_files) else None
        if not isinstance(value, Mapping) or not isinstance(original, Mapping):
            files.append(value)
            complete = False
            continue
        row = dict(value)
        text = row.get('content')
        if type(text) is not str:
            files.append(row)
            complete = False
            continue
        clipped = text != original.get('content')
        row.pop('sha256', None)
        row.update(source_file_sha256=original.get('sha256'),
                   shown_text_bytes=len(text.encode('utf-8')),
                   shown_text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                   content_truncated=clipped)
        complete = complete and not clipped and row.get('path') == original.get('path')
        files.append(row)
    preview = {**shown, 'files': files, 'truncated': not complete}
    return {**projected, 'source_context': preview}, _notice(complete, 'source_context_files')


def source_preview_excerpt(
    artifact: Artifact, original_text: str, displayed_text: str,
) -> dict[str, object] | None:
    """Direct displays a bounded JSON string, so its digest covers that exact excerpt."""
    source = _source(artifact)
    if source is None:
        return None
    notice = _notice(_complete(source) and displayed_text == original_text, 'artifact_json_excerpt')
    notice.update(displayed_text_bytes=len(displayed_text.encode('utf-8')),
                  displayed_text_sha256=hashlib.sha256(displayed_text.encode('utf-8')).hexdigest())
    return notice
