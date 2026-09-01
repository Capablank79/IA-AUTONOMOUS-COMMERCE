"""
Modelos de dominio para Detección de Cambios (Change Detection - Hito J.4).

Define ChangeRecord y tipos asociados para capturar diferencias deterministas,
trazables e inmutables entre observaciones y oportunidades a lo largo del tiempo.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union

from src.domain.market_intelligence.models import Confidence, SignalType


class ChangeSubjectType(str, Enum):
    """Tipo de sujeto sobre el cual se detecta el cambio."""
    MARKET_OBSERVATION = "MARKET_OBSERVATION"
    OPPORTUNITY = "OPPORTUNITY"
    TEMPORAL_SNAPSHOT = "TEMPORAL_SNAPSHOT"


class ChangeType(str, Enum):
    """
    Taxonomía de tipos de cambio reconocidos en J.4.
    """
    NO_CHANGE = "NO_CHANGE"
    PRICE_CHANGED = "PRICE_CHANGED"
    STOCK_CHANGED = "STOCK_CHANGED"
    AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"
    SOLD_QUANTITY_CHANGED = "SOLD_QUANTITY_CHANGED"
    COMPETITION_CHANGED = "COMPETITION_CHANGED"
    SELLER_CHANGED = "SELLER_CHANGED"
    SOURCE_STATUS_CHANGED = "SOURCE_STATUS_CHANGED"
    OPPORTUNITY_STATUS_CHANGED = "OPPORTUNITY_STATUS_CHANGED"
    OPPORTUNITY_SCORE_CHANGED = "OPPORTUNITY_SCORE_CHANGED"
    OPPORTUNITY_METRICS_CHANGED = "OPPORTUNITY_METRICS_CHANGED"
    UNKNOWN_CHANGED = "UNKNOWN_CHANGED"
    UNKNOWN_TRANSITION = "UNKNOWN_TRANSITION"
    MULTIPLE_CHANGES = "MULTIPLE_CHANGES"


class ChangeSignificance(str, Enum):
    """
    Nivel determinista de significancia del cambio.
    Basado en reglas explícitas de dominio (no ML, no LLM).
    """
    NONE = "NONE"
    NEGLIGIBLE = "NEGLIGIBLE"
    MODERATE = "MODERATE"
    SIGNIFICANT = "SIGNIFICANT"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ObservedChangeField:
    """
    Representa un campo directamente observado que cambió entre T0 y T1.
    Preserva el valor anterior y el actual sin fabricar inferencias.
    UNKNOWN != 0.
    """
    field_name: str
    previous_value: Any
    current_value: Any
    is_previous_unknown: bool = False
    is_current_unknown: bool = False

    def __post_init__(self):
        if not self.field_name or not isinstance(self.field_name, str):
            raise ValueError("field_name must be a non-empty string")


@dataclass(frozen=True)
class DerivedChangeDelta:
    """
    Representa un delta calculado a partir de campos observados numéricos.
    Separación estricta entre valor observado y valor derivado.
    """
    field_name: str
    numeric_delta: Optional[Decimal] = None
    percentage_delta: Optional[Decimal] = None
    delta_description: Optional[str] = None
    is_valid_delta: bool = True

    def __post_init__(self):
        if not self.field_name or not isinstance(self.field_name, str):
            raise ValueError("field_name must be a non-empty string")


@dataclass(frozen=True)
class ChangeRecord:
    """
    Entidad inmutable de Dominio para el Registro de Cambio (Hito J.4).
    Representa un cambio determinista, trazable e inmutable entre T0 y T1.

    Límites:
    - NO crea DecisionRecord.
    - NO ejecuta acciones comerciales.
    - NO genera alertas distribuidas (J.6).
    - NO emite eventos a un Event Bus (J.5).
    - NO modifica PolicyEngine.
    """
    change_id: str
    subject_type: ChangeSubjectType
    subject_id: str
    previous_reference: Optional[str]
    current_reference: str
    change_type: ChangeType
    detected_at: datetime
    observed_from: Optional[datetime]
    observed_to: datetime
    changed_fields: Tuple[str, ...]
    observed_changes: Tuple[ObservedChangeField, ...]
    derived_deltas: Tuple[DerivedChangeDelta, ...]
    significance: ChangeSignificance = ChangeSignificance.NONE
    confidence: Confidence = Confidence.HIGH
    provenance: str = "DERIVED"
    correlation_id: str = "default-correlation"
    idempotency_key: str = ""
    evidence_references: Tuple[str, ...] = field(default_factory=tuple)
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.change_id or not isinstance(self.change_id, str):
            raise ValueError("change_id must be a non-empty string")
        if not self.subject_id or not isinstance(self.subject_id, str):
            raise ValueError("subject_id must be a non-empty string")
        if not self.current_reference or not isinstance(self.current_reference, str):
            raise ValueError("current_reference must be a non-empty string")
        if self.detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware (UTC)")
        if self.observed_to.tzinfo is None:
            raise ValueError("observed_to must be timezone-aware (UTC)")
        if self.observed_from is not None and self.observed_from.tzinfo is None:
            raise ValueError("observed_from must be timezone-aware (UTC)")

        if not isinstance(self.changed_fields, tuple):
            object.__setattr__(self, "changed_fields", tuple(self.changed_fields))
        if not isinstance(self.observed_changes, tuple):
            object.__setattr__(self, "observed_changes", tuple(self.observed_changes))
        if not isinstance(self.derived_deltas, tuple):
            object.__setattr__(self, "derived_deltas", tuple(self.derived_deltas))
        if not isinstance(self.evidence_references, tuple):
            object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

        if not self.idempotency_key:
            prev = self.previous_reference or "NONE"
            auto_key = f"{self.subject_type.value}::{self.subject_id}::{prev}::{self.current_reference}::{self.change_type.value}"
            object.__setattr__(self, "idempotency_key", auto_key)
