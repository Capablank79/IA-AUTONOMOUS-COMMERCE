"""
Modelos de dominio para Compresión Determinista de Prompt y Contexto (Prompt Compression - Hito M.3).

Transversal M — Control de Coste e Inferencia.

M.3 responde:
"Si el contexto excede el presupuesto, ¿cómo lo reducimos de forma determinista sin perder información crítica?"

Transforma:
OVER_BUDGET input -> compressed context -> nuevo token estimate -> WITHIN_BUDGET (si es posible)
sin ocultar qué fue removido/resumido ni destruir evidencia crítica.

Define:
- CompressionStatus: Estados canónicos (COMPRESSED, UNCHANGED, CANNOT_COMPRESS, UNKNOWN, ERROR).
- ContextComponentType: Tipos de componentes de contexto (SYSTEM_INSTRUCTIONS, USER_INPUT, MEMORY_CONTEXT, TOOL_SCHEMAS, RETRIEVED_EVIDENCE, CONVERSATION_HISTORY, OTHER).
- PriorityLevel: Niveles canónicos de prioridad (PROTECTED, HIGH_PRIORITY, NORMAL, LOW_PRIORITY, REMOVABLE).
- ContextItem: Unidad granular inmutable de contexto con prioridad, contenido y metadatos.
- CompressionActionType: Acciones deterministas registradas (DROP_DUPLICATES, PRUNE_OLDEST_HISTORY, LIMIT_OPTIONAL_EVIDENCE, COMPACT_STRUCTURED_JSON, REMOVE_LOW_PRIORITY, NO_OP).
- CompressionAction: Registro estructurado e inmutable de una acción de compresión aplicada.
- CompressionPolicy: Política inmutable y versionada que define prioridades, estrategias y límites.
- CompressionRequest: Solicitud inmutable de compresión.
- CompressionResult: Resultado inmutable, determinista y auditable con conteos originales y finales, componentes preservados y reducidos, y checksum SHA-256.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Sequence, Tuple, Union

from src.domain.context_budget.models import (
    ContextBudgetDecision,
    ContextBudgetStatus,
    InputTokensBreakdown,
)
from src.domain.model_routing.models import sanitize_routing_data, deep_freeze


class CompressionStatus(str, Enum):
    """Estados canónicos de resultado de compresión de contexto."""
    COMPRESSED = "COMPRESSED"
    UNCHANGED = "UNCHANGED"
    CANNOT_COMPRESS = "CANNOT_COMPRESS"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ContextComponentType(str, Enum):
    """Tipos canónicos de componentes de contexto alineados con M.2."""
    SYSTEM_INSTRUCTIONS = "SYSTEM_INSTRUCTIONS"
    USER_INPUT = "USER_INPUT"
    MEMORY_CONTEXT = "MEMORY_CONTEXT"
    TOOL_SCHEMAS = "TOOL_SCHEMAS"
    RETRIEVED_EVIDENCE = "RETRIEVED_EVIDENCE"
    CONVERSATION_HISTORY = "CONVERSATION_HISTORY"
    OTHER = "OTHER"


class PriorityLevel(str, Enum):
    """
    Niveles de prioridad para compresión determinista.
    PROTECTED: Nunca se descarta ni trunca (ej: system prompt, current user query, tool schemas obligatorios).
    HIGH_PRIORITY: Evidencia estructurada clave o decisiones recientes.
    NORMAL: Contexto general de memoria y evidencia estándar.
    LOW_PRIORITY: Historial antiguo opcional, metadata accesoria.
    REMOVABLE: Elementos duplicados o evidencia puramente prescindible.
    """
    PROTECTED = "PROTECTED"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    NORMAL = "NORMAL"
    LOW_PRIORITY = "LOW_PRIORITY"
    REMOVABLE = "REMOVABLE"


class CompressionActionType(str, Enum):
    """Tipos de acciones deterministas aplicables al contexto."""
    DROP_DUPLICATES = "DROP_DUPLICATES"
    PRUNE_OLDEST_HISTORY = "PRUNE_OLDEST_HISTORY"
    LIMIT_OPTIONAL_EVIDENCE = "LIMIT_OPTIONAL_EVIDENCE"
    COMPACT_STRUCTURED = "COMPACT_STRUCTURED"
    REMOVE_LOW_PRIORITY = "REMOVE_LOW_PRIORITY"
    NO_OP = "NO_OP"


@dataclass(frozen=True)
class ContextItem:
    """
    Elemento granular inmutable de contexto evaluado para compresión.
    """
    item_id: str
    component_type: ContextComponentType
    content: Any  # Puede ser str, dict, list o estructura serializable
    priority: PriorityLevel = PriorityLevel.NORMAL
    sequence_order: int = 0  # Orden cronológico o de presentación (0 = más antiguo o inicial)
    token_count: Optional[int] = None
    is_duplicate: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.item_id or not self.item_id.strip():
            raise ValueError("item_id cannot be empty")
        if not isinstance(self.component_type, ContextComponentType):
            raise ValueError("component_type must be a valid ContextComponentType")
        if not isinstance(self.priority, PriorityLevel):
            raise ValueError("priority must be a valid PriorityLevel")
        if not isinstance(self.sequence_order, int) or isinstance(self.sequence_order, bool):
            raise ValueError("sequence_order must be an integer")
        if self.token_count is not None:
            if not isinstance(self.token_count, int) or isinstance(self.token_count, bool):
                raise ValueError("token_count must be an integer")
            if self.token_count < 0:
                raise ValueError("token_count cannot be negative")

        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))


@dataclass(frozen=True)
class CompressionAction:
    """
    Registro inmutable de una acción de compresión aplicada deterministamente.
    """
    action_type: CompressionActionType
    target_component: ContextComponentType
    item_ids_affected: Tuple[str, ...] = ()
    tokens_saved: int = 0
    rationale: str = ""

    def __post_init__(self):
        if not isinstance(self.action_type, CompressionActionType):
            raise ValueError("action_type must be a valid CompressionActionType")
        if not isinstance(self.target_component, ContextComponentType):
            raise ValueError("target_component must be a valid ContextComponentType")
        if not isinstance(self.tokens_saved, int) or isinstance(self.tokens_saved, bool):
            raise ValueError("tokens_saved must be an integer")
        if self.tokens_saved < 0:
            raise ValueError("tokens_saved cannot be negative")
        if not isinstance(self.item_ids_affected, tuple):
            object.__setattr__(self, "item_ids_affected", tuple(self.item_ids_affected))


@dataclass(frozen=True)
class CompressionPolicy:
    """
    Política inmutable y versionada para compresión de prompt y contexto.
    Define reglas deterministas, prioridades por defecto y estrategias permitidas.
    """
    policy_id: str
    version: str = "1.0.0"
    allow_drop_duplicates: bool = True
    allow_prune_history: bool = True
    allow_limit_evidence: bool = True
    allow_compact_structured: bool = True
    max_history_items_to_keep: int = 5
    max_evidence_items_to_keep: int = 10
    default_priorities: Mapping[ContextComponentType, PriorityLevel] = field(
        default_factory=lambda: {
            ContextComponentType.SYSTEM_INSTRUCTIONS: PriorityLevel.PROTECTED,
            ContextComponentType.USER_INPUT: PriorityLevel.PROTECTED,
            ContextComponentType.TOOL_SCHEMAS: PriorityLevel.PROTECTED,
            ContextComponentType.MEMORY_CONTEXT: PriorityLevel.NORMAL,
            ContextComponentType.RETRIEVED_EVIDENCE: PriorityLevel.NORMAL,
            ContextComponentType.CONVERSATION_HISTORY: PriorityLevel.LOW_PRIORITY,
            ContextComponentType.OTHER: PriorityLevel.LOW_PRIORITY,
        }
    )
    description: str = "Default deterministic prompt compression policy"

    def __post_init__(self):
        if not self.policy_id or not self.policy_id.strip():
            raise ValueError("policy_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("version cannot be empty")
        if not isinstance(self.max_history_items_to_keep, int) or isinstance(self.max_history_items_to_keep, bool):
            raise ValueError("max_history_items_to_keep must be an integer")
        if self.max_history_items_to_keep < 0:
            raise ValueError("max_history_items_to_keep cannot be negative")
        if not isinstance(self.max_evidence_items_to_keep, int) or isinstance(self.max_evidence_items_to_keep, bool):
            raise ValueError("max_evidence_items_to_keep must be an integer")
        if self.max_evidence_items_to_keep < 0:
            raise ValueError("max_evidence_items_to_keep cannot be negative")

        object.__setattr__(self, "default_priorities", MappingProxyType(dict(self.default_priorities)))


@dataclass(frozen=True)
class RawContextPayload:
    """
    Estructura inmutable de entrada con los componentes del contexto sin procesar o semi-estructurados.
    """
    system_instructions: Optional[str] = None
    user_input: Optional[str] = None
    memory_context: Optional[Union[str, Sequence[Any]]] = None
    tool_schemas: Optional[Sequence[Any]] = None
    retrieved_evidence: Optional[Sequence[Any]] = None
    conversation_history: Optional[Sequence[Any]] = None
    other: Optional[Any] = None
    custom_items: Tuple[ContextItem, ...] = ()

    def __post_init__(self):
        if not isinstance(self.custom_items, tuple):
            object.__setattr__(self, "custom_items", tuple(self.custom_items))


@dataclass(frozen=True)
class CompressedContextPayload:
    """
    Estructura inmutable de salida con los componentes de contexto comprimidos y reconstruidos.
    """
    system_instructions: Optional[str] = None
    user_input: Optional[str] = None
    memory_context: Optional[Union[str, Tuple[Any, ...]]] = None
    tool_schemas: Optional[Tuple[Any, ...]] = None
    retrieved_evidence: Optional[Tuple[Any, ...]] = None
    conversation_history: Optional[Tuple[Any, ...]] = None
    other: Optional[Any] = None
    items: Tuple[ContextItem, ...] = ()

    def __post_init__(self):
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if self.tool_schemas is not None and not isinstance(self.tool_schemas, tuple):
            object.__setattr__(self, "tool_schemas", tuple(self.tool_schemas))
        if self.retrieved_evidence is not None and not isinstance(self.retrieved_evidence, tuple):
            object.__setattr__(self, "retrieved_evidence", tuple(self.retrieved_evidence))
        if self.conversation_history is not None and not isinstance(self.conversation_history, tuple):
            object.__setattr__(self, "conversation_history", tuple(self.conversation_history))
        if isinstance(self.memory_context, (list, set)):
            object.__setattr__(self, "memory_context", tuple(self.memory_context))


@dataclass(frozen=True)
class CompressionRequest:
    """
    Solicitud inmutable de compresión determinista de prompt / contexto.
    """
    raw_payload: RawContextPayload
    target_budget_tokens: Optional[int]
    budget_decision: Optional[ContextBudgetDecision] = None
    model_id: Optional[str] = None
    policy: Optional[CompressionPolicy] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.target_budget_tokens is not None:
            if not isinstance(self.target_budget_tokens, int) or isinstance(self.target_budget_tokens, bool):
                raise ValueError("target_budget_tokens must be an integer")
            if self.target_budget_tokens < 0:
                raise ValueError("target_budget_tokens cannot be negative")

        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))


@dataclass(frozen=True)
class CompressionResult:
    """
    Resultado inmutable y determinista de la compresión de contexto (M.3).
    Conserva trazabilidad completa, tokens originales/finales, componentes preservados y reducidos, y checksum.
    """
    status: CompressionStatus
    original_token_count: Optional[int]
    final_token_count: Optional[int]
    target_budget_tokens: Optional[int]
    compressed_payload: Optional[CompressedContextPayload]
    actions_applied: Tuple[CompressionAction, ...] = ()
    preserved_components: Tuple[str, ...] = ()
    reduced_components: Tuple[str, ...] = ()
    final_breakdown: Optional[InputTokensBreakdown] = None
    policy_id: str = "default_deterministic_m3_policy"
    policy_version: str = "1.0.0"
    rationale: str = ""
    evaluation_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.status, CompressionStatus):
            raise ValueError("status must be a valid CompressionStatus")
        if self.original_token_count is not None:
            if not isinstance(self.original_token_count, int) or isinstance(self.original_token_count, bool):
                raise ValueError("original_token_count must be an integer")
            if self.original_token_count < 0:
                raise ValueError("original_token_count cannot be negative")
        if self.final_token_count is not None:
            if not isinstance(self.final_token_count, int) or isinstance(self.final_token_count, bool):
                raise ValueError("final_token_count must be an integer")
            if self.final_token_count < 0:
                raise ValueError("final_token_count cannot be negative")
        if self.target_budget_tokens is not None:
            if not isinstance(self.target_budget_tokens, int) or isinstance(self.target_budget_tokens, bool):
                raise ValueError("target_budget_tokens must be an integer")
            if self.target_budget_tokens < 0:
                raise ValueError("target_budget_tokens cannot be negative")
        if not isinstance(self.actions_applied, tuple):
            object.__setattr__(self, "actions_applied", tuple(self.actions_applied))
        if not isinstance(self.preserved_components, tuple):
            object.__setattr__(self, "preserved_components", tuple(self.preserved_components))
        if not isinstance(self.reduced_components, tuple):
            object.__setattr__(self, "reduced_components", tuple(self.reduced_components))

    @property
    def is_within_target_budget(self) -> bool:
        """Indica si el resultado final satisface el presupuesto objetivo."""
        if self.final_token_count is None or self.target_budget_tokens is None:
            return False
        return self.final_token_count <= self.target_budget_tokens

    @property
    def tokens_saved(self) -> int:
        if self.original_token_count is None or self.final_token_count is None:
            return 0
        return max(0, self.original_token_count - self.final_token_count)

    def calculate_checksum(self) -> str:
        """Calcula hash SHA-256 canónico y determinista del resultado para auditoría."""
        payload = {
            "status": self.status.value,
            "original_token_count": self.original_token_count,
            "final_token_count": self.final_token_count,
            "target_budget_tokens": self.target_budget_tokens,
            "actions_applied": [
                {
                    "action_type": a.action_type.value,
                    "target_component": a.target_component.value,
                    "item_ids_affected": list(a.item_ids_affected),
                    "tokens_saved": a.tokens_saved,
                    "rationale": a.rationale,
                }
                for a in self.actions_applied
            ],
            "preserved_components": list(self.preserved_components),
            "reduced_components": list(self.reduced_components),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "rationale": self.rationale,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
