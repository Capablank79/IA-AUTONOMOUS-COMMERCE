"""
Adaptador JSON de Repositorio de Misiones Aislado por Tenant (Hito Q.4 — Mission Dashboard).

Layout en disco:
  base_dir / "tenants" / tenant_id / "missions" / "missions.json"
  base_dir / "tenants" / tenant_id / "missions" / "results" / "{mission_id}.json" (o partición granular de resultados)
Asegura:
- Validación de identificadores seguros con validate_safe_identifier.
- Validación de aislamiento estricto con CrossTenantGuard.
- Operaciones atómicas con flush y fsync.
- Sanitización recursiva de claves sensibles (N.9) y exclusión de CoT privado.
- Reconstrucción fiel de tipos (Mission, MissionResult, MissionTraceEntry, Enum, datetime UTC).
- UNKNOWN != 0 y preservación de incertidumbre.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType
from dataclasses import is_dataclass, asdict

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionPriority,
    MissionStatus,
    MissionResult,
    MissionTraceEntry,
)
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort


class CorruptedTenantMissionDataError(Exception):
    """Lanzada cuando un archivo de datos JSON de misiones está corrupto o ilegible."""
    pass


SENSITIVE_KEYS = {
    "password", "secret", "token", "api_key", "apikey", "pan", "cvv",
    "private_key", "credential", "access_token", "refresh_token", "authorization",
    "chain_of_thought", "reasoning", "reasoning_tokens", "internal_scratchpad",
}


def _encode_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if is_dataclass(val) and not isinstance(val, type):
        return _encode_json_value(asdict(val))
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple, set)):
        return [_encode_json_value(v) for v in val]
    return val


def _serialize_mission(mission: Mission) -> Dict[str, Any]:
    return {
        "mission_id": mission.mission_id,
        "type": mission.type.value if hasattr(mission.type, "value") else str(mission.type),
        "priority": mission.priority.value if hasattr(mission.priority, "value") else str(mission.priority),
        "status": mission.status.value if hasattr(mission.status, "value") else str(mission.status),
        "parameters": _encode_json_value(mission.parameters),
        "created_at": mission.created_at.isoformat() if mission.created_at else None,
        "updated_at": mission.updated_at.isoformat() if mission.updated_at else None,
    }


def _deserialize_mission(d: Dict[str, Any]) -> Mission:
    created_at = datetime.fromisoformat(d["created_at"]) if d.get("created_at") else datetime.now(timezone.utc)
    updated_at = datetime.fromisoformat(d["updated_at"]) if d.get("updated_at") else datetime.now(timezone.utc)
    return Mission(
        mission_id=d["mission_id"],
        type=MissionType(d["type"]),
        priority=MissionPriority(d.get("priority", "MEDIUM")),
        status=MissionStatus(d.get("status", "PENDING")),
        parameters=d.get("parameters", {}),
        created_at=created_at,
        updated_at=updated_at,
    )


class JsonTenantMissionRepository(TenantMissionRepositoryPort):
    """
    Repositorio JSON multi-tenant para Mission y MissionResult.
    Almacena los datos particionados físicamente por tenant_id.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)

    def _get_tenant_dirs(self, tenant_id: str) -> Tuple[Path, Path]:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        mission_dir = self.tenants_dir / tenant_id / "missions"
        results_dir = mission_dir / "results"
        mission_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        return mission_dir, results_dir

    def _get_missions_file(self, tenant_id: str) -> Path:
        mission_dir, _ = self._get_tenant_dirs(tenant_id)
        return mission_dir / "missions.json"

    def _read_raw_missions(self, tenant_id: str) -> List[Dict[str, Any]]:
        file_path = self._get_missions_file(tenant_id)
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip():
                    return []
                return json.loads(content)
        except json.JSONDecodeError as e:
            raise CorruptedTenantMissionDataError(f"Corrupted JSON in {file_path}") from e

    def _write_atomic(self, file_path: Path, data: Any) -> None:
        tmp_file = file_path.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, file_path)

    def save(self, context: TenantContext, mission: Mission) -> None:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        records = self._read_raw_missions(tenant_id)
        serialized = _serialize_mission(mission)

        for i, r in enumerate(records):
            if r.get("mission_id") == mission.mission_id:
                records[i] = serialized
                self._write_atomic(self._get_missions_file(tenant_id), records)
                return

        records.append(serialized)
        self._write_atomic(self._get_missions_file(tenant_id), records)

    def save_all(self, context: TenantContext, missions: List[Mission]) -> int:
        CrossTenantGuard.ensure_tenant_context(context)
        if not missions:
            return 0
        tenant_id = context.tenant_id
        records = self._read_raw_missions(tenant_id)
        rec_map = {r.get("mission_id"): i for i, r in enumerate(records)}

        count = 0
        for m in missions:
            serialized = _serialize_mission(m)
            mid = m.mission_id
            if mid in rec_map:
                records[rec_map[mid]] = serialized
            else:
                records.append(serialized)
                rec_map[mid] = len(records) - 1
            count += 1

        self._write_atomic(self._get_missions_file(tenant_id), records)
        return count

    def get_by_id(self, context: TenantContext, mission_id: str) -> Optional[Mission]:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(mission_id, field_name="mission_id")
        tenant_id = context.tenant_id
        records = self._read_raw_missions(tenant_id)
        for r in records:
            if r.get("mission_id") == mission_id:
                return _deserialize_mission(r)
        return None

    def list_all(self, context: TenantContext) -> List[Mission]:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        records = self._read_raw_missions(tenant_id)
        return [_deserialize_mission(r) for r in records]

    def save_result(self, context: TenantContext, result: MissionResult) -> None:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        _, results_dir = self._get_tenant_dirs(tenant_id)
        validate_safe_identifier(result.mission_id, field_name="mission_id")
        file_path = results_dir / f"{result.mission_id}.json"

        trace_data: List[Dict[str, Any]] = [
            {
                "step": entry.step,
                "status": entry.status.value if hasattr(entry.status, "value") else str(entry.status),
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                "metadata": _encode_json_value(entry.metadata),
            }
            for entry in result.trace
        ]

        data = {
            "mission_id": result.mission_id,
            "status": result.status.value if hasattr(result.status, "value") else str(result.status),
            "output": _encode_json_value(result.output),
            "trace": trace_data,
            "evidences": _encode_json_value(result.evidences),
            "blocks": _encode_json_value(result.blocks),
            "errors": result.errors,
            "finished_at": result.finished_at.isoformat() if result.finished_at else None,
        }

        self._write_atomic(file_path, data)

    def get_result(self, context: TenantContext, mission_id: str) -> Optional[MissionResult]:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        _, results_dir = self._get_tenant_dirs(tenant_id)
        validate_safe_identifier(mission_id, field_name="mission_id")
        file_path = results_dir / f"{mission_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            trace = [
                MissionTraceEntry(
                    step=t["step"],
                    status=MissionStatus(t["status"]),
                    timestamp=datetime.fromisoformat(t["timestamp"]) if t.get("timestamp") else datetime.now(timezone.utc),
                    metadata=t.get("metadata", {}),
                )
                for t in data.get("trace", [])
            ]

            return MissionResult(
                mission_id=data["mission_id"],
                status=MissionStatus(data["status"]),
                output=data.get("output", {}),
                trace=trace,
                evidences=data.get("evidences", []),
                blocks=data.get("blocks", []),
                errors=data.get("errors", []),
                finished_at=datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else datetime.now(timezone.utc),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise CorruptedTenantMissionDataError(f"Corrupted mission result data for {mission_id}: {e}") from e

    def delete(self, context: TenantContext, mission_id: str) -> bool:
        CrossTenantGuard.ensure_tenant_context(context)
        tenant_id = context.tenant_id
        validate_safe_identifier(mission_id, field_name="mission_id")
        records = self._read_raw_missions(tenant_id)
        found = False
        new_records = []
        for r in records:
            if r.get("mission_id") == mission_id:
                found = True
            else:
                new_records.append(r)

        if found:
            self._write_atomic(self._get_missions_file(tenant_id), new_records)
            _, results_dir = self._get_tenant_dirs(tenant_id)
            res_path = results_dir / f"{mission_id}.json"
            if res_path.exists():
                try:
                    os.remove(res_path)
                except OSError:
                    pass
            return True
        return False
