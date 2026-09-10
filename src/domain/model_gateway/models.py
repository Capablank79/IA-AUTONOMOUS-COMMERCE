"""
Modelos de dominio para el Model Gateway SaaS (Hito O.5 — SaaS / Platformization).

Define:
- ModelGatewayStatus: Estados canónicos de respuesta de inferencia SaaS.
- ProviderErrorType: Clasificación normalizada de errores de proveedor.
- ProviderRequestReference: Referencia inmutable a la invocación del proveedor.
- TenantModelConfig: Configuración inmutable de modelos y proveedores por tenant.
- ModelGatewayRequest: Solicitud inmutable de inferencia gobernada para un tenant.
- ModelGatewayContext: Contexto inmutable de ejecución del gateway SaaS.
- ModelGatewayResponse: Respuesta estructurada e inmutable del gateway SaaS con facts de token/coste.
- Excepciones de dominio para el Model Gateway SaaS.

Principios O.5:
1. Responde a: "¿Cómo invoca un tenant modelos de IA de forma aislada, autorizada y gobernada sin acceder directamente al proveedor?".
2. Pipeline orquestado:
   SaaS Session (O.3) -> O.4 Authorization -> TenantContext (O.1) -> Model Gateway (O.5)
   -> M.5 Task Requirements -> M.1 Routing -> M.2 Budget -> M.3 Compression -> M.6 Cost Policy
   -> M.4 Cache -> N.9 Sensitive Data -> N.5 Credential Resolution -> Provider Adapter -> K.1/K.2/K.3.
3. REUSE > EXTEND > CREATE: No duplica routers, cost policies, caching, budgeting ni compression engines.
4. Tenant Isolation:
   - Toda solicitud DEBE tener TenantContext válido y SaaSSession activa.
   - Tenant A nunca puede usar credenciales ni configuraciones de Tenant B.
   - Cache key contiene tenant_id y security context (sin cross-tenant HIT).
5. Autorización O.4: MODEL_INFERENCE_EXECUTE obligatoria antes de invocar proveedor.
6. Cero secretos o credenciales en modelos, respuestas, logs, auditoría o excepciones.
7. Emisión de hechos estructurados de tokens y coste para futuras O.6/O.7 sin almacenar métricas agregadas ni aplicar quotas de tenant.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Union, Sequence, Dict, Set
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.model_routing.models import (
    ModelRoute,
    RouteCapability,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RoutingDecision,
)
from src.domain.model_selection.models import (
    StandardTaskType,
    TaskModelProfile,
    TaskSelectionRequirements,
    ModelSelectionResult,
)
from src.domain.context_budget.models import (
    ContextBudgetDecision,
    ContextBudgetStatus,
    InputTokensBreakdown,
)
from src.domain.prompt_compression.models import (
    CompressionResult,
    CompressionStatus,
    ContextItem,
)
from src.domain.caching.models import (
    CacheLookupResult,
    CacheLookupStatus,
)
from src.domain.cost_aware_policy.models import (
    CostAwareDecision,
    CostAwareDecisionStatus,
)
from src.domain.secrets.models import (
    SecretReference,
)
from src.domain.session.models import SaaSSession, SessionContext
from src.domain.tenant.models import TenantContext


class ModelGatewayStatus(str, Enum):
    """Estados canónicos de la respuesta del Model Gateway SaaS."""
    SUCCESS = "SUCCESS"
    CACHED = "CACHED"
    UNAUTHORIZED = "UNAUTHORIZED"
    TENANT_INVALID = "TENANT_INVALID"
    FORBIDDEN_MODEL = "FORBIDDEN_MODEL"
    NO_ROUTE = "NO_ROUTE"
    OVER_BUDGET = "OVER_BUDGET"
    COST_REJECTED = "COST_REJECTED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    CREDENTIAL_ERROR = "CREDENTIAL_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    SENSITIVE_DATA_BLOCKED = "SENSITIVE_DATA_BLOCKED"
    TOOL_POLICY_BLOCKED = "TOOL_POLICY_BLOCKED"
    ERROR = "ERROR"


class ProviderErrorType(str, Enum):
    """Tipos normalizados de error de proveedor de modelos (sin filtrar payload sensible)."""
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNKNOWN = "UNKNOWN"


class ModelGatewayError(Exception):
    """Excepción base de dominio para fallos del Model Gateway SaaS."""
    pass


class ModelGatewaySecurityError(ModelGatewayError):
    """Excepción para violaciones de seguridad o aislamiento multi-tenant."""
    pass


class ModelGatewayProviderError(ModelGatewayError):
    """Excepción normalizada para fallos en la interacción con el proveedor."""
    def __init__(self, error_type: ProviderErrorType, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(f"[{error_type.value}] {message}")
        self.error_type = error_type
        self.message = message
        self.details = deep_freeze(sanitize_security_data(details or {}))


@dataclass(frozen=True)
class TenantModelConfig:
    """
    Configuración inmutable de modelos y proveedores por tenant (Hito O.5).

    Define qué proveedores y modelos tiene permitidos un tenant, rutas preferidas,
    referencias a credenciales seguras (SecretReference N.5) y si se permite fallback.
    """
    tenant_id: str
    allowed_providers: Tuple[str, ...] = ("openai", "anthropic", "gemini", "mock")
    allowed_models: Tuple[str, ...] = field(default_factory=tuple)  # Vacío = todos los disponibles en allowed_providers
    preferred_route_id: Optional[str] = None
    credential_references: Mapping[str, SecretReference] = field(default_factory=dict)  # provider_name -> SecretReference
    allow_fallback: bool = True
    max_budget_tokens: Optional[int] = None
    max_cost_limit: Optional[Decimal] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        object.__setattr__(self, "allowed_providers", tuple(str(p).lower().strip() for p in self.allowed_providers))
        if self.allowed_models:
            object.__setattr__(self, "allowed_models", tuple(str(m).strip() for m in self.allowed_models))
        else:
            object.__setattr__(self, "allowed_models", tuple())

        # Inmutabilidad de mappings
        object.__setattr__(self, "credential_references", deep_freeze(self.credential_references))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))

    def is_provider_allowed(self, provider: str) -> bool:
        """Verifica si un proveedor está explícitamente permitido para este tenant."""
        if not provider:
            return False
        return str(provider).lower().strip() in self.allowed_providers

    def is_model_allowed(self, model_id: str) -> bool:
        """Verifica si un modelo específico está permitido para este tenant."""
        if not model_id:
            return False
        if not self.allowed_models:
            return True  # Si no hay lista restrictiva de modelos, rige allowed_providers
        return str(model_id).strip() in self.allowed_models


@dataclass(frozen=True)
class ProviderRequestReference:
    """
    Referencia inmutable a una petición emitida hacia un proveedor LLM.
    Nunca contiene credenciales, API keys ni PII en texto claro.
    """
    provider_name: str
    model_id: str
    endpoint: Optional[str] = None
    request_timestamp: Optional[datetime] = None
    correlation_id: str = field(default_factory=lambda: f"prov_req_{uuid.uuid4().hex[:12]}")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class ModelGatewayRequest:
    """
    Petición inmutable de inferencia gobernada para el Model Gateway SaaS.

    Transporta:
    - tenant_id: Identificador del tenant solicitante (validado contra sesión O.3/O.4).
    - session_id: Identificador de la sesión SaaS activa (O.3).
    - task_type: Tipo canónico de tarea (StandardTaskType o string).
    - prompt_payload: Representación segura del prompt/contexto (str o dict).
    - context_items: Componentes estructurados de contexto para M.2 y M.3 (opcional).
    - required_capabilities: Capacidades obligatorias requeridas (RouteCapability).
    - preferred_model_id: Preferencia de modelo sugerida por el caller (opcional, sujeta a policy).
    - preferred_provider: Preferencia de proveedor sugerida por el caller (opcional, sujeta a policy).
    - tools_requested: Herramientas que el modelo podría invocar (sujetas a N.8 Tool Policy).
    - session: Objeto SaaSSession opcional si ya fue pre-resuelto.
    - session_context: Objeto SessionContext opcional si ya fue pre-resuelto.
    - correlation_id: Identificador de correlación para trazas y auditoría.
    - policy_version: Versión de la política a aplicar.
    - metadata: Metadatos adicionales sanitizados.
    """
    tenant_id: str
    session_id: Optional[str] = None
    task_type: Union[StandardTaskType, str] = StandardTaskType.MARKET_DISCOVERY.value
    prompt_payload: Union[str, Mapping[str, Any]] = ""
    context_items: Tuple[ContextItem, ...] = field(default_factory=tuple)
    required_capabilities: Tuple[RouteCapability, ...] = field(default_factory=tuple)
    preferred_model_id: Optional[str] = None
    preferred_provider: Optional[str] = None
    tools_requested: Tuple[str, ...] = field(default_factory=tuple)
    session: Optional[SaaSSession] = None
    session_context: Optional[SessionContext] = None
    organization_id: Optional[str] = None
    identity_id: Optional[str] = None
    correlation_id: str = field(default_factory=lambda: f"gw_req_{uuid.uuid4().hex[:12]}")
    policy_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        task_str = self.task_type.value if isinstance(self.task_type, StandardTaskType) else str(self.task_type)
        object.__setattr__(self, "task_type", task_str)
        object.__setattr__(self, "context_items", tuple(self.context_items))
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))
        object.__setattr__(self, "tools_requested", tuple(self.tools_requested))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class ModelGatewayContext:
    """
    Contexto inmutable de evaluación y ejecución dentro del Model Gateway SaaS.
    Reúne los resultados de cada etapa del pipeline sin mutaciones.
    """
    request: ModelGatewayRequest
    tenant_context: Optional[TenantContext] = None
    tenant_config: Optional[TenantModelConfig] = None
    session: Optional[SaaSSession] = None
    task_selection_result: Optional[ModelSelectionResult] = None
    routing_decision: Optional[RoutingDecision] = None
    budget_decision: Optional[ContextBudgetDecision] = None
    compression_result: Optional[CompressionResult] = None
    cost_decision: Optional[CostAwareDecision] = None
    cache_lookup_result: Optional[CacheLookupResult] = None
    allowed_tools: Tuple[str, ...] = field(default_factory=tuple)
    correlation_id: str = field(default_factory=lambda: f"gw_ctx_{uuid.uuid4().hex[:12]}")

    def __post_init__(self):
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))


@dataclass(frozen=True)
class ModelGatewayResponse:
    """
    Respuesta inmutable y estructurada del Model Gateway SaaS.

    Garantías:
    - Nunca expone credenciales ni API keys de ningún tenant ni proveedor.
    - Emite hechos estructurados de tokens y costo (input_tokens, output_tokens, total_tokens, estimated_cost, actual_cost)
      para que futuras fases (O.6 / O.7 / O.9) puedan consumirlos sin que O.5 implemente métricas agregadas ni límites de cuota.
    - Contiene trazabilidad completa de ruta, proveedor, estado de caché y diagnóstico seguro.
    """
    status: ModelGatewayStatus
    tenant_id: str
    output_content: Optional[str] = None
    output_structured: Optional[Mapping[str, Any]] = None
    route_used: Optional[ModelRoute] = None
    provider_reference: Optional[ProviderRequestReference] = None
    cache_status: CacheLookupStatus = CacheLookupStatus.MISS

    # Token & Cost Facts (para O.6 / O.7)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: Optional[Decimal] = None
    actual_cost: Optional[Decimal] = None

    # Razones y diagnósticos seguros
    reason_code: Optional[str] = None
    error_type: Optional[ProviderErrorType] = None
    error_message: Optional[str] = None
    correlation_id: str = field(default_factory=lambda: f"gw_res_{uuid.uuid4().hex[:12]}")
    policy_version: str = "1.0.0"
    safe_diagnostics: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        if self.output_structured is not None:
            object.__setattr__(self, "output_structured", deep_freeze(sanitize_security_data(self.output_structured)))
        object.__setattr__(self, "safe_diagnostics", deep_freeze(sanitize_security_data(self.safe_diagnostics)))

        # Calcular checksum canónico SHA-256
        payload = {
            "status": self.status.value,
            "tenant_id": self.tenant_id,
            "cache_status": self.cache_status.value,
            "route_id": self.route_used.route_id if self.route_used else None,
            "provider": self.provider_reference.provider_name if self.provider_reference else None,
            "model": self.provider_reference.model_id if self.provider_reference else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": str(self.estimated_cost) if self.estimated_cost is not None else None,
            "actual_cost": str(self.actual_cost) if self.actual_cost is not None else None,
            "reason_code": self.reason_code,
            "correlation_id": self.correlation_id,
            "policy_version": self.policy_version,
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        object.__setattr__(self, "checksum", hashlib.sha256(canonical.encode("utf-8")).hexdigest())
