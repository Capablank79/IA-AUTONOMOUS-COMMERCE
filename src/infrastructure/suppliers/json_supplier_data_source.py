import json
from decimal import Decimal
from typing import Optional
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import SupplierData, SupplierEvidence, ConfirmedQuote
from src.domain.supplier_intelligence.ports import SupplierDataSource

class JsonSupplierDataSource(SupplierDataSource):
    """
    Implementación del puerto SupplierDataSource basada en archivos JSON.
    El JSON es la única fuente de verdad, sin scraping ni APIs externas.
    """
    def __init__(self, directory_path: str):
        self.directory_path = Path(directory_path)

    def _get_file_path(self, supplier_id: str) -> Path:
        numeric_id = supplier_id.split("-")[-1]
        return self.directory_path / f"supplier_{numeric_id}.json"

    def get_supplier_data(self, supplier_id: str) -> Optional[SupplierData]:
        file_path = self._get_file_path(supplier_id)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("supplier_id") != supplier_id:
                return None

            return SupplierData(
                supplier_id=data["supplier_id"],
                name=data.get("company", {}).get("name", ""),
                country=data.get("company", {}).get("country", ""),
                status=data.get("supplier_status", "UNKNOWN")
            )
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def get_supplier_evidence(self, supplier_id: str, sku: str) -> Optional[SupplierEvidence]:
        file_path = self._get_file_path(supplier_id)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("supplier_id") != supplier_id:
                return None

            product_data = data.get("product", {})
            if product_data.get("sku") != sku:
                return None

            pricing_data = data.get("pricing", {})
            stock_data = data.get("stock", {})
            commercial_data = data.get("commercial", {})
            logistics_data = data.get("logistics", {})

            shipping_cost_raw = logistics_data.get("shipping_cost_clp")
            shipping_cost = Decimal(str(shipping_cost_raw)) if shipping_cost_raw is not None else None

            delivery_time_raw = logistics_data.get("delivery_time_days")
            delivery_time_days = int(delivery_time_raw) if delivery_time_raw is not None else None

            # Carga de cotización confirmada si existe
            quote = None
            quote_data = data.get("confirmed_quote")
            if quote_data:
                quote = ConfirmedQuote(
                    quote_id=quote_data.get("quote_id", "Q-UNKNOWN"),
                    wholesale_price=Decimal(str(quote_data.get("wholesale_price_clp", 0))),
                    shipping_cost=Decimal(str(quote_data.get("shipping_cost_clp", 0))),
                    lead_time_days=int(quote_data.get("delivery_time_days", 0)),
                    currency=quote_data.get("currency", "CLP")
                )

            return SupplierEvidence(
                supplier_id=data["supplier_id"],
                sku=sku,
                wholesale_price=Decimal(str(pricing_data.get("wholesale_price_clp", 0))),
                currency=pricing_data.get("currency", "CLP"),
                minimum_order_quantity=int(commercial_data.get("minimum_order_quantity", 1)),
                stock_available=bool(stock_data.get("available", False)),
                shipping_cost=shipping_cost,
                lead_time_days=delivery_time_days,
                confidence=Confidence.HIGH,  # Ya que proviene de nuestra fuente de verdad interna
                quote=quote
            )
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            return None
