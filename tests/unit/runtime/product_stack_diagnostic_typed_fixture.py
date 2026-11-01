"""Typed publication callbacks and intentional invalid-preview input boundary."""

from typing import TypedDict

from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_project_reports import PythonProjectFailure
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.files.generated import GeneratedFileStore
from agent_hub.runs.project_verification_receipts import ProjectVerificationReceiptRepository
from tests.unit.capabilities.durable_capture_typed_fixture import Case
from tests.unit.capabilities.test_python_product_delivery import Spool

type ProductCase = tuple[Case, ProjectVerificationService, GeneratedFileStore, list[Spool]]


class ReceiptOption(TypedDict, total=False):
    receipt_repository: ProjectVerificationReceiptRepository


def error_with_untrusted_preview(preview: object, failure: PythonProjectFailure) -> RuntimeCapabilityError:
    # The actual RuntimeCapabilityError constructor, not a fixture check, must
    # reject an object that impersonates a native diagnostic.
    constructor: object = RuntimeCapabilityError
    assert callable(constructor)
    result: object = constructor('failed', execution_diagnostic=preview, python_project_failure=failure)
    assert isinstance(result, RuntimeCapabilityError)
    return result
