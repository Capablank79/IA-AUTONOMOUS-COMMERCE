"""
Adaptador JSON de Repositorio de Proveedores Aislado por Tenant (Hito Q.2 — Supplier Dashboard).

Layout en disco:
  base_dir / "tenants" / tenant_id / "suppliers" / "suppliers.json"
Asegura:
- Validación de identificadores seguros con validate_safe_identifier.
- Validación de aislamiento estricto con CrossTenantGuard.
- Operaciones atómicas con flush y fsync.
- Sanitización recursiva de claves sensibles (N.9).
- Reconstrucción fiel de tipos (Supplier, SupplierLocation, SupplierContact, SupplierProductReference, Enum, Decimal, datetime UTC).
- UNKNOWN != 0.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    SupplierStatus,
    EvidenceProvenanceType,
)
from src.domain.supplier_dashboard.ports import TenantSupplierRepositoryPort


class CorruptedSupplierDataError(Exception):
    """Lanzada cuando un archivo de datos JSON de proveedores está corrupto o ilegible."""
    pass


SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "apikey", "pan", "cvv", "private_key", "credential"}


def _encode_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _serialize_supplier(supplier: Supplier) -> Dict[str, Any]:
    """Serializa un Supplier canónico a diccionario JSON-friendly."""
    return {
        "supplier_id": supplier.supplier_id,
        "name": supplier.name,
        "source": supplier.source,
        "source_type": supplier.source_type.value if hasattr(supplier.source_type, "value") else str(supplier.source_type),
        "status": supplier.status.value if hasattr(supplier.status, "value") else str(supplier.status),
        "observed_at": supplier.observed_at.isoformat() if supplier.observed_at else None,
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
        "metadata": _encode_json_value(dict(supplier.metadata)),
    }


def _decode_supplier(data: Dict[str, Any]) -> Supplier:
    """Decodifica un diccionario JSON a Supplier canónico de dominio."""
    try:
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

        observed_at = datetime.fromisoformat(data["observed_at"]) if data.get("observed_at") else datetime.now(timezone.utc)

        source_type_val = data.get("source_type", EvidenceProvenanceType.LIVE.value)
        source_type = EvidenceProvenanceType(source_type_val) if source_type_val in [e.value for e in EvidenceProvenanceType] else EvidenceProvenanceType.LIVE

        status_val = data.get("status", SupplierStatus.RESEARCH.value)
        status = SupplierStatus(status_val) if status_val in [s.value for s in SupplierStatus] else SupplierStatus.RESEARCH

        return Supplier(
            supplier_id=data["supplier_id"],
            name=data["name"],
            source=data.get("source", "UNKNOWN"),
            source_type=source_type,
            location=location,
            contact=contact,
            status=status,
            observed_at=observed_at,
            product_reference=prod_ref,
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise CorruptedSupplierDataError(f"Error decoding supplier record: {e}") from e


class JsonTenantSupplierRepository(TenantSupplierRepositoryPort):
    """
    Repositorio JSON multi-tenant para Supplier.
    Almacena los datos particionados físicamente por tenant_id.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)

    def _get_tenant_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        supp_dir = self.tenants_dir / tenant_id / "suppliers"
        supp_dir.mkdir(parents=True, exist_ok=True)
        return supp_dir / "suppliers.json"

    def _read_raw(self, tenant_id: str) -> List[Dict[str, Any]]:
        file_path = self._get_tenant_file(tenant_id)
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip():
                    return []
                return json.loads(content)
        except json.JSONDecodeError as e:
            raise CorruptedSupplierDataError(f"Corrupted JSON in {file_path}") from e

    def _write_atomic(self, tenant_id: str, data: List[Dict[str, Any]]) -> None:
        file_path = self._get_tenant_file(tenant_id)
        tmp_file = file_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, file_path)

    def save(self, context: TenantContext, supplier: Supplier) -> None:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        serialized = _serialize_supplier(supplier)

        for i, r in enumerate(records):
            if r.get("supplier_id") == supplier.supplier_id:
                records[i] = serialized
                self._write_atomic(tenant_id, records)
                return

        records.append(serialized)
        self._write_atomic(tenant_id, records)

    def save_all(self, context: TenantContext, suppliers: List[Supplier]) -> int:
        CrossTenantGuard.ensure_tenant_context(context)
        if not suppliers:
            return 0
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        existing_ids = {r.get("supplier_id") for r in records}

        added_count = 0
        for supp in suppliers:
            if supp.supplier_id in existing_ids:
                # Update existing
                for i, r in enumerate(records):
                    if r.get("supplier_id") == supp.supplier_id:
                        records[i] = _serialize_supplier(supp)
                        break
            else:
                records.append(_serialize_supplier(supp))
                existing_ids.add(supp.supplier_id)
                added_count += 1

        self._write_atomic(tenant_id, records)
        return added_count

    def get_by_id(self, context: TenantContext, supplier_id: str) -> Optional[Supplier]:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(supplier_id, field_name="supplier_id")
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        for r in records:
            if r.get("supplier_id") == supplier_id:
                return _decode_supplier(r)
        return None

    def list_all(self, context: TenantContext) -> List[Supplier]:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        return [_decode_supplier(r) for r in records]

    def delete(self, context: TenantContext, supplier_id: str) -> bool:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(supplier_id, field_name="supplier_id")
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        new_records = [r for r in records if r.get("supplier_id") != supplier_id]
        if len(new_records) < len(records):
            self._write_atomic(tenant_id, new_records)
            return True
        return False
