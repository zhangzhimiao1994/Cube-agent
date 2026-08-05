"""Model registry and transport contracts."""

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityLease,
    CapacityPool,
    CapacityUnavailable,
    CredentialDescriptor,
    CredentialRegistry,
    safe_operational_limit,
)
from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    ModelGateway,
    ModelGatewayError,
    TokenEstimator,
)
from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelResponseError,
    ModelTransportError,
)
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredResponseSchema,
    TokenUsage,
    ToolCall,
)

__all__ = [
    "CapacityBackendError",
    "CapacityLease",
    "CapacityPool",
    "CapacityUnavailable",
    "ConservativeTokenEstimator",
    "CredentialDescriptor",
    "CredentialRegistry",
    "Deployment",
    "LiteLLMClient",
    "ModelCapability",
    "ModelGateway",
    "ModelGatewayError",
    "ModelMessage",
    "ModelRegistry",
    "ModelRequest",
    "ModelResponse",
    "ModelResponseError",
    "ModelTransportError",
    "NoCapableDeployment",
    "StructuredResponseSchema",
    "TokenEstimator",
    "TokenUsage",
    "ToolCall",
    "safe_operational_limit",
]
