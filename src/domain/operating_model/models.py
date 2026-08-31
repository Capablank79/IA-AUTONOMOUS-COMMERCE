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
    SupplierRecommendation,
    SupplierRiskProfile,
    ShippingOption,
)
from src.domain.profit.models import (
    Money,
    EconomicScenarioType,
    ProfitStatus,
    UnitEconomics,
    CostComponent,
    CostComponentStatus,
    CostComponentType,
    LandedCost,
    ProfitResult,
    MarginResult,
)
from src.domain.capital.models import (
    CapitalBudget,
    CapitalExposure,
    AllocationDecision,
    AllocationStatus,
)


class OperatingModelType(str, Enum):
    """
    Tipo de modelo operativo formal.
    """
    INVENTORY = "INVENTORY"
    DROPSHIPPING = "DROPSHIPPING"
    NO_DECISION = "NO_DECISION"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"


class OperatingDecisionType(str, Enum):
    """
    Decisión determinista final sobre el modelo operativo.
    """
    SELECT_INVENTORY = "SELECT_INVENTORY"
    SELECT_DROPSHIPPING = "SELECT_DROPSHIPPING"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"
    NO_DECISION = "NO_DECISION"


class DecisionTrigger(str, Enum):
    """
    Desencadenantes de decisión y reevaluación.
    """
    INITIAL_EVALUATION = "INITIAL_EVALUATION"
    SUPPLIER_RISK_INCREASE = "SUPPLIER_RISK_INCREASE"
    SUPPLIER_STOCK_DEPLETION = "SUPPLIER_STOCK_DEPLETION"
    DEMAND_CHANGE = "DEMAND_CHANGE"
    CAPITAL_CONSTRAINT_CHANGE = "CAPITAL_CONSTRAINT_CHANGE"
    MOQ_CHANGE = "MOQ_CHANGE"
    PRICE_CHANGE = "PRICE_CHANGE"
    LEAD_TIME_EXTENSION = "LEAD_TIME_EXTENSION"
    MARGIN_EROSION = "MARGIN_EROSION"
    EVIDENCE_STALENESS = "EVIDENCE_STALENESS"
    MANUAL_TRIGGER = "MANUAL_TRIGGER"


class DemandVelocity(str, Enum):
    """
    Velocidad estimada de rotación de inventario con evidencia temporal.
    UNKNOWN no se asume HIGH ni LOW.
    """
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    SLOW = "SLOW"
    STAGNANT = "STAGNANT"
    UNKNOWN = "UNKNOWN"


class ObsolescenceRisk(str, Enum):
    """
    Riesgo de obsolescencia / perecibilidad / pérdida de relevancia del producto.
    """
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class InventoryScenario:
    """
    Escenario operativo de compra y mantenimiento de inventario propio.
    
    Características clave:
    - Compra por volumen / MOQ.
    - Requiere compromiso de capital por adelantado (stock_exposure = purchase_cost * quantity + shipping + taxes).
    - Menor costo unitario -> mayor margen bruto/neto potencial.
    - Riesgo de stock / capital inmovilizado y obsolescencia.
    - Menor riesgo operacional en entrega cliente final una vez recibido el stock.
    """
    opportunity_id: str
    supplier_id: str
    target_quantity: int
    moq: int
    unit_economics: UnitEconomics
    required_capital: Decimal
    stock_exposure: Decimal
    lead_time_days: Optional[int]
    demand_signal_type: SignalType
    demand_velocity: DemandVelocity
    obsolescence_risk: ObsolescenceRisk
    supplier_risk_level: RiskLevel
    supplier_reliability_score: Optional[Decimal]
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    storage_cost_monthly: Optional[CostComponent] = None
    estimated_days_to_sell: Optional[int] = None
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if not self.supplier_id:
            raise ValueError("supplier_id cannot be empty")
        if self.target_quantity < 1:
            raise ValueError("target_quantity must be at least 1")
        if self.moq < 1:
            raise ValueError("moq must be at least 1")
        if self.required_capital < Decimal("0"):
            raise ValueError("required_capital cannot be negative")
        if self.stock_exposure < Decimal("0"):
            raise ValueError("stock_exposure cannot be negative")
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))

    @property
    def expected_profit(self) -> Optional[Decimal]:
        if self.unit_economics.net_profit is None:
            return None
        return self.unit_economics.net_profit * Decimal(str(self.target_quantity))

    @property
    def expected_margin_pct(self) -> Optional[Decimal]:
        return self.unit_economics.net_margin_pct

    @property
    def is_viable_economically(self) -> bool:
        return (
            self.unit_economics.status == ProfitStatus.PROFIT_COMPLETE
            and self.unit_economics.net_profit is not None
            and self.unit_economics.net_profit > Decimal("0")
            and self.unit_economics.net_margin_pct is not None
            and self.unit_economics.net_margin_pct > Decimal("0")
        )


@dataclass(frozen=True)
class DropshippingScenario:
    """
    Escenario operativo de despacho directo por parte del proveedor bajo demanda.
    
    Características clave:
    - Compra unidad a unidad (QTY=1 por orden). MOQ=1.
    - No requiere inmovilizar capital en stock anticipado (stock_exposure = 0 o buffer operativo mínimo).
    - Costo unitario típicamente superior -> menor margen unitario.
    - Alto riesgo operacional de proveedor (dependencia en tiempo real de stock del proveedor, SLA de despacho y calidad).
    - Cero riesgo de obsolescencia de stock propio.
    """
    opportunity_id: str
    supplier_id: str
    unit_economics: UnitEconomics
    required_operational_capital: Decimal
    lead_time_days: Optional[int]
    supplier_risk_level: RiskLevel
    supplier_reliability_score: Optional[Decimal]
    supplier_sla_compliant: bool
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    payment_gateway_fee_pct: Optional[Decimal] = None
    marketplace_fee_pct: Optional[Decimal] = None
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if not self.supplier_id:
            raise ValueError("supplier_id cannot be empty")
        if self.required_operational_capital < Decimal("0"):
            raise ValueError("required_operational_capital cannot be negative")
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))

    @property
    def expected_profit_per_unit(self) -> Optional[Decimal]:
        return self.unit_economics.net_profit

    @property
    def expected_margin_pct(self) -> Optional[Decimal]:
        return self.unit_economics.net_margin_pct

    @property
    def is_viable_economically(self) -> bool:
        return (
            self.unit_economics.status == ProfitStatus.PROFIT_COMPLETE
            and self.unit_economics.net_profit is not None
            and self.unit_economics.net_profit > Decimal("0")
            and self.unit_economics.net_margin_pct is not None
            and self.unit_economics.net_margin_pct > Decimal("0")
        )


@dataclass(frozen=True)
class OperatingModelComparison:
    """
    Comparación determinista, multidimensional y estructurada entre Inventory y Dropshipping.
    Distingue:
    - Diferencial de profit esperado.
    - Diferencial de margen porcentual.
    - Diferencial de capital requerido y exposición de stock.
    - Diferencial de riesgo operativo vs riesgo de stock.
    - Suficiencia y frescura de evidencia en ambos modelos.
    """
    opportunity_id: str
    inventory_scenario: InventoryScenario
    dropshipping_scenario: DropshippingScenario
    profit_differential: Optional[Decimal]  # inventory profit - dropshipping profit (per unit or total batch)
    profit_differential_per_unit: Optional[Decimal]
    margin_differential_pct: Optional[Decimal]  # inventory margin % - dropshipping margin %
    capital_differential: Decimal  # inventory required capital - dropshipping required capital
    stock_exposure_differential: Decimal
    inventory_advantages: Tuple[str, ...]
    dropshipping_advantages: Tuple[str, ...]
    inventory_disadvantages: Tuple[str, ...]
    dropshipping_disadvantages: Tuple[str, ...]
    combined_unknowns: Tuple[str, ...]
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.inventory_advantages, tuple):
            object.__setattr__(self, "inventory_advantages", tuple(self.inventory_advantages))
        if not isinstance(self.dropshipping_advantages, tuple):
            object.__setattr__(self, "dropshipping_advantages", tuple(self.dropshipping_advantages))
        if not isinstance(self.inventory_disadvantages, tuple):
            object.__setattr__(self, "inventory_disadvantages", tuple(self.inventory_disadvantages))
        if not isinstance(self.dropshipping_disadvantages, tuple):
            object.__setattr__(self, "dropshipping_disadvantages", tuple(self.dropshipping_disadvantages))
        if not isinstance(self.combined_unknowns, tuple):
            object.__setattr__(self, "combined_unknowns", tuple(self.combined_unknowns))


@dataclass(frozen=True)
class OperatingModelPolicy:
    """
    Política determinista y configurable de decisión de modelo operativo.
    
    Reglas:
    - minimum_margin_inventory_pct: Margen mínimo exigido para asumir riesgo de inventario.
    - minimum_margin_dropshipping_pct: Margen mínimo exigido para dropshipping.
    - min_margin_advantage_for_inventory_pct: Ventaja mínima de margen que debe tener inventory sobre dropshipping para justificar lock-up de capital.
    - max_supplier_risk_for_dropshipping: Nivel máximo de riesgo aceptable en dropshipping (High o Critical descartan dropshipping).
    - max_lead_time_days_dropshipping: Días máximos de lead time tolerables en dropshipping antes de ser inviable frente al cliente.
    - min_supplier_reliability_for_dropshipping: Confiabilidad mínima de proveedor para dropshipping.
    - max_stock_exposure_ratio: Ratio máximo de stock exposure respecto al allocatable capital.
    - require_demand_validation_for_inventory: Si es True, no permite inventario si demand es INFERRED o UNKNOWN sin evidencia observada.
    """
    minimum_margin_inventory_pct: Decimal = Decimal("15.0")  # 15%
    minimum_margin_dropshipping_pct: Decimal = Decimal("8.0")  # 8%
    min_margin_advantage_for_inventory_pct: Decimal = Decimal("5.0")  # +5% margin advantage for inventory
    max_supplier_risk_for_dropshipping: RiskLevel = RiskLevel.MEDIUM
    max_lead_time_days_dropshipping: int = 7
    min_supplier_reliability_for_dropshipping: Decimal = Decimal("0.75")
    max_stock_exposure_ratio: Decimal = Decimal("0.50")
    require_demand_validation_for_inventory: bool = True
    max_lead_time_days_inventory: int = 45

    def __post_init__(self):
        if self.minimum_margin_inventory_pct < Decimal("0"):
            raise ValueError("minimum_margin_inventory_pct cannot be negative")
        if self.minimum_margin_dropshipping_pct < Decimal("0"):
            raise ValueError("minimum_margin_dropshipping_pct cannot be negative")
        if self.min_supplier_reliability_for_dropshipping < Decimal("0") or self.min_supplier_reliability_for_dropshipping > Decimal("1.0"):
            raise ValueError("min_supplier_reliability_for_dropshipping must be between 0.0 and 1.0")


@dataclass(frozen=True)
class DecisionExplanation:
    """
    Explicación estructurada, completa y trazable de la decisión de modelo operativo.
    1. Modelo elegido.
    2. Modelo alternativo.
    3. Diferencia económica.
    4. Diferencia de capital.
    5. Diferencia de riesgo.
    6. Evidencia que soporta la decisión.
    7. Unknowns detectados.
    8. Condiciones requeridas.
    9. Factores de invalidación (qué cambiaría la decisión).
    """
    selected_model: OperatingModelType
    alternative_model: OperatingModelType
    economic_rationale: str
    capital_rationale: str
    risk_rationale: str
    evidence_summary: str
    unknowns_summary: str
    conditions_summary: str
    invalidation_triggers: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.invalidation_triggers, tuple):
            object.__setattr__(self, "invalidation_triggers", tuple(self.invalidation_triggers))


@dataclass(frozen=True)
class OperatingDecision:
    """
    Decisión determinista, inmutable y explicable de modelo operativo (D-03).
    
    Representa la respuesta definitiva a:
    "INVENTORY vs DROPSHIPPING vs NEEDS_INVESTIGATION vs NO_DECISION"
    """
    decision_id: str
    opportunity_id: str
    supplier_id: str
    decision_type: OperatingDecisionType
    selected_model: OperatingModelType
    alternative_model: OperatingModelType
    comparison: OperatingModelComparison
    explanation: DecisionExplanation
    conditions: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    freshness: QuoteFreshness = QuoteFreshness.FRESH
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.decision_id:
            raise ValueError("decision_id cannot be empty")
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if not self.supplier_id:
            raise ValueError("supplier_id cannot be empty")
        if not isinstance(self.conditions, tuple):
            object.__setattr__(self, "conditions", tuple(self.conditions))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))

    @property
    def is_actionable(self) -> bool:
        return self.selected_model in (OperatingModelType.INVENTORY, OperatingModelType.DROPSHIPPING)


@dataclass(frozen=True)
class OperatingReassessmentRecord:
    """
    Registro histórico inmutable de reevaluación o pivot de modelo operativo.
    """
    reassessment_id: str
    opportunity_id: str
    previous_decision: OperatingDecision
    new_decision: OperatingDecision
    trigger: DecisionTrigger
    reason: str
    pivoted: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.reassessment_id:
            raise ValueError("reassessment_id cannot be empty")
        if not self.opportunity_id:
            raise ValueError("opportunity_id cannot be empty")
        if not self.reason:
            raise ValueError("reason cannot be empty")
