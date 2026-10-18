from uuid import uuid4

import pytest

from agent_hub.runtime.contracts import Artifact


def artifact(text: str) -> Artifact:
    return Artifact(id=uuid4(), type='text', producer='worker', content={'text':text})


def test_text_artifact_preserves_original_crlf_bytes_and_hash() -> None:
    text = '```json\r\n{"summary":"original"}\r\n```'
    original = artifact(text)
    restored = Artifact.from_payload(original.to_payload())
    assert original.content['text'] == restored.content['text'] == text
    assert restored.content_sha256 == original.content_sha256
    assert artifact(text.replace('\r\n','\n')).content_sha256 != original.content_sha256


@pytest.mark.parametrize('text', ['bare\rreturn', 'double\r\r\nreturn', 'ansi\r\n\x1b[31m',
                                  'bidi\r\n\u202e', 'nul\r\n\x00'])
def test_crlf_does_not_admit_other_unsafe_controls(text: str) -> None:
    with pytest.raises(ValueError, match='unsafe control'):
        artifact(text)
