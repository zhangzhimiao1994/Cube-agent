"""Owned declaration rejection must enter draft repair before any effect freeze."""
import pytest

from agent_hub.capabilities.python_input_preparation import (
    ResolutionDeclarationChangedError,
    validate_dynamic_python_controls,
)
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST


@pytest.mark.parametrize(('manifest', 'lock', 'descriptor'), [
    (b'[project', LOCK, None),
    (MANIFEST, b'', None),
    (MANIFEST, LOCK, None),
], ids=['manifest-rejected', 'lock-rejected', 'descriptor-rejected'])
def test_control_rejection_has_the_owned_draft_repair_signal(
    manifest: bytes, lock: bytes, descriptor: object,
) -> None:
    with pytest.raises(ResolutionDeclarationChangedError) as caught:
        validate_dynamic_python_controls(manifest, lock, descriptor)
    assert type(caught.value) is ResolutionDeclarationChangedError
    assert caught.value.args == ('resolution_declaration_changed',)
