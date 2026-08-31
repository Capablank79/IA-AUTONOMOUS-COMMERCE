import json
from pathlib import Path
from decimal import Decimal
from typing import Union, Optional, List, Sequence, Dict
from datetime import datetime, timezone

from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    SupplierStatus,
    EvidenceProvenanceType,
    ConfirmedQuote,
)
from src.domain.supplier_intelligence.ports import SupplierRepository
from src.domain.market_intelligence.models import Confidence, SignalType


class JsonSupplierRepository(SupplierRepository):
    """
    Adapter de infraestructura para persistencia de proveedores y evidencia en JSON.
    Totalmente compatible con SupplierRepository port.
    """

    def __init__(self, storage_dir: Union[Path, str]):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._suppliers_dir = self.storage_dir / "suppliers"
        self._evidences_dir = self.storage_dir / "evidences"
        self._suppliers_dir.mkdir(parents=True, exist_ok=True)
        self._evidences_dir.mkdir(parents=True, exist_ok=True)

    def save_supplier(self, supplier: Supplier) -> None:
        file_path = self._suppliers_dir / f"{supplier.supplier_id}.json"
        data = {
            "supplier_id": supplier.supplier_id,
            "name": supplier.name,
            "source": supplier.source,
            "source_type": supplier.source_type.value,
            "status": supplier.status.value,
            "observed_at": supplier.observed_at.isoformat(),
            "location": {
                "country": supplier.location.country,
                "city": supplier.location.city,
                "region": supplier.location.region,
            } if supplier.location else None,
            "contact": {
                "name": supplier.contact.name,
                "email": supplier.contact.email,
                "phone": supplier.contact.phone,
                "website": supplier.contact.website,
            } if supplier.contact else None,
            "product_reference": {
                "sku": supplier.product_reference.sku,
                "title": supplier.product_reference.title,
                "brand": supplier.product_reference.brand,
                "model": supplier.product_reference.model,
                "category": supplier.product_reference.category,
                "source_product_id": supplier.product_reference.source_product_id,
                "source_url": supplier.product_reference.source_url,
            } if supplier.product_reference else None,
            "metadata": dict(supplier.metadata),
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_supplier(self, supplier_id: str) -> Optional[Supplier]:
        file_path = self._suppliers_dir / f"{supplier_id}.json"
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            location = None
            if data.get("location"):
                loc_d = data["location"]
                location = SupplierLocation(
                    country=loc_d["country"],
                    city=loc_d.get("city"),
                    region=loc_d.get("region"),
                )

            contact = None
            if data.get("contact"):
                con_d = data["contact"]
                contact = SupplierContact(
                    name=con_d.get("name"),
                    email=con_d.get("email"),
                    phone=con_d.get("phone"),
                    website=con_d.get("website"),
                )

            prod_ref = None
            if data.get("product_reference"):
                p_d = data["product_reference"]
                prod_ref = SupplierProductReference(
                    sku=p_d.get("sku"),
                    title=p_d.get("title"),
                    brand=p_d.get("brand"),
                    model=p_d.get("model"),
                    category=p_d.get("category"),
                    source_product_id=p_d.get("source_product_id"),
                    source_url=p_d.get("source_url"),
                )

            return Supplier(
                supplier_id=data["supplier_id"],
                name=data["name"],
                source=data["source"],
                source_type=EvidenceProvenanceType(data["source_type"]),
                location=location,
                contact=contact,
                status=SupplierStatus(data.get("status", "RESEARCH")),
                observed_at=datetime.fromisoformat(data["observed_at"]),
                product_reference=prod_ref,
                metadata=data.get("metadata", {}),
            )
        except Exception:
            return None

    def save_evidence(self, evidence: SupplierEvidence) -> None:
        clean_sku = "".join(c for c in evidence.sku if c.isalnum() or c in ("-", "_"))
        file_path = self._evidences_dir / f"{evidence.supplier_id}_{clean_sku}.json"
        quote_data = None
        if evidence.quote:
            quote_data = {
                "quote_id": evidence.quote.quote_id,
                "wholesale_price": str(evidence.quote.wholesale_price),
                "shipping_cost": str(evidence.quote.shipping_cost),
                "lead_time_days": evidence.quote.lead_time_days,
                "currency": evidence.quote.currency,
            }

        data = {
            "supplier_id": evidence.supplier_id,
            "sku": evidence.sku,
            "wholesale_price": str(evidence.wholesale_price) if evidence.wholesale_price is not None else None,
            "currency": evidence.currency,
            "minimum_order_quantity": evidence.minimum_order_quantity,
            "stock_available": evidence.stock_available,
            "shipping_cost": str(evidence.shipping_cost) if evidence.shipping_cost is not None else None,
            "lead_time_days": evidence.lead_time_days,
            "confidence": evidence.confidence.value,
            "signal_type": evidence.signal_type.value,
            "provenance_type": evidence.provenance_type.value,
            "source": evidence.source,
            "observed_at": evidence.observed_at.isoformat(),
            "raw_payload": dict(evidence.raw_payload),
            "quote": quote_data,
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_evidence(self, supplier_id: str, sku: str) -> Optional[SupplierEvidence]:
        clean_sku = "".join(c for c in sku if c.isalnum() or c in ("-", "_"))
        file_path = self._evidences_dir / f"{supplier_id}_{clean_sku}.json"
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            quote = None
            if data.get("quote"):
                q_d = data["quote"]
                quote = ConfirmedQuote(
                    quote_id=q_d["quote_id"],
                    wholesale_price=Decimal(q_d["wholesale_price"]),
                    shipping_cost=Decimal(q_d["shipping_cost"]),
                    lead_time_days=int(q_d["lead_time_days"]),
                    currency=q_d.get("currency", "CLP"),
                )

            wholesale_price = Decimal(data["wholesale_price"]) if data.get("wholesale_price") is not None else None
            shipping_cost = Decimal(data["shipping_cost"]) if data.get("shipping_cost") is not None else None

            return SupplierEvidence(
                supplier_id=data["supplier_id"],
                sku=data["sku"],
                wholesale_price=wholesale_price,
                currency=data.get("currency", "CLP"),
                minimum_order_quantity=data.get("minimum_order_quantity"),
                stock_available=data.get("stock_available"),
                shipping_cost=shipping_cost,
                lead_time_days=data.get("lead_time_days"),
                confidence=Confidence(data.get("confidence", "UNKNOWN")),
                signal_type=SignalType(data.get("signal_type", "OBSERVED")),
                provenance_type=EvidenceProvenanceType(data.get("provenance_type", "FIXTURE")),
                source=data.get("source", "INTERNAL_CATALOG"),
                observed_at=datetime.fromisoformat(data["observed_at"]),
                raw_payload=data.get("raw_payload", {}),
                quote=quote,
            )
        except Exception:
            return None

    def list_suppliers(self, limit: int = 100) -> Sequence[Supplier]:
        suppliers: List[Supplier] = []
        for p in self._suppliers_dir.glob("*.json"):
            if len(suppliers) >= limit:
                break
            sup = self.get_supplier(p.stem)
            if sup:
                suppliers.append(sup)
        return suppliers
