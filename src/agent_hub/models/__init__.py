"""Model registry and transport contracts."""

from agent_hub.models.capabilities import infer_model_capabilities
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityLease,
    CapacityPool,
    CapacityQueueFull,
    CapacityUnavailable,
    CapacityWaitTimeout,
    CredentialDescriptor,
    CredentialRegistry,
    safe_operational_limit,
)
from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    DeploymentPricing,
    GatewayCompletion,
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
    "CapacityConfigurationError",
    "CapacityLease",
    "CapacityPool",
    "CapacityQueueFull",
    "CapacityUnavailable",
    "CapacityWaitTimeout",
    "ConservativeTokenEstimator",
    "CredentialDescriptor",
    "CredentialRegistry",
    "Deployment",
    "DeploymentPricing",
    "GatewayCompletion",
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
    "infer_model_capabilities",
    "safe_operational_limit",
]
