from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Tuple, Mapping, Any, Dict
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.models import (
    EvidenceProvenanceType,
    RiskLevel,
    QuoteFreshness,
)
from src.domain.profit.models import (
    Money,
    EconomicScenarioType,
    ProfitStatus,
    UnitEconomics,
)


class AllocationStatus(str, Enum):
    """
    Estado formal de una decisión o registro de asignación de capital.
    """
    APPROVED = "APPROVED"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    REJECTED = "REJECTED"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"
    LIMITED_ALLOCATION = "LIMITED_ALLOCATION"
    RELEASED = "RELEASED"
    REALLOCATED = "REALLOCATED"
    INVALIDATED = "INVALIDATED"


class AllocationDecisionReason(str, Enum):
    """
    Razones estructuradas y tipadas para la decisión de asignación de capital.
    """
    APPROVED_FULL_BUDGET = "APPROVED_FULL_BUDGET"
    CAPPED_BY_MAXIMUM_EXPOSURE = "CAPPED_BY_MAXIMUM_EXPOSURE"
    CAPPED_BY_AVAILABLE_CAPITAL = "CAPPED_BY_AVAILABLE_CAPITAL"
    INSUFFICIENT_AVAILABLE_CAPITAL = "INSUFFICIENT_AVAILABLE_CAPITAL"
    INSUFFICIENT_ECONOMIC_EVIDENCE = "INSUFFICIENT_ECONOMIC_EVIDENCE"
    NEGATIVE_OR_INSUFFICIENT_MARGIN = "NEGATIVE_OR_INSUFFICIENT_MARGIN"
    EXCESSIVE_SUPPLIER_OR_MARKET_RISK = "EXCESSIVE_SUPPLIER_OR_MARKET_RISK"
    LOW_CONFIDENCE_REQUIRES_INVESTIGATION = "LOW_CONFIDENCE_REQUIRES_INVESTIGATION"
    CURRENCY_MISMATCH_NO_FX = "CURRENCY_MISMATCH_NO_FX"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    OPPORTUNITY_NOT_READY = "OPPORTUNITY_NOT_READY"
    CAPITAL_RELEASED_DETERIORATION = "CAPITAL_RELEASED_DETERIORATION"
    CAPITAL_REALLOCATED = "CAPITAL_REALLOCATED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


@dataclass(frozen=True)
class CapitalBudget:
    """
    Presupuesto de capital determinista e inmutable.
    
    Reglas:
    - total_capital >= 0
    - reserved_capital >= 0 (capital protegido / de reserva que NUNCA se asigna a oportunidades)
    - committed_capital >= 0 (capital actualmente comprometido en asignaciones activas)
    - total_capital >= reserved_capital + committed_capital
    - allocatable_capital = total_capital - reserved_capital - committed_capital
    - uncommitted_capital = total_capital - committed_capital
    """
    budget_id: str
    total_capital: Decimal
    reserved_capital: Decimal
    committed_capital: Decimal
    currency: str = "CLP"
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.budget_id:
            raise ValueError("budget_id cannot be empty")
        if not self.currency:
            raise ValueError("currency cannot be empty")
        if self.total_capital < Decimal("0"):
            raise ValueError("total_capital cannot be negative")
        if self.reserved_capital < Decimal("0"):
            raise ValueError("reserved_capital cannot be negative")
        if self.committed_capital < Decimal("0"):
            raise ValueError("committed_capital cannot be negative")
        if self.total_capital < (self.reserved_capital + self.committed_capital):
            raise ValueError(
                f"total_capital ({self.total_capital}) must be >= reserved_capital ({self.reserved_capital}) + committed_capital ({self.committed_capital})"
            )

    @property
    def allocatable_capital(self) -> Decimal:
        """Capital disponible estrictamente asignable a nuevas oportunidades."""
        return self.total_capital - self.reserved_capital - self.committed_capital

    @property
    def uncommitted_capital(self) -> Decimal:
        """Capital que no está actualmente comprometido (incluye reserva protegida)."""
        return self.total_capital - self.committed_capital

    @classmethod
    def create(
        cls,
        budget_id: str,
        total_capital: Decimal,
        reserve_ratio: Optional[Decimal] = None,
        reserved_amount: Optional[Decimal] = None,
        reserved_capital: Optional[Decimal] = None,
        committed_capital: Decimal = Decimal("0"),
        currency: str = "CLP",
        as_of: Optional[datetime] = None,
    ) -> "CapitalBudget":
        """
        Crea un CapitalBudget calculando la reserva si se especifica reserve_ratio, reserved_amount o reserved_capital.
        """
        if reserved_capital is not None:
            res_val = reserved_capital
        elif reserved_amount is not None:
            res_val = reserved_amount
        elif reserve_ratio is not None:
            if reserve_ratio < Decimal("0") or reserve_ratio > Decimal("1.0"):
                raise ValueError("reserve_ratio must be between 0.0 and 1.0")
            res_val = total_capital * reserve_ratio
        else:
            res_val = Decimal("0")

        return cls(
            budget_id=budget_id,
            total_capital=total_capital,
            reserved_capital=res_val,
            committed_capital=committed_capital,
            currency=currency,
            as_of=as_of or datetime.now(timezone.utc),
        )

    def with_commitment(self, amount: Decimal) -> "CapitalBudget":
        """Genera un nuevo snapshot de CapitalBudget con capital adicional comprometido."""
        if amount < Decimal("0"):
            raise ValueError("Commitment amount cannot be negative")
        if amount > self.allocatable_capital:
            raise ValueError(
                f"Cannot commit {amount}: exceeds allocatable capital {self.allocatable_capital}"
            )
        return CapitalBudget(
            budget_id=self.budget_id,
            total_capital=self.total_capital,
            reserved_capital=self.reserved_capital,
            committed_capital=self.committed_capital + amount,
            currency=self.currency,
            as_of=datetime.now(timezone.utc),
        )

    def with_release(self, amount: Decimal) -> "CapitalBudget":
        """Genera un nuevo snapshot liberando capital previamente comprometido."""
        if amount < Decimal("0"):
            raise ValueError("Release amount cannot be negative")
        if amount > self.committed_capital:
            raise ValueError(
                f"Cannot release {amount}: exceeds current committed capital {self.committed_capital}"
            )
        return CapitalBudget(
            budget_id=self.budget_id,
            total_capital=self.total_capital,
            reserved_capital=self.reserved_capital,
            committed_capital=self.committed_capital - amount,
            currency=self.currency,
            as_of=datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class CapitalExposure:
    """
    Representa el estado y límites de exposición de capital por oportunidad y agregado.
    
    Reglas:
    - current_exposure: exposición actual antes de nueva asignación
    - maximum_allowed_exposure: límite absoluto permitido por política para la oportunidad
    - requested_exposure: monto de capital adicional solicitado
    - new_exposure: exposición total que resultaría si se aprobara
    - remaining_opportunity_capacity: cuánto más puede recibir esta oportunidad antes del límite
    """
    opportunity_id: str
    existing_exposure: Decimal
    maximum_allowed_exposure: Decimal
    allocatable_budget_capital: Decimal
    currency: str = "CLP"

    def __post_init__(self):
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if self.existing_exposure < Decimal("0"):
            raise ValueError("existing_exposure cannot be negative")
        if self.maximum_allowed_exposure < Decimal("0"):
            raise ValueError("maximum_allowed_exposure cannot be negative")
        if self.allocatable_budget_capital < Decimal("0"):
            raise ValueError("allocatable_budget_capital cannot be negative")

    @property
    def remaining_opportunity_capacity(self) -> Decimal:
        """Capacidad restante que puede comprometerse en esta oportunidad."""
        rem = self.maximum_allowed_exposure - self.existing_exposure
        return rem if rem > Decimal("0") else Decimal("0")

    @property
    def effective_available_ceiling(self) -> Decimal:
        """El techo efectivo disponible considerando tanto el límite de la oportunidad como el budget total."""
        return min(self.remaining_opportunity_capacity, self.allocatable_budget_capital)


@dataclass(frozen=True)
class CapitalDownsideAnalysis:
    """
    Representación del riesgo a la baja y horizonte de liquidez.
    Sin inventar probabilidades ni retornos no observados.
    """
    capital_at_risk: Decimal
    liquidity_constraints: Tuple[str, ...] = field(default_factory=tuple)
    capital_horizon_days: Optional[int] = None
    is_downside_known: bool = True
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.liquidity_constraints, tuple):
            object.__setattr__(self, "liquidity_constraints", tuple(self.liquidity_constraints))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class AllocationPolicy:
    """
    Política determinista, parametrizable y explicable para la asignación de capital.
    
    No asume porcentajes mágicos: todos los umbrales son configurables con defaults transparentes.
    """
    max_exposure_per_opportunity_pct: Decimal = Decimal("0.25")  # Max 25% del allocatable capital por oportunidad
    max_exposure_absolute_amount: Optional[Decimal] = None      # Límite monetario fijo opcional
    min_net_margin_pct: Decimal = Decimal("10.0")              # Margen neto mínimo requerido (ej: 10%)
    min_gross_margin_pct: Decimal = Decimal("15.0")            # Margen bruto mínimo requerido (ej: 15%)
    max_risk_score_allowed: Decimal = Decimal("70.0")          # Riesgo máximo tolerable (0-100)
    min_confidence_for_full_allocation: Confidence = Confidence.MEDIUM
    allow_partial_allocation: bool = True
    require_known_economics: bool = True
    limited_allocation_cap_pct: Decimal = Decimal("0.10")      # Cap para prueba limitada cuando la evidencia es parcial

    def __post_init__(self):
        if self.max_exposure_per_opportunity_pct <= Decimal("0") or self.max_exposure_per_opportunity_pct > Decimal("1.0"):
            raise ValueError("max_exposure_per_opportunity_pct must be between 0 (exclusive) and 1.0")
        if self.max_exposure_absolute_amount is not None and self.max_exposure_absolute_amount <= Decimal("0"):
            raise ValueError("max_exposure_absolute_amount must be greater than zero if specified")
        if self.limited_allocation_cap_pct < Decimal("0") or self.limited_allocation_cap_pct > Decimal("1.0"):
            raise ValueError("limited_allocation_cap_pct must be between 0.0 and 1.0")


@dataclass(frozen=True)
class AllocationDecision:
    """
    Decisión determinista y estructurada de asignación de capital.
    """
    decision_id: str
    opportunity_id: str
    supplier_id: Optional[str]
    status: AllocationStatus
    reason: AllocationDecisionReason
    requested_capital: Decimal
    approved_capital: Decimal
    unapproved_capital: Decimal
    maximum_allowed_exposure: Decimal
    available_allocatable_capital: Decimal
    remaining_allocatable_capital: Decimal
    currency: str
    allocation_ratio: Decimal  # approved_capital / requested_capital
    profit_score: Optional[Decimal]
    risk_score: Optional[Decimal]
    opportunity_score: Optional[Decimal]
    allocation_score: Optional[Decimal]
    expected_profit: Optional[Decimal]
    expected_margin_pct: Optional[Decimal]
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    downside_analysis: CapitalDownsideAnalysis
    scenario_allocations: Mapping[EconomicScenarioType, Decimal] = field(default_factory=dict)
    conditions: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    explanation: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.decision_id:
            raise ValueError("decision_id cannot be empty")
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if not isinstance(self.conditions, tuple):
            object.__setattr__(self, "conditions", tuple(self.conditions))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.scenario_allocations, MappingProxyType):
            object.__setattr__(self, "scenario_allocations", MappingProxyType(dict(self.scenario_allocations)))


@dataclass(frozen=True)
class AllocationHistoryEntry:
    """
    Registro inmutable de un cambio o reevaluación de asignación de capital.
    """
    timestamp: datetime
    previous_status: AllocationStatus
    new_status: AllocationStatus
    previous_amount: Decimal
    new_amount: Decimal
    released_amount: Decimal
    reason: str
    trigger_event: str


@dataclass(frozen=True)
class CapitalAllocation:
    """
    Entidad inmutable que representa una asignación activa o histórica de capital.
    """
    allocation_id: str
    budget_id: str
    opportunity_id: str
    supplier_id: Optional[str]
    allocated_amount: Decimal
    currency: str
    status: AllocationStatus
    decision: AllocationDecision
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    history: Tuple[AllocationHistoryEntry, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.allocation_id:
            raise ValueError("allocation_id cannot be empty")
        if not self.budget_id:
            raise ValueError("budget_id cannot be empty")
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if self.allocated_amount < Decimal("0"):
            raise ValueError("allocated_amount cannot be negative")
        if not isinstance(self.history, tuple):
            object.__setattr__(self, "history", tuple(self.history))

    def release(self, reason: str = "Capital released due to condition change") -> "CapitalAllocation":
        """
        Libera completamente la asignación actual, dejando allocated_amount en 0 y status RELEASED.
        """
        entry = AllocationHistoryEntry(
            timestamp=datetime.now(timezone.utc),
            previous_status=self.status,
            new_status=AllocationStatus.RELEASED,
            previous_amount=self.allocated_amount,
            new_amount=Decimal("0"),
            released_amount=self.allocated_amount,
            reason=reason,
            trigger_event="RELEASE",
        )
        return CapitalAllocation(
            allocation_id=self.allocation_id,
            budget_id=self.budget_id,
            opportunity_id=self.opportunity_id,
            supplier_id=self.supplier_id,
            allocated_amount=Decimal("0"),
            currency=self.currency,
            status=AllocationStatus.RELEASED,
            decision=self.decision,
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc),
            history=(*self.history, entry),
        )

    def reallocate(
        self,
        new_amount: Decimal,
        new_status: AllocationStatus,
        new_decision: AllocationDecision,
        reason: str = "Capital reallocated",
    ) -> "CapitalAllocation":
        """
        Reasigna el capital modificando el monto y registrando la diferencia en el historial.
        """
        if new_amount < Decimal("0"):
            raise ValueError("new_amount cannot be negative")
        released = (self.allocated_amount - new_amount) if new_amount < self.allocated_amount else Decimal("0")
        entry = AllocationHistoryEntry(
            timestamp=datetime.now(timezone.utc),
            previous_status=self.status,
            new_status=new_status,
            previous_amount=self.allocated_amount,
            new_amount=new_amount,
            released_amount=released,
            reason=reason,
            trigger_event="REALLOCATE",
        )
        return CapitalAllocation(
            allocation_id=self.allocation_id,
            budget_id=self.budget_id,
            opportunity_id=self.opportunity_id,
            supplier_id=self.supplier_id,
            allocated_amount=new_amount,
            currency=self.currency,
            status=new_status,
            decision=new_decision,
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc),
            history=(*self.history, entry),
        )
