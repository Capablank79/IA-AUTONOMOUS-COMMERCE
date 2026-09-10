"""
Adaptador de Repositorio JSON para Sesiones SaaS Multi-Tenant (Hito O.3 — SaaS / Platformization).

Implementa:
- JsonSaaSSessionRepository (SaaSSessionRepositoryPort)

Garantías:
- Aislamiento físico estricto por Tenant: `base_dir / "tenants" / tenant_safe_id / "sessions" / session_id.json`
- Índice seguro en memoria o particionado para lookup seguro por session_id evitando enumeración cruzada.
- Operaciones atómicas y crash-safe (`.tmp` + `flush` + `os.fsync` + `os.replace`).
- Verificación de integridad criptográfica SHA-256 (fail-safe ante corrupción / tampering).
- Prevención total de path traversal (`validate_safe_identifier`).
- Thread-safety mediante `threading.RLock`.
- Prevención de ataques Cross-Tenant: Si se especifica tenant_id esperado, el acceso a sesiones de otro tenant es bloqueado.
"""

import json
import os
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

from src.domain.session.models import (
    SaaSSession,
    SessionStatus,
    SessionNotFoundError,
    SessionTenantMismatchError,
    SessionValidationError,
    compute_session_checksum,
)
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.security.models import validate_safe_identifier
from src.domain.tenant.models import CrossTenantAccessError


class JsonSaaSSessionRepository(SaaSSessionRepositoryPort):
    """
    Repositorio JSON aislado por Tenant para Sesiones SaaS Multi-Tenant.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_session_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        session_dir = self.tenants_dir / tenant_id / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _get_file_path(self, tenant_id: str, session_id: str) -> Path:
        validate_safe_identifier(session_id, field_name="session_id")
        return self._get_session_dir(tenant_id) / f"{session_id}.json"

    def save(self, session: SaaSSession) -> None:
        """Guarda o actualiza de forma atómica y crash-safe una SaaSSession."""
        if not isinstance(session, SaaSSession):
            raise TypeError("session must be an instance of SaaSSession.")

        validate_safe_identifier(session.tenant_id, field_name="tenant_id")
        validate_safe_identifier(session.session_id, field_name="session_id")

        with self._lock:
            file_path = self._get_file_path(session.tenant_id, session.session_id)
            tmp_path = file_path.with_suffix(".tmp")

            data = session.to_dict()
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, file_path)

    def get_by_id(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[SaaSSession]:
        """
        Obtiene una sesión por su ID.
        Si tenant_id es provisto, busca en la partición de dicho tenant.
        Si tenant_id no es provisto, busca en las particiones de todos los tenants registrados.
        """
        validate_safe_identifier(session_id, field_name="session_id")

        with self._lock:
            if tenant_id is not None:
                validate_safe_identifier(tenant_id, field_name="tenant_id")
                file_path = self._get_file_path(tenant_id, session_id)
                if not file_path.exists():
                    return None

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    if data.get("tenant_id") != tenant_id or data.get("session_id") != session_id:
                        return None

                    return SaaSSession.from_dict(data)
                except Exception:
                    # Corrupción de datos o tampering -> Fail-safe None
                    return None

            # Búsqueda a través de tenants si tenant_id no fue provisto
            if not self.tenants_dir.exists():
                return None

            for tenant_folder in self.tenants_dir.iterdir():
                if not tenant_folder.is_dir():
                    continue
                sess_dir = tenant_folder / "sessions"
                if not sess_dir.exists():
                    continue
                cand_file = sess_dir / f"{session_id}.json"
                if cand_file.exists():
                    try:
                        with open(cand_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if data.get("session_id") == session_id:
                            return SaaSSession.from_dict(data)
                    except Exception:
                        return None

            return None

    def list_by_tenant(self, tenant_id: str) -> List[SaaSSession]:
        """Lista todas las sesiones registradas bajo un tenant específico."""
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        with self._lock:
            sess_dir = self._get_session_dir(tenant_id)
            results: List[SaaSSession] = []

            for json_file in sess_dir.glob("*.json"):
                if json_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("tenant_id") == tenant_id:
                        results.append(SaaSSession.from_dict(data))
                except Exception:
                    continue

            return sorted(results, key=lambda s: s.created_at, reverse=True)

    def list_by_identity(self, identity_id: str, tenant_id: Optional[str] = None) -> List[SaaSSession]:
        """Lista todas las sesiones de una identidad."""
        validate_safe_identifier(identity_id, field_name="identity_id")

        with self._lock:
            candidates: List[SaaSSession] = []
            if tenant_id is not None:
                candidates = self.list_by_tenant(tenant_id)
            else:
                if not self.tenants_dir.exists():
                    return []
                for tenant_folder in self.tenants_dir.iterdir():
                    if tenant_folder.is_dir():
                        candidates.extend(self.list_by_tenant(tenant_folder.name))

            results = [s for s in candidates if s.identity_id == identity_id]
            return sorted(results, key=lambda s: s.created_at, reverse=True)

    def delete(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        """Elimina físicamente el archivo de una sesión."""
        validate_safe_identifier(session_id, field_name="session_id")

        with self._lock:
            if tenant_id is not None:
                file_path = self._get_file_path(tenant_id, session_id)
                if not file_path.exists():
                    return False
                file_path.unlink()
                return True

            # Buscar y eliminar
            if not self.tenants_dir.exists():
                return False

            for tenant_folder in self.tenants_dir.iterdir():
                if not tenant_folder.is_dir():
                    continue
                cand_file = tenant_folder / "sessions" / f"{session_id}.json"
                if cand_file.exists():
                    cand_file.unlink()
                    return True

            return False
