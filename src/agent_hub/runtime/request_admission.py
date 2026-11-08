"""Pure request planning only. No capacity lease, billing authority or context relabeling."""
from __future__ import annotations

from dataclasses import replace

from agent_hub.models.capacity import CapacityRequestTooLarge
from agent_hub.models.gateway import ConservativeTokenEstimator
from agent_hub.models.types import ModelRequest


def admission_token_limit(gateway: object, request: ModelRequest) -> int | None:
    query = getattr(gateway, 'admission_token_limit', None)
    limit = query(request) if callable(query) else None
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError('invalid model admission limit')
    return limit


def input_tokens(gateway: object, request: ModelRequest) -> int:
    query = getattr(gateway, 'estimate_input_tokens', None)
    value = query(request) if callable(query) else ConservativeTokenEstimator().estimate_input(request)
    if type(value) is not int or value <= 0:
        raise ValueError('invalid model input estimate')
    return value


def request_tokens(gateway: object, request: ModelRequest) -> int:
    query = getattr(gateway, 'estimate_request_tokens', None)
    value = query(request) if callable(query) else input_tokens(gateway, request) + request.max_output_tokens
    if type(value) is not int or value <= 0:
        raise ValueError('invalid model request estimate')
    return value


def input_target(context_window: int | None, admission_limit: int | None) -> int | None:
    """Compile two separate limits into a prompt target with the existing 1/8 reserve."""
    bounds = [value for value in (context_window, admission_limit) if value is not None]
    if any(type(value) is not int or value <= 0 for value in bounds):
        raise ValueError('invalid model request budget')
    return min(value - max(1, value // 8) for value in bounds) if bounds else None


def fit_output(gateway: object, request: ModelRequest) -> ModelRequest:
    """Lower only an optional output reservation; do not alter messages or acquire."""
    limit = admission_token_limit(gateway, request)
    if limit is None or request_tokens(gateway, request) <= limit:
        return request
    low, high = 1, request.max_output_tokens
    fitted: ModelRequest | None = None
    while low <= high:
        cap = (low + high) // 2
        candidate = replace(request, max_output_tokens=cap)
        if request_tokens(gateway, candidate) <= limit:
            fitted = candidate
            low = cap + 1
        else:
            high = cap - 1
    if fitted is None:
        raise CapacityRequestTooLarge()
    return fitted
