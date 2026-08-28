from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.domain.market_intelligence.models import Confidence

@dataclass(frozen=True)
class SupplierData:
    """
    Representa los datos base de un proveedor, extraídos de la fuente de verdad.
    """
    supplier_id: str
    name: str
    country: str
    status: str

@dataclass(frozen=True)
class ConfirmedQuote:
    """
    Representa una cotización comercial confirmada por el proveedor.
    Completa la evidencia cuando los datos de mercado son parciales.
    """
    quote_id: str
    wholesale_price: Decimal
    shipping_cost: Decimal
    lead_time_days: int
    currency: str = "CLP"

    def __post_init__(self):
        if not self.quote_id:
            raise ValueError("quote_id must be valid")
        if self.wholesale_price <= 0:
            raise ValueError("wholesale_price must be greater than zero")
        if self.shipping_cost < 0:
            raise ValueError("shipping_cost cannot be negative")
        if self.lead_time_days < 0:
            raise ValueError("lead_time_days cannot be negative")

@dataclass(frozen=True)
class SupplierEvidence:
    """
    Evidencia inmutable del mercado de proveedores.
    No toma decisiones (como si el proveedor es rentable o no), solo transporta evidencia.
    """
    supplier_id: str
    sku: str
    wholesale_price: Decimal
    currency: str
    minimum_order_quantity: int
    stock_available: bool
    shipping_cost: Optional[Decimal]
    lead_time_days: Optional[int]
    confidence: Confidence = Confidence.UNKNOWN
    quote: Optional[ConfirmedQuote] = None

    def __post_init__(self):
        if not self.supplier_id:
            raise ValueError("supplier_id must be valid")
        if not self.sku:
            raise ValueError("sku must be valid")
        if self.wholesale_price <= 0:
            raise ValueError("wholesale_price must be greater than zero")
        if self.minimum_order_quantity < 1:
            raise ValueError("minimum_order_quantity must be at least 1")

        # Regla crítica: None != 0. shipping_cost puede ser None (desconocido) o 0 (envío gratis).
        if self.shipping_cost is not None and self.shipping_cost < 0:
            raise ValueError("shipping_cost cannot be negative")

        # Regla crítica: None != 0. lead_time_days puede ser None (desconocido) o >= 0.
        if self.lead_time_days is not None and self.lead_time_days < 0:
            raise ValueError("lead_time_days cannot be negative")
