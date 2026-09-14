"""
Adaptador JSON de Repositorio de Oportunidades Aislado por Tenant (Hito Q.1 — Opportunity Dashboard).

Layout en disco:
  base_dir / "tenants" / tenant_id / "opportunities" / "opportunities.json" (o partición granular por ID).
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

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
from src.infrastructure.persistence.data.json.opportunity_repository import (
    _encode_json_value,
    _decode_opportunity_record,
    _serialize_opportunity_record,
    CorruptedOpportunityDataError,
)


class JsonTenantOpportunityRepository(TenantOpportunityRepositoryPort):
    """
    Repositorio JSON multi-tenant para OpportunityRecord.
    Almacena los datos particionados físicamente por tenant_id.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)

    def _get_tenant_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        opp_dir = self.tenants_dir / tenant_id / "opportunities"
        opp_dir.mkdir(parents=True, exist_ok=True)
        return opp_dir / "opportunities.json"

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
            raise CorruptedOpportunityDataError(f"Corrupted JSON in {file_path}") from e

    def _write_atomic(self, tenant_id: str, data: List[Dict[str, Any]]) -> None:
        file_path = self._get_tenant_file(tenant_id)
        tmp_file = file_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, file_path)

    def save(self, context: TenantContext, opportunity: OpportunityRecord) -> None:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        serialized = _serialize_opportunity_record(opportunity)

        for i, r in enumerate(records):
            if (
                r.get("opportunity_id") == opportunity.opportunity_id
                or (opportunity.idempotency_key and r.get("idempotency_key") == opportunity.idempotency_key)
            ):
                records[i] = serialized
                self._write_atomic(tenant_id, records)
                return

        records.append(serialized)
        self._write_atomic(tenant_id, records)

    def save_all(self, context: TenantContext, opportunities: List[OpportunityRecord]) -> int:
        CrossTenantGuard.ensure_tenant_context(context)
        if not opportunities:
            return 0
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        existing_ids = {r.get("opportunity_id") for r in records}
        existing_idemp = {r.get("idempotency_key") for r in records if r.get("idempotency_key")}

        added_count = 0
        for opp in opportunities:
            if opp.opportunity_id in existing_ids or (opp.idempotency_key and opp.idempotency_key in existing_idemp):
                continue
            records.append(_serialize_opportunity_record(opp))
            existing_ids.add(opp.opportunity_id)
            if opp.idempotency_key:
                existing_idemp.add(opp.idempotency_key)
            added_count += 1

        if added_count > 0:
            self._write_atomic(tenant_id, records)
        return added_count

    def get_by_id(self, context: TenantContext, opportunity_id: str) -> Optional[OpportunityRecord]:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(opportunity_id, field_name="opportunity_id")
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        for r in records:
            if r.get("opportunity_id") == opportunity_id:
                return _decode_opportunity_record(r)
        return None

    def list_all(self, context: TenantContext) -> List[OpportunityRecord]:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        decoded = [_decode_opportunity_record(r) for r in records]
        # Orden cronológico descendente por defecto
        decoded.sort(key=lambda o: (o.detected_at, o.opportunity_id), reverse=True)
        return decoded

    def delete(self, context: TenantContext, opportunity_id: str) -> bool:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(opportunity_id, field_name="opportunity_id")
        tenant_id = context.tenant_id
        records = self._read_raw(tenant_id)
        original_len = len(records)
        filtered = [r for r in records if r.get("opportunity_id") != opportunity_id]
        if len(filtered) < original_len:
            self._write_atomic(tenant_id, filtered)
            return True
        return False
