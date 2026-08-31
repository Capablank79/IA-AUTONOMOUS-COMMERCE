from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Mapping, Any, Tuple, Union, Sequence
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.publication.models import SalesChannel


class InventoryChangeReason(str, Enum):
    """
    Razón comercial/operativa para el cambio o ajuste de inventario.
    """
    SUPPLIER_SYNC = "SUPPLIER_SYNC"
    STOCK_REDUCTION = "STOCK_REDUCTION"
    SAFETY_BUFFER_ADJUSTMENT = "SAFETY_BUFFER_ADJUSTMENT"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    OUT_OF_STOCK_PROTECTION = "OUT_OF_STOCK_PROTECTION"
    RECONCILIATION = "RECONCILIATION"
    POLICY_CORRECTION = "POLICY_CORRECTION"
    REPLENISHMENT = "REPLENISHMENT"


class InventoryStatus(str, Enum):
    """
    Ciclo de vida y estado de una acción de actualización de stock.
    UNKNOWN es un estado de primera clase: timeout o respuesta ambigua
    requiere verificación previa (VERIFY_CURRENT_STOCK / RECONCILE) antes de reintentar.
    """
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class InventoryErrorCategory(str, Enum):
    """
    Categorías taxonómicas deterministas de error en operaciones de inventario.
    """
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class InventoryError:
    """
    Error estructurado resultante de un intento de consulta o cambio de stock.
    """
    category: InventoryErrorCategory
    message: str
    code: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self):
        if not self.message or not self.message.strip():
            raise ValueError("InventoryError message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class StockLevel:
    """
    Representación semántica e inmutable de los niveles de stock (Source of Truth).
    
    Aislamiento y Semántica de Stock:
    - supplier_stock: cuánto tiene el proveedor respaldado por evidencia.
    - owned_stock: stock propio/físico disponible en bodega propia.
    - reserved_stock: unidades reservadas por órdenes pendientes o compromisos operativos.
    - safety_buffer: unidades de amortiguación de riesgo / buffer de seguridad.
    - in_transit_stock: unidades en tránsito desde proveedor o entre bodegas.
    - listed_stock: unidades actualmente publicadas en el canal externo.
    
    Fórmula determinista de Available Stock (Sellable):
    available_stock = max(0, (owned_stock + supplier_stock) - reserved_stock - safety_buffer)
    """
    supplier_stock: int = 0
    owned_stock: int = 0
    reserved_stock: int = 0
    safety_buffer: int = 0
    in_transit_stock: int = 0
    listed_stock: Optional[int] = None

    def __post_init__(self):
        if self.supplier_stock < 0:
            raise ValueError("supplier_stock cannot be negative")
        if self.owned_stock < 0:
            raise ValueError("owned_stock cannot be negative")
        if self.reserved_stock < 0:
            raise ValueError("reserved_stock cannot be negative")
        if self.safety_buffer < 0:
            raise ValueError("safety_buffer cannot be negative")
        if self.in_transit_stock < 0:
            raise ValueError("in_transit_stock cannot be negative")
        if self.listed_stock is not None and self.listed_stock < 0:
            raise ValueError("listed_stock cannot be negative")

    @property
    def total_backed_stock(self) -> int:
        """Total de stock respaldado real (físico propio + proveedor verificado)."""
        return self.owned_stock + self.supplier_stock

    @property
    def total_commitments(self) -> int:
        """Total de deducciones por reservas y buffer de seguridad."""
        return self.reserved_stock + self.safety_buffer

    @property
    def available_to_sell(self) -> int:
        """
        Stock disponible determinista para venta / publicación.
        Protección contra overselling: nunca retorna un valor negativo.
        """
        calculated = self.total_backed_stock - self.total_commitments
        return max(0, calculated)

    @property
    def max_sellable_quantity(self) -> int:
        """Cantidad máxima vendible garantizada sin incurrir en overselling."""
        return self.available_to_sell

    def is_overselling(self, proposed_quantity: int) -> bool:
        """Determina si una cantidad propuesta excede el stock vendible respaldado."""
        if proposed_quantity < 0:
            return True
        return proposed_quantity > self.available_to_sell


@dataclass(frozen=True)
class InventoryDecision:
    """
    Representación estructurada e inmutable de una decisión de stock/inventario.
    
    Aislamiento y Principios:
    - No guarda sólo un número entero: contiene contexto de inventario, evidencia, justificación y riesgos.
    - Respeta la protección determinista contra overselling (max_allowed_stock = available_to_sell).
    - Permite auditoría de por qué y con qué evidencia se tomó la decisión.
    """
    decision_id: str
    listing_id: str
    channel: SalesChannel
    current_stock: int
    proposed_stock: int
    stock_levels: StockLevel
    product_id: Optional[str] = None
    reason: InventoryChangeReason = InventoryChangeReason.SUPPLIER_SYNC
    rationale: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.HIGH
    risk_level: RiskLevel = RiskLevel.LOW
    constraints: Mapping[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.decision_id or not self.decision_id.strip():
            raise ValueError("decision_id cannot be empty")
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        if self.current_stock < 0:
            raise ValueError("current_stock cannot be negative")
        if not isinstance(self.evidence, MappingProxyType):
            object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        if not isinstance(self.constraints, MappingProxyType):
            object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))

    @property
    def proposed_quantity(self) -> int:
        return self.proposed_stock

    @property
    def current_quantity(self) -> int:
        return self.current_stock

    @property
    def max_available_quantity(self) -> int:
        return self.stock_levels.available_to_sell

    @property
    def is_overselling(self) -> bool:
        return self.stock_levels.is_overselling(self.proposed_stock)

    @property
    def stock_delta(self) -> int:
        return self.proposed_stock - self.current_stock

    @property
    def is_overselling_risk(self) -> bool:
        return self.is_overselling


@dataclass(frozen=True)
class InventoryAction:
    """
    Intención explícita de acción de actualización o sincronización de stock ejecutable por ActionExecutor.
    Desacoplada del motor de análisis y de la infraestructura HTTP/API.
    """
    action_id: str
    decision_id: str
    listing_id: str
    channel: SalesChannel
    proposed_stock: Optional[int] = None
    current_stock: Optional[int] = None
    old_quantity: Optional[int] = None
    new_quantity: Optional[int] = None
    reason: Union[InventoryChangeReason, str] = InventoryChangeReason.SUPPLIER_SYNC
    request_id: str = ""
    idempotency_key: str = ""
    correlation_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.action_id or not self.action_id.strip():
            raise ValueError("action_id cannot be empty")
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        
        target_new = self.new_quantity if self.new_quantity is not None else self.proposed_stock
        if target_new is None or target_new < 0:
            raise ValueError("proposed_stock/new_quantity cannot be negative or None")
            
        if self.new_quantity is None and self.proposed_stock is not None:
            object.__setattr__(self, "new_quantity", self.proposed_stock)
        if self.proposed_stock is None and self.new_quantity is not None:
            object.__setattr__(self, "proposed_stock", self.new_quantity)

        if self.old_quantity is None and self.current_stock is not None:
            object.__setattr__(self, "old_quantity", self.current_stock)
        if self.current_stock is None and self.old_quantity is not None:
            object.__setattr__(self, "current_stock", self.old_quantity)

    @property
    def proposed_quantity(self) -> int:
        return self.new_quantity or 0

    @property
    def current_quantity(self) -> Optional[int]:
        return self.old_quantity


@dataclass(frozen=True)
class InventoryRequest:
    """
    DTO tipado de entrada para la ejecución de una acción de inventario en el puerto de salida.
    """
    request_id: str
    listing_id: str
    proposed_quantity: int
    current_quantity: Optional[int] = None
    channel: Optional[SalesChannel] = None
    idempotency_key: Optional[str] = None
    correlation_id: Optional[str] = None
    reason: Union[InventoryChangeReason, str] = InventoryChangeReason.SUPPLIER_SYNC
    metadata: Mapping[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.request_id or not self.request_id.strip():
            raise ValueError("request_id cannot be empty")
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        if self.proposed_quantity < 0:
            raise ValueError("proposed_quantity cannot be negative")
        if self.current_quantity is not None and self.current_quantity < 0:
            raise ValueError("current_quantity cannot be negative")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class InventoryResult:
    """
    Resultado estructurado e inmutable de la ejecución o verificación de una acción de inventario.
    """
    inventory_id: Optional[str]
    listing_id: str
    channel: SalesChannel
    status: InventoryStatus
    applied_quantity: Optional[int] = None
    previous_quantity: Optional[int] = None
    external_reference: Optional[str] = None
    errors: Sequence[InventoryError] = field(default_factory=tuple)
    raw_response: Optional[Mapping[str, Any]] = None
    request_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.HIGH
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        if self.applied_quantity is not None and self.applied_quantity < 0:
            raise ValueError("applied_quantity cannot be negative")
        if self.previous_quantity is not None and self.previous_quantity < 0:
            raise ValueError("previous_quantity cannot be negative")
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))
        if self.raw_response is not None and not isinstance(self.raw_response, MappingProxyType):
            object.__setattr__(self, "raw_response", MappingProxyType(dict(self.raw_response)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def is_success(self) -> bool:
        return self.status == InventoryStatus.APPLIED

    @property
    def is_unknown(self) -> bool:
        return self.status == InventoryStatus.UNKNOWN

    @property
    def is_failed(self) -> bool:
        return self.status == InventoryStatus.FAILED
