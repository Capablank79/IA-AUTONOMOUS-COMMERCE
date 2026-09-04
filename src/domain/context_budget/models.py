"""
Modelos de dominio para el Presupuesto de Contexto (Context Budgeting - Hito M.2).

Transversal M — Control de Coste e Inferencia (Hito M.2).

M.2 responde:
"¿Cuánto contexto puede consumir esta inferencia y cómo evitamos exceder ese presupuesto?"

Define:
- ContextBudgetStatus: Estados de decisión presupuestaria (WITHIN_BUDGET, OVER_BUDGET, UNKNOWN, ERROR).
- BudgetExclusionReason: Códigos estructurados de desborde o fallo presupuestario (INPUT_TOO_LARGE, OUTPUT_RESERVATION_EXCEEDED, MODEL_CONTEXT_UNKNOWN, TOKEN_ESTIMATE_UNKNOWN, INVALID_PARAMETERS).
- InputTokensBreakdown: Desglose inmutable y estructurado de componentes de tokens de entrada (system, user, memory, tools, evidence, history, other).
- ContextBudgetPolicy: Política inmutable y versionada para la reserva y márgenes de seguridad de contexto.
- ContextBudgetRequest: Solicitud inmutable de evaluación de presupuesto de contexto.
- ContextBudgetDecision: Evaluación inmutable y determinista del presupuesto de contexto con checksum auditable.

Principios M.2:
- Aritmética explícita en números enteros (int, NUNCA float para conteo de tokens).
- Regla canónica:
    available_input = context_window - reserved_output - safety_margin
    requested_input <= available_input -> WITHIN_BUDGET
    requested_input > available_input -> OVER_BUDGET
- No valores negativos silenciosos: si available_input < 0 -> OVER_BUDGET con OUTPUT_RESERVATION_EXCEEDED.
- UNKNOWN context window o UNKNOWN token count -> UNKNOWN / ERROR, NUNCA WITHIN_BUDGET (UNKNOWN != safe).
- NO truncar silenciosamente, NO comprimir (M.3), NO cachear (M.4), NO seleccionar modelo económico (M.5/M.6).
- Sanitización estricta de secretos y no persistencia de Chain-of-Thought ni prompts privados.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Union

from src.domain.model_routing.models import sanitize_routing_data, deep_freeze, ModelRoute, RoutingDecision


class ContextBudgetStatus(str, Enum):
    """Estados canónicos de evaluación de presupuesto de contexto."""
    WITHIN_BUDGET = "WITHIN_BUDGET"
    OVER_BUDGET = "OVER_BUDGET"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class BudgetExclusionReason(str, Enum):
    """Códigos estructurados de justificación para OVER_BUDGET, UNKNOWN o ERROR."""
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    OUTPUT_RESERVATION_EXCEEDED = "OUTPUT_RESERVATION_EXCEEDED"
    MODEL_CONTEXT_UNKNOWN = "MODEL_CONTEXT_UNKNOWN"
    TOKEN_ESTIMATE_UNKNOWN = "TOKEN_ESTIMATE_UNKNOWN"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    SAFETY_MARGIN_EXCEEDED = "SAFETY_MARGIN_EXCEEDED"


@dataclass(frozen=True)
class InputTokensBreakdown:
    """
    Desglose inmutable de los componentes del input para token accounting.
    Todos los valores son enteros no negativos (int).
    """
    system_instructions: int = 0
    user_input: int = 0
    memory_context: int = 0
    tool_schemas: int = 0
    retrieved_evidence: int = 0
    conversation_history: int = 0
    other: int = 0

    def __post_init__(self):
        for field_name in (
            "system_instructions",
            "user_input",
            "memory_context",
            "tool_schemas",
            "retrieved_evidence",
            "conversation_history",
            "other",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, int) or isinstance(val, bool):
                raise ValueError(f"{field_name} must be an integer")
            if val < 0:
                raise ValueError(f"{field_name} cannot be negative")

    @property
    def total_input_tokens(self) -> int:
        return (
            self.system_instructions
            + self.user_input
            + self.memory_context
            + self.tool_schemas
            + self.retrieved_evidence
            + self.conversation_history
            + self.other
        )


@dataclass(frozen=True)
class ContextBudgetPolicy:
    """
    Política declarativa, inmutable y versionada para asignación de presupuesto de contexto.
    """
    policy_id: str
    version: str = "1.0.0"
    default_reserved_output_tokens: int = 1024
    safety_margin_tokens: int = 256
    description: str = "Default deterministic context budget policy"

    def __post_init__(self):
        if not self.policy_id or not self.policy_id.strip():
            raise ValueError("policy_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("version cannot be empty")
        if not isinstance(self.default_reserved_output_tokens, int) or isinstance(self.default_reserved_output_tokens, bool):
            raise ValueError("default_reserved_output_tokens must be an integer")
        if self.default_reserved_output_tokens < 0:
            raise ValueError("default_reserved_output_tokens cannot be negative")
        if not isinstance(self.safety_margin_tokens, int) or isinstance(self.safety_margin_tokens, bool):
            raise ValueError("safety_margin_tokens must be an integer")
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens cannot be negative")


@dataclass(frozen=True)
class ContextBudgetRequest:
    """
    Solicitud inmutable para evaluar el presupuesto de contexto de una inferencia.
    """
    route: Union[ModelRoute, RoutingDecision, str]
    requested_input_tokens: Optional[int] = None
    input_breakdown: Optional[InputTokensBreakdown] = None
    reserved_output_tokens: Optional[int] = None
    safety_margin_tokens: Optional[int] = None
    context_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.requested_input_tokens is not None:
            if not isinstance(self.requested_input_tokens, int) or isinstance(self.requested_input_tokens, bool):
                raise ValueError("requested_input_tokens must be an integer")
            if self.requested_input_tokens < 0:
                raise ValueError("requested_input_tokens cannot be negative")

        if self.reserved_output_tokens is not None:
            if not isinstance(self.reserved_output_tokens, int) or isinstance(self.reserved_output_tokens, bool):
                raise ValueError("reserved_output_tokens must be an integer")
            if self.reserved_output_tokens < 0:
                raise ValueError("reserved_output_tokens cannot be negative")

        if self.safety_margin_tokens is not None:
            if not isinstance(self.safety_margin_tokens, int) or isinstance(self.safety_margin_tokens, bool):
                raise ValueError("safety_margin_tokens must be an integer")
            if self.safety_margin_tokens < 0:
                raise ValueError("safety_margin_tokens cannot be negative")

        clean_meta = sanitize_routing_data(self.context_metadata)
        object.__setattr__(self, "context_metadata", deep_freeze(clean_meta))


@dataclass(frozen=True)
class ContextBudgetDecision:
    """
    Resultado inmutable y determinista de la evaluación del presupuesto de contexto (M.2).
    """
    status: ContextBudgetStatus
    route_id: Optional[str]
    model_id: Optional[str]
    context_window: Optional[int]
    requested_input_tokens: Optional[int]
    reserved_output_tokens: int
    safety_margin_tokens: int
    available_input_tokens: Optional[int]
    estimated_total_tokens: Optional[int]
    reason_code: Optional[BudgetExclusionReason] = None
    rationale: str = ""
    policy_id: str = "default_context_budget_policy"
    policy_version: str = "1.0.0"
    input_breakdown: Optional[InputTokensBreakdown] = None
    evaluation_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_within_budget(self) -> bool:
        return self.status == ContextBudgetStatus.WITHIN_BUDGET

    def calculate_checksum(self) -> str:
        """Calcula hash SHA-256 canónico de la decisión para auditoría y reproducibilidad."""
        payload = {
            "status": self.status.value,
            "route_id": self.route_id,
            "model_id": self.model_id,
            "context_window": self.context_window,
            "requested_input_tokens": self.requested_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "available_input_tokens": self.available_input_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "reason_code": self.reason_code.value if self.reason_code else None,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "rationale": self.rationale,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
