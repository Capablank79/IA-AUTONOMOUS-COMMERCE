import json
from decimal import Decimal
from typing import Optional, List, Sequence
from pathlib import Path
from datetime import datetime, timezone

from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierData,
    SupplierEvidence,
    ConfirmedQuote,
    SupplierCandidate,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    SupplierStatus,
    EvidenceProvenanceType,
)
from src.domain.supplier_intelligence.ports import SupplierDataSource, SupplierSource
from src.domain.supplier_intelligence.services import ProductMatcher


class JsonSupplierDataSource(SupplierDataSource, SupplierSource):
    """
    Implementación multi-propósito de SupplierDataSource y SupplierSource basada en archivos JSON estructurados.
    Preserva procedencia (FIXTURE / CATALOG), frescura y no inventa datos.
    """
    def __init__(self, directory_path: str, source_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE):
        self.directory_path = Path(directory_path)
        self.source_type = source_type

    @property
    def source_name(self) -> str:
        return f"JSON_CATALOG_{self.directory_path.name}"

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

            company_data = data.get("company", {})
            contact_data = data.get("contact", {})
            country = company_data.get("country", "")

            return SupplierData(
                supplier_id=data["supplier_id"],
                name=company_data.get("name", ""),
                country=country,
                status=data.get("supplier_status", "UNKNOWN"),
                location=SupplierLocation(country=country if country else "Chile"),
                contact=SupplierContact(
                    name=contact_data.get("name"),
                    email=contact_data.get("email"),
                    phone=contact_data.get("phone"),
                    website=company_data.get("website")
                ),
                source=self.source_name,
                source_type=self.source_type,
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

            raw_wholesale = pricing_data.get("wholesale_price_clp")
            wholesale_price = Decimal(str(raw_wholesale)) if raw_wholesale is not None else None

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
                wholesale_price=wholesale_price,
                currency=pricing_data.get("currency", "CLP"),
                minimum_order_quantity=int(commercial_data.get("minimum_order_quantity", 1)) if commercial_data.get("minimum_order_quantity") is not None else None,
                stock_available=stock_data.get("available"),
                shipping_cost=shipping_cost,
                lead_time_days=delivery_time_days,
                confidence=Confidence.HIGH,
                signal_type=SignalType.OBSERVED,
                provenance_type=self.source_type,
                source=self.source_name,
                raw_payload=data,
                quote=quote
            )
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def search_suppliers(
        self,
        query: str,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        sku: Optional[str] = None,
        limit: int = 10,
    ) -> Sequence[SupplierCandidate]:
        """
        Escanea el directorio de catálogo y genera candidatos evaluando matching del producto.
        """
        candidates: List[SupplierCandidate] = []
        if not self.directory_path.exists() or not self.directory_path.is_dir():
            return candidates

        for file_path in self.directory_path.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue

            sup_id = data.get("supplier_id")
            if not sup_id:
                continue

            company_data = data.get("company", {})
            contact_data = data.get("contact", {})
            product_data = data.get("product", {})
            pricing_data = data.get("pricing", {})
            stock_data = data.get("stock", {})
            commercial_data = data.get("commercial", {})
            logistics_data = data.get("logistics", {})
            country = company_data.get("country", "")

            # Construir entidad Supplier
            supplier_ent = Supplier(
                supplier_id=sup_id,
                name=company_data.get("name", f"Supplier {sup_id}"),
                source=self.source_name,
                source_type=self.source_type,
                location=SupplierLocation(country=country if country else "Chile"),
                contact=SupplierContact(
                    name=contact_data.get("name"),
                    email=contact_data.get("email"),
                    phone=contact_data.get("phone"),
                    website=company_data.get("website")
                ),
                status=SupplierStatus(data.get("supplier_status", "RESEARCH")) if data.get("supplier_status") in SupplierStatus.__members__ else SupplierStatus.RESEARCH,
                product_reference=SupplierProductReference(
                    sku=product_data.get("sku"),
                    title=product_data.get("title") or product_data.get("name") or f"{product_data.get('brand', '')} {product_data.get('model', '')}".strip(),
                    brand=product_data.get("brand"),
                    model=product_data.get("model"),
                    category=product_data.get("category"),
                ),
                metadata=data.get("metadata", {})
            )

            # Evidencia
            shipping_cost_raw = logistics_data.get("shipping_cost_clp")
            shipping_cost = Decimal(str(shipping_cost_raw)) if shipping_cost_raw is not None else None
            delivery_time_raw = logistics_data.get("delivery_time_days")
            delivery_time_days = int(delivery_time_raw) if delivery_time_raw is not None else None

            raw_wholesale = pricing_data.get("wholesale_price_clp")
            wholesale_price = Decimal(str(raw_wholesale)) if raw_wholesale is not None else None

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

            item_sku = product_data.get("sku") or "UNKNOWN_SKU"
            evidence = SupplierEvidence(
                supplier_id=sup_id,
                sku=item_sku,
                wholesale_price=wholesale_price,
                currency=pricing_data.get("currency", "CLP"),
                minimum_order_quantity=int(commercial_data.get("minimum_order_quantity", 1)) if commercial_data.get("minimum_order_quantity") is not None else None,
                stock_available=stock_data.get("available"),
                shipping_cost=shipping_cost,
                lead_time_days=delivery_time_days,
                confidence=Confidence.HIGH if self.source_type == EvidenceProvenanceType.LIVE else Confidence.MEDIUM,
                signal_type=SignalType.OBSERVED,
                provenance_type=self.source_type,
                source=self.source_name,
                raw_payload=data,
                quote=quote
            )

            # Product Matching
            match = ProductMatcher.match(
                target_title=query,
                target_brand=brand,
                target_model=model,
                target_sku=sku,
                supplier_sku=product_data.get("sku"),
                supplier_title=product_data.get("title") or f"{product_data.get('brand', '')} {product_data.get('model', '')}".strip(),
                supplier_brand=product_data.get("brand"),
                supplier_model=product_data.get("model"),
            )

            cand = SupplierCandidate(
                supplier=supplier_ent,
                evidence=evidence,
                product_match=match,
            )
            candidates.append(cand)

        return candidates[:limit]
