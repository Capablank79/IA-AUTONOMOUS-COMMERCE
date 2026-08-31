from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Tuple, Mapping, Any, Dict
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class Decision(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    REJECT = "REJECT"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if not self.currency:
            raise ValueError("currency cannot be empty")


@dataclass(frozen=True)
class FinancialData:
    price: Money
    supplier_price: Money
    commission_pct: Decimal
    shipping: Money
    other_costs: Money
    visible_sales: int


@dataclass(frozen=True)
class DecisionRules:
    minimum_margin_pct: Decimal
    excellent_margin_pct: Decimal
    minimum_sales: int


@dataclass(frozen=True)
class ProfitAnalysis:
    net_profit: Money
    net_margin_pct: Decimal
    decision: Decision
    commission: Money
    market_demand_ok: bool


# ==============================================================================
# D-01 DOMAIN MODELS: PROFIT ENGINE & LANDED COST
# ==============================================================================

class CostComponentType(str, Enum):
    PRODUCT_COST = "PRODUCT_COST"
    SHIPPING_COST = "SHIPPING_COST"
    IMPORT_DUTIES = "IMPORT_DUTIES"
    TAXES = "TAXES"
    MARKETPLACE_FEES = "MARKETPLACE_FEES"
    PAYMENT_FEES = "PAYMENT_FEES"
    PACKAGING = "PACKAGING"
    FULFILLMENT = "FULFILLMENT"
    OTHER_VARIABLE_COSTS = "OTHER_VARIABLE_COSTS"


class CostComponentStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SalePriceType(str, Enum):
    OBSERVED_SALE_PRICE = "OBSERVED_SALE_PRICE"
    ASSUMED_SALE_PRICE = "ASSUMED_SALE_PRICE"
    SCENARIO_SALE_PRICE = "SCENARIO_SALE_PRICE"


class LandedCostStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INCOMPLETE = "INCOMPLETE"
    NOT_COMPARABLE_CURRENCY = "NOT_COMPARABLE_CURRENCY"
    UNKNOWN = "UNKNOWN"


class ProfitStatus(str, Enum):
    PROFIT_COMPLETE = "PROFIT_COMPLETE"
    PROFIT_PARTIAL = "PROFIT_PARTIAL"
    PROFIT_INCOMPLETE = "PROFIT_INCOMPLETE"
    PROFIT_UNKNOWN = "PROFIT_UNKNOWN"
    NOT_COMPARABLE_CURRENCY = "NOT_COMPARABLE_CURRENCY"


class EconomicScenarioType(str, Enum):
    BASE = "BASE"
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"


@dataclass(frozen=True)
class ExchangeRate:
    """
    Representa un tipo de cambio verificado entre dos monedas.
    Nunca se asume FX=1.0 ni se fabrican conversiones sin procedencia y fecha.
    """
    from_currency: str
    to_currency: str
    rate: Decimal
    source: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: Confidence = Confidence.HIGH
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.LIVE

    def __post_init__(self):
        if not self.from_currency:
            raise ValueError("from_currency cannot be empty")
        if not self.to_currency:
            raise ValueError("to_currency cannot be empty")
        if self.rate <= Decimal("0"):
            raise ValueError("Exchange rate must be greater than zero")


@dataclass(frozen=True)
class CostComponent:
    """
    Representación inmutable y tipada de un componente individual de costo.
    UNKNOWN != 0. UNKNOWN != FREE.
    """
    component_type: CostComponentType
    status: CostComponentStatus
    amount: Optional[Decimal] = None
    currency: str = "CLP"
    fee_rate: Optional[Decimal] = None  # Ejemplo: 0.13 para 13% de marketplace fee
    fixed_fee_amount: Optional[Decimal] = None
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    source: str = "ESTIMATION"
    effective_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: str = ""
    is_per_unit: bool = True

    @classmethod
    def known(
        cls,
        component_type: CostComponentType,
        amount: Optional[Decimal] = None,
        currency: str = "CLP",
        fee_rate: Optional[Decimal] = None,
        fixed_fee_amount: Optional[Decimal] = None,
        confidence: Confidence = Confidence.HIGH,
        provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE,
        source: str = "MANUAL",
        effective_at: Optional[datetime] = None,
        details: str = "",
        is_per_unit: bool = True,
    ) -> "CostComponent":
        return cls(
            component_type=component_type,
            status=CostComponentStatus.KNOWN,
            amount=amount,
            currency=currency,
            fee_rate=fee_rate,
            fixed_fee_amount=fixed_fee_amount,
            confidence=confidence,
            provenance_type=provenance_type,
            source=source,
            effective_at=effective_at or datetime.now(timezone.utc),
            details=details,
            is_per_unit=is_per_unit,
        )

    @classmethod
    def unknown(
        cls,
        component_type: CostComponentType,
        currency: str = "CLP",
        details: str = "",
        source: str = "UNKNOWN_ABSENCE",
    ) -> "CostComponent":
        return cls(
            component_type=component_type,
            status=CostComponentStatus.UNKNOWN,
            amount=None,
            currency=currency,
            confidence=Confidence.UNKNOWN,
            provenance_type=EvidenceProvenanceType.FIXTURE,
            source=source,
            details=details,
        )

    @classmethod
    def not_applicable(
        cls,
        component_type: CostComponentType,
        currency: str = "CLP",
        details: str = "",
    ) -> "CostComponent":
        return cls(
            component_type=component_type,
            status=CostComponentStatus.NOT_APPLICABLE,
            amount=Decimal("0"),
            currency=currency,
            confidence=Confidence.HIGH,
            provenance_type=EvidenceProvenanceType.DERIVED,
            source="NOT_APPLICABLE_RULE",
            details=details,
        )

    @property
    def is_known(self) -> bool:
        return self.status == CostComponentStatus.KNOWN and (self.amount is not None or self.fee_rate is not None)

    def calculate_amount(self, base_sale_price: Optional[Decimal] = None) -> Optional[Decimal]:
        if self.status != CostComponentStatus.KNOWN:
            return None
        if self.amount is not None:
            return self.amount
        if self.fee_rate is not None and base_sale_price is not None:
            fee = base_sale_price * self.fee_rate
            if self.fixed_fee_amount is not None:
                fee += self.fixed_fee_amount
            return fee
        return None

    def __post_init__(self):
        if self.status == CostComponentStatus.KNOWN:
            if self.amount is None and self.fee_rate is None and self.fixed_fee_amount is None:
                raise ValueError(f"CostComponent {self.component_type} marked as KNOWN must have an amount or fee structure")
            if self.amount is not None and self.amount < Decimal("0"):
                raise ValueError(f"CostComponent {self.component_type} amount cannot be negative")
        elif self.status == CostComponentStatus.UNKNOWN:
            if self.amount is not None:
                raise ValueError(f"CostComponent {self.component_type} marked as UNKNOWN cannot have an amount (amount must be None)")
        elif self.status == CostComponentStatus.NOT_APPLICABLE:
            if self.amount is not None and self.amount != Decimal("0"):
                raise ValueError(f"CostComponent {self.component_type} marked as NOT_APPLICABLE must have amount None or 0")


@dataclass(frozen=True)
class SalePrice:
    """
    Representa el precio de venta unitario de mercado con procedencia explícita.
    """
    amount: Decimal
    currency: str
    price_type: SalePriceType
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    source: str = "MERCADO_LIBRE"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: str = ""

    @classmethod
    def observed(
        cls,
        amount: Decimal,
        currency: str = "CLP",
        confidence: Confidence = Confidence.HIGH,
        provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.LIVE,
        source: str = "MERCADO_LIBRE",
        details: str = "",
    ) -> "SalePrice":
        return cls(
            amount=amount,
            currency=currency,
            price_type=SalePriceType.OBSERVED_SALE_PRICE,
            confidence=confidence,
            provenance_type=provenance_type,
            source=source,
            details=details,
        )

    def __post_init__(self):
        if self.amount <= Decimal("0"):
            raise ValueError("Sale price amount must be greater than zero")
        if not self.currency:
            raise ValueError("currency cannot be empty")


@dataclass(frozen=True)
class Revenue:
    """
    Representa los ingresos brutos generados por la venta.
    REVENUE = unit_price * quantity.
    """
    unit_price: Decimal
    quantity: int
    total_amount: Decimal
    currency: str
    price_type: SalePriceType
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    source: str = "MERCADO_LIBRE"

    def __post_init__(self):
        if self.unit_price <= Decimal("0"):
            raise ValueError("unit_price must be greater than zero")
        if self.quantity < 1:
            raise ValueError("quantity must be at least 1")
        expected_total = self.unit_price * Decimal(str(self.quantity))
        if self.total_amount != expected_total:
            raise ValueError(f"total_amount ({self.total_amount}) must equal unit_price * quantity ({expected_total})")


@dataclass(frozen=True)
class LandedCost:
    """
    Costo real de adquisición puesto en destino determinista e inmutable.
    LANDED COST = purchase_price + shipping + duties + taxes + other_acquisition_costs.
    Solo suma componentes conocidos.
    """
    product_id: str
    supplier_id: str
    quantity: int
    currency: str
    purchase_cost: CostComponent
    shipping_cost: CostComponent
    duties_cost: CostComponent
    taxes_cost: CostComponent
    other_acquisition_cost: CostComponent
    total_landed_cost: Optional[Decimal]
    unit_landed_cost: Optional[Decimal]
    status: LandedCostStatus
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    components: Tuple[CostComponent, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.quantity < 1:
            raise ValueError("quantity must be at least 1")
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.components, tuple):
            object.__setattr__(self, "components", tuple(self.components))


@dataclass(frozen=True)
class ProfitResult:
    """
    Resultado determinista del cálculo de profit.
    Distingue GROSS PROFIT (Revenue - Landed Cost) y NET PROFIT (Gross Profit - Channel/Operating Costs).
    """
    revenue: Revenue
    landed_cost: LandedCost
    total_known_costs: Decimal
    gross_profit: Optional[Decimal]
    net_profit: Optional[Decimal]
    status: ProfitStatus
    currency: str
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    cost_breakdown: Tuple[CostComponent, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.cost_breakdown, tuple):
            object.__setattr__(self, "cost_breakdown", tuple(self.cost_breakdown))


@dataclass(frozen=True)
class MarginResult:
    """
    Márgenes y markup deterministas.
    Gross Margin % = ((Revenue - LandedCost) / Revenue) * 100
    Net Margin % = (Net Profit / Revenue) * 100
    Markup % = (Profit / Cost) * 100
    """
    gross_margin_pct: Optional[Decimal]
    net_margin_pct: Optional[Decimal]
    markup_pct: Optional[Decimal]
    is_computable: bool
    formula_explanation: str
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class BreakEvenResult:
    """
    Resultado de punto de equilibrio determinista.
    Calcula el precio mínimo de venta para no perder dinero:
    Break-Even Price = (Unit Landed Cost + Fixed Costs per Unit) / (1 - Variable Fee Rates)
    """
    break_even_sale_price: Optional[Decimal]
    break_even_units: Optional[int]
    target_net_margin_price: Optional[Decimal]  # Precio para alcanzar un margen deseado
    is_computable: bool
    currency: str
    formula_used: str
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_calculable(self) -> bool:
        return self.is_computable

    def __post_init__(self):
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class UnitEconomics:
    """
    Evaluación económica unitaria determinista e inmutable para un producto, proveedor y escenario de cantidad.
    """
    product_id: str
    supplier_id: str
    quantity_scenario: int
    sale_price: SalePrice
    purchase_cost: CostComponent
    shipping_cost: CostComponent
    import_duties: CostComponent
    taxes: CostComponent
    marketplace_fees: CostComponent
    payment_fees: CostComponent
    packaging_cost: CostComponent
    fulfillment_cost: CostComponent
    other_costs: CostComponent
    landed_cost: LandedCost
    gross_profit: Optional[Decimal]
    net_profit: Optional[Decimal]
    gross_margin_pct: Optional[Decimal]
    net_margin_pct: Optional[Decimal]
    unit_markup_pct: Optional[Decimal]
    status: ProfitStatus
    currency: str
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    trace: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.quantity_scenario < 1:
            raise ValueError("quantity_scenario must be at least 1")
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.trace, tuple):
            object.__setattr__(self, "trace", tuple(self.trace))


@dataclass(frozen=True)
class EconomicInvestigationNeed:
    """
    Representa una necesidad de investigación económica cuando falta un componente crítico de costo.
    """
    missing_component: CostComponentType
    impact: str
    priority: str  # HIGH, MEDIUM, LOW
    suggested_action: str

    @property
    def component_type(self) -> CostComponentType:
        return self.missing_component


@dataclass(frozen=True)
class ProfitTrace:
    """
    Trazabilidad inmutable y determinista del cálculo económico.
    Permite auditar el origen y cálculo de cada componente.
    """
    product_id: str
    supplier_id: str
    steps: Tuple[str, ...] = field(default_factory=tuple)
    components_trace: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.steps, tuple):
            object.__setattr__(self, "steps", tuple(self.steps))
        if not isinstance(self.components_trace, tuple):
            frozen_comp = tuple(
                MappingProxyType(c) if isinstance(c, dict) and not isinstance(c, MappingProxyType) else c
                for c in self.components_trace
            )
            object.__setattr__(self, "components_trace", frozen_comp)


@dataclass(frozen=True)
class ScenarioAnalysisResult:
    """
    Análisis de escenarios deterministas (Base, Conservador, Optimista).
    Solo varía parámetros explícitos sin inventar probabilidades.
    """
    base_scenario: UnitEconomics
    conservative_scenario: UnitEconomics
    optimistic_scenario: UnitEconomics
    comparison_summary: str
    scenarios: Mapping[EconomicScenarioType, UnitEconomics] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.scenarios, MappingProxyType):
            object.__setattr__(self, "scenarios", MappingProxyType(dict(self.scenarios)))


@dataclass(frozen=True)
class EconomicEvaluationResult:
    """
    Resultado global de la evaluación económica determinista D-01.
    """
    product_id: str
    supplier_id: str
    primary_unit_economics: UnitEconomics
    quantity_scenarios: Mapping[int, UnitEconomics]
    break_even: BreakEvenResult
    scenarios: Optional[ScenarioAnalysisResult]
    investigation_needs: Tuple[EconomicInvestigationNeed, ...]
    overall_confidence: Confidence
    overall_status: ProfitStatus
    profit_trace: ProfitTrace
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.quantity_scenarios, MappingProxyType):
            object.__setattr__(self, "quantity_scenarios", MappingProxyType(dict(self.quantity_scenarios)))
        if not isinstance(self.investigation_needs, tuple):
            object.__setattr__(self, "investigation_needs", tuple(self.investigation_needs))


@dataclass(frozen=True)
class MarketplaceFeeStructure:
    """
    Estructura determinista de comisiones por categoría / canal de marketplace.
    """
    marketplace: str
    category: str
    fee_rate: Decimal  # Ejemplo: 0.13 para 13%
    fixed_fee: Decimal = Decimal("0")
    fixed_fee_amount: Optional[Decimal] = None
    payment_fee_rate: Decimal = Decimal("0")
    payment_fixed_fee: Decimal = Decimal("0")
    currency: str = "CLP"
    source: str = "OFFICIAL_TARIFF"
    effective_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: Confidence = Confidence.HIGH

    def __post_init__(self):
        if self.fixed_fee_amount is not None and self.fixed_fee == Decimal("0"):
            object.__setattr__(self, "fixed_fee", self.fixed_fee_amount)
        if self.fee_rate < Decimal("0"):
            raise ValueError("fee_rate cannot be negative")
        if self.fixed_fee < Decimal("0"):
            raise ValueError("fixed_fee cannot be negative")
        if self.payment_fee_rate < Decimal("0"):
            raise ValueError("payment_fee_rate cannot be negative")
        if self.payment_fixed_fee < Decimal("0"):
            raise ValueError("payment_fixed_fee cannot be negative")
