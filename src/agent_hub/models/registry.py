from collections import defaultdict
from collections.abc import Iterable
from types import MappingProxyType

from agent_hub.models.types import Deployment, ModelCapability, _require_safe_identifier


class NoCapableDeployment(LookupError):
    """Raised when no configured deployment meets a request's requirements."""


class ModelRegistry:
    """Deterministic logical-model lookup without capacity or health policy."""

    def __init__(self, deployments: Iterable[Deployment]) -> None:
        ordered = tuple(sorted(deployments, key=lambda deployment: deployment.id))
        seen: set[str] = set()
        grouped: defaultdict[str, list[Deployment]] = defaultdict(list)
        for deployment in ordered:
            if deployment.id in seen:
                raise ValueError(f"duplicate deployment id: {deployment.id!r}")
            seen.add(deployment.id)
            grouped[deployment.logical_model].append(deployment)
        self._by_logical_model = MappingProxyType({
            logical_model: tuple(candidates)
            for logical_model, candidates in sorted(grouped.items())
        })

    def candidates(
        self,
        logical_model: str,
        required: Iterable[ModelCapability] = (),
    ) -> tuple[Deployment, ...]:
        _require_safe_identifier("logical_model", logical_model)
        required_capabilities = frozenset(ModelCapability(item) for item in required)
        matches = tuple(
            deployment
            for deployment in self._by_logical_model.get(logical_model, ())
            if required_capabilities.issubset(deployment.capabilities)
        )
        if matches:
            return matches
        capabilities = ", ".join(sorted(required_capabilities)) or "none"
        raise NoCapableDeployment(
            f"no capable deployment for logical model {logical_model!r}: {capabilities}"
        )
