import json
from decimal import Decimal
from typing import Optional, List, Sequence, Dict, Any
from pathlib import Path
from datetime import datetime, timezone

from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    ConfirmedQuote,
)
from src.domain.supplier_intelligence.ports import SupplierSource
from src.domain.supplier_intelligence.services import ProductMatcher, SupplierNormalizer


class DirectorySupplierSource(SupplierSource):
    """
    Adapter de infraestructura que busca proveedores en un directorio de archivos JSON de catálogos.
    Cumple con el contrato SupplierSource desacoplado.
    """

    def __init__(
        self,
        directory_path: str,
        source_name_override: Optional[str] = None,
        source_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE,
    ):
        self.directory_path = Path(directory_path)
        self._source_name = source_name_override or f"DIRECTORY_{self.directory_path.name}"
        self.source_type = source_type

    @property
    def source_name(self) -> str:
        return self._source_name

    def search_suppliers(
        self,
        query: str,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        sku: Optional[str] = None,
        limit: int = 10,
    ) -> Sequence[SupplierCandidate]:
        candidates: List[SupplierCandidate] = []
        if not self.directory_path.exists() or not self.directory_path.is_dir():
            return candidates

        for file_path in self.directory_path.glob("*.json"):
            if len(candidates) >= limit:
                break
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                cand = self._parse_json_candidate(data, query, brand, model, sku)
                if cand is not None:
                    candidates.append(cand)
            except Exception:
                continue

        return candidates

    def _parse_json_candidate(
        self,
        data: Dict[str, Any],
        query: str,
        brand: Optional[str],
        model: Optional[str],
        sku: Optional[str],
    ) -> Optional[SupplierCandidate]:
        supplier_id = data.get("supplier_id")
        if not supplier_id:
            return None

        company = data.get("company", {})
        contact = data.get("contact", {})
        product = data.get("product", {})
        pricing = data.get("pricing", {})
        stock = data.get("stock", {})
        commercial = data.get("commercial", {})
        logistics = data.get("logistics", {})

        s_title = product.get("title") or f"{product.get('brand', '')} {product.get('model', '')} {product.get('sku', '')}".strip()
        s_brand = product.get("brand")
        s_model = product.get("model")
        s_sku = product.get("sku") or supplier_id

        # Product Match
        match = ProductMatcher.match(
            target_title=query,
            target_brand=brand,
            target_model=model,
            target_sku=sku,
            supplier_sku=s_sku,
            supplier_title=s_title,
            supplier_brand=s_brand,
            supplier_model=s_model,
        )

        country = company.get("country") or logistics.get("ships_from") or "Chile"

        supplier_entity = Supplier(
            supplier_id=supplier_id,
            name=company.get("name") or f"Supplier {supplier_id}",
            source=self.source_name,
            source_type=self.source_type,
            location=SupplierLocation(country=country),
            contact=SupplierContact(
                name=contact.get("name"),
                email=contact.get("email"),
                phone=contact.get("phone"),
                website=company.get("website"),
            ),
            status=SupplierStatus(data.get("supplier_status", "RESEARCH")),
            product_reference=SupplierProductReference(
                sku=s_sku,
                title=s_title,
                brand=s_brand,
                model=s_model,
                category=product.get("category"),
            ),
        )

        raw_wholesale = pricing.get("wholesale_price_clp")
        wholesale_price = Decimal(str(raw_wholesale)) if raw_wholesale is not None else None

        raw_shipping = logistics.get("shipping_cost_clp")
        shipping_cost = Decimal(str(raw_shipping)) if raw_shipping is not None else None

        raw_delivery = logistics.get("delivery_time_days")
        delivery_time_days = int(raw_delivery) if raw_delivery is not None else None

        moq = commercial.get("minimum_order_quantity")
        moq_int = int(moq) if moq is not None else None

        quote = None
        if data.get("confirmed_quote"):
            q_d = data["confirmed_quote"]
            quote = ConfirmedQuote(
                quote_id=q_d.get("quote_id", "Q-UNKNOWN"),
                wholesale_price=Decimal(str(q_d.get("wholesale_price_clp", 0))),
                shipping_cost=Decimal(str(q_d.get("shipping_cost_clp", 0))),
                lead_time_days=int(q_d.get("delivery_time_days", 0)),
                currency=q_d.get("currency", "CLP"),
            )

        evidence = SupplierEvidence(
            supplier_id=supplier_id,
            sku=s_sku,
            wholesale_price=wholesale_price,
            currency=pricing.get("currency", "CLP"),
            minimum_order_quantity=moq_int,
            stock_available=stock.get("available"),
            shipping_cost=shipping_cost,
            lead_time_days=delivery_time_days,
            confidence=Confidence.HIGH if self.source_type == EvidenceProvenanceType.LIVE else Confidence.MEDIUM,
            signal_type=SignalType.OBSERVED,
            provenance_type=self.source_type,
            source=self.source_name,
            raw_payload=data,
            quote=quote,
        )

        unknowns: List[str] = []
        if wholesale_price is None and quote is None:
            unknowns.append("wholesale_price")
        if stock.get("available") is None:
            unknowns.append("stock_available")
        if shipping_cost is None and (quote is None or quote.shipping_cost is None):
            unknowns.append("shipping_cost")
        if delivery_time_days is None and (quote is None or quote.lead_time_days is None):
            unknowns.append("lead_time_days")
        if moq_int is None:
            unknowns.append("minimum_order_quantity")

        risks: List[str] = []
        if stock.get("available") is False:
            risks.append("OUT_OF_STOCK")
        if supplier_entity.status == SupplierStatus.UNVERIFIED:
            risks.append("UNVERIFIED_SUPPLIER")

        return SupplierCandidate(
            supplier=supplier_entity,
            evidence=evidence,
            product_match=match,
            readiness=SupplierReadiness.DISCOVERED,
            unknowns=tuple(unknowns),
            risks=tuple(risks),
        )
