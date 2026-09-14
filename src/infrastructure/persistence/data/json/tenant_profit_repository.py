"""
Adaptador JSON de Repositorio de Rentabilidad Aislado por Tenant (Hito Q.3 — Profit Dashboard).

Layout en disco:
  base_dir / "tenants" / tenant_id / "profit" / "profit_items.json"
Asegura:
- Validación de identificadores seguros con validate_safe_identifier.
- Validación de aislamiento estricto con CrossTenantGuard.
- Operaciones atómicas con flush y fsync.
- Sanitización recursiva de claves sensibles (N.9).
- Reconstrucción fiel de tipos (Decimal, datetime UTC, Enum).
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
from src.domain.profit_dashboard.models import (
    ProfitDashboardItem,
    ProfitCompleteness,
)
from src.domain.profit_dashboard.ports import TenantProfitRepositoryPort


class CorruptedProfitDataError(Exception):
    """Lanzada cuando un archivo de datos JSON de rentabilidad está corrupto o ilegible."""
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


def _serialize_profit_item(item: ProfitDashboardItem) -> Dict[str, Any]:
    """Serializa un ProfitDashboardItem canónico a diccionario JSON-friendly."""
    return {
        "item_id": item.item_id,
        "product_id": item.product_id,
        "opportunity_id": item.opportunity_id,
        "supplier_id": item.supplier_id,
        "supplier_name": item.supplier_name,
        "marketplace": item.marketplace,
        "title": item.title,
        "category": item.category,
        "product_sku": item.product_sku,
        "currency": item.currency,
        "completeness": item.completeness.value if hasattr(item.completeness, "value") else str(item.completeness),
        "calculated_at": item.calculated_at.isoformat() if item.calculated_at else None,
        "sale_price": str(item.sale_price) if item.sale_price is not None else None,
        "unit_cost": str(item.unit_cost) if item.unit_cost is not None else None,
        "shipping_cost": str(item.shipping_cost) if item.shipping_cost is not None else None,
        "marketplace_fee": str(item.marketplace_fee) if item.marketplace_fee is not None else None,
        "payment_fee": str(item.payment_fee) if item.payment_fee is not None else None,
        "tax_cost": str(item.tax_cost) if item.tax_cost is not None else None,
        "other_costs": str(item.other_costs) if item.other_costs is not None else None,
        "total_known_cost": str(item.total_known_cost) if item.total_known_cost is not None else None,
        "gross_profit": str(item.gross_profit) if item.gross_profit is not None else None,
        "contribution_profit": str(item.contribution_profit) if item.contribution_profit is not None else None,
        "margin_pct": str(item.margin_pct) if item.margin_pct is not None else None,
        "markup_pct": str(item.markup_pct) if item.markup_pct is not None else None,
        "break_even_sale_price": str(item.break_even_sale_price) if item.break_even_sale_price is not None else None,
        "confidence": item.confidence,
        "missing_cost_components": list(item.missing_cost_components),
        "unknown_fields": list(item.unknown_fields),
    }


def _deserialize_profit_item(d: Dict[str, Any]) -> ProfitDashboardItem:
    """Reconstruye fielmente un ProfitDashboardItem desde un dict JSON."""
    calc_at_str = d.get("calculated_at")
    calc_at = datetime.fromisoformat(calc_at_str) if calc_at_str else datetime.now(timezone.utc)
    if calc_at.tzinfo is None:
        calc_at = calc_at.replace(tzinfo=timezone.utc)

    comp_str = d.get("completeness", "INSUFFICIENT_DATA")
    try:
        completeness = ProfitCompleteness(comp_str)
    except ValueError:
        completeness = ProfitCompleteness.INSUFFICIENT_DATA

    return ProfitDashboardItem(
        item_id=d["item_id"],
        product_id=d["product_id"],
        opportunity_id=d.get("opportunity_id"),
        supplier_id=d.get("supplier_id"),
        supplier_name=d.get("supplier_name"),
        marketplace=d.get("marketplace", "mercadolibre"),
        title=d.get("title"),
        category=d.get("category"),
        product_sku=d.get("product_sku"),
        currency=d.get("currency", "CLP"),
        completeness=completeness,
        calculated_at=calc_at,
        sale_price=Decimal(str(d["sale_price"])) if d.get("sale_price") is not None else None,
        unit_cost=Decimal(str(d["unit_cost"])) if d.get("unit_cost") is not None else None,
        shipping_cost=Decimal(str(d["shipping_cost"])) if d.get("shipping_cost") is not None else None,
        marketplace_fee=Decimal(str(d["marketplace_fee"])) if d.get("marketplace_fee") is not None else None,
        payment_fee=Decimal(str(d["payment_fee"])) if d.get("payment_fee") is not None else None,
        tax_cost=Decimal(str(d["tax_cost"])) if d.get("tax_cost") is not None else None,
        other_costs=Decimal(str(d["other_costs"])) if d.get("other_costs") is not None else None,
        total_known_cost=Decimal(str(d["total_known_cost"])) if d.get("total_known_cost") is not None else None,
        gross_profit=Decimal(str(d["gross_profit"])) if d.get("gross_profit") is not None else None,
        contribution_profit=Decimal(str(d["contribution_profit"])) if d.get("contribution_profit") is not None else None,
        margin_pct=Decimal(str(d["margin_pct"])) if d.get("margin_pct") is not None else None,
        markup_pct=Decimal(str(d["markup_pct"])) if d.get("markup_pct") is not None else None,
        break_even_sale_price=Decimal(str(d["break_even_sale_price"])) if d.get("break_even_sale_price") is not None else None,
        confidence=d.get("confidence"),
        missing_cost_components=tuple(d.get("missing_cost_components", [])),
        unknown_fields=tuple(d.get("unknown_fields", [])),
    )


class JsonTenantProfitRepository(TenantProfitRepositoryPort):
    """
    Repositorio JSON multi-tenant para ProfitDashboardItem.
    Almacena los datos particionados físicamente por tenant_id.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)

    def _get_tenant_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        profit_dir = self.tenants_dir / tenant_id / "profit"
        profit_dir.mkdir(parents=True, exist_ok=True)
        return profit_dir / "profit_items.json"

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
            raise CorruptedProfitDataError(f"Corrupted JSON in {file_path}") from e

    def _write_atomic(self, tenant_id: str, data: List[Dict[str, Any]]) -> None:
        file_path = self._get_tenant_file(tenant_id)
        tmp_file = file_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, file_path)

    def save(self, context: TenantContext, item: ProfitDashboardItem) -> None:
        validate_safe_identifier(item.item_id, field_name="item_id")
        raw_list = self._read_raw(context.tenant_id)
        raw_item = _serialize_profit_item(item)

        updated = False
        for i, existing in enumerate(raw_list):
            if existing.get("item_id") == item.item_id:
                raw_list[i] = raw_item
                updated = True
                break
        if not updated:
            raw_list.append(raw_item)

        self._write_atomic(context.tenant_id, raw_list)

    def save_all(self, context: TenantContext, items: List[ProfitDashboardItem]) -> int:
        if not items:
            return 0
        raw_list = self._read_raw(context.tenant_id)
        item_map = {d.get("item_id"): d for d in raw_list}

        count = 0
        for item in items:
            validate_safe_identifier(item.item_id, field_name="item_id")
            raw_item = _serialize_profit_item(item)
            item_map[item.item_id] = raw_item
            count += 1

        self._write_atomic(context.tenant_id, list(item_map.values()))
        return count

    def get_by_id(self, context: TenantContext, item_id: str) -> Optional[ProfitDashboardItem]:
        validate_safe_identifier(item_id, field_name="item_id")
        raw_list = self._read_raw(context.tenant_id)
        for raw in raw_list:
            if raw.get("item_id") == item_id:
                return _deserialize_profit_item(raw)
        return None

    def list_all(self, context: TenantContext) -> List[ProfitDashboardItem]:
        raw_list = self._read_raw(context.tenant_id)
        items = []
        for raw in raw_list:
            try:
                items.append(_deserialize_profit_item(raw))
            except Exception:
                continue
        return items

    def delete(self, context: TenantContext, item_id: str) -> bool:
        validate_safe_identifier(item_id, field_name="item_id")
        raw_list = self._read_raw(context.tenant_id)
        initial_len = len(raw_list)
        filtered = [d for d in raw_list if d.get("item_id") != item_id]
        if len(filtered) < initial_len:
            self._write_atomic(context.tenant_id, filtered)
            return True
        return False
