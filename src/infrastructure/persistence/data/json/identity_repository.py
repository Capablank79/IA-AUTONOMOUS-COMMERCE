"""
Implementación JSON persistente, atómica y determinista para Identity (Hito N.1).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Idempotencia estricta para identidades idénticas (mismo checksum).
- Detección explícita de conflictos ante diferente checksum para misma identity_id (IdentityConflictError).
- Detección de colisiones si un canonical_identifier ya está asignado a otro identity_id (IdentityCanonicalConflictError).
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción (CorruptedIdentityRecordError).
- Thread-safe mediante lock de concurrencia.
- Recuperación y reconstrucción resiliente del índice index/identities_index.jsonl ante caídas o reinicios.
- Path safety estricto (rechaza traversals, .. , /, \\, :).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence
from contextlib import contextmanager
import threading

from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    compute_identity_checksum,
    build_canonical_identifier,
)
from src.domain.identity.ports import IdentityRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonIdentityRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de Identity."""
    pass


class IdentityConflictError(JsonIdentityRepositoryError):
    """Se lanza cuando se intenta registrar una identidad con el mismo ID pero contenido incompatible."""
    pass


class IdentityCanonicalConflictError(JsonIdentityRepositoryError):
    """Se lanza cuando un canonical_identifier ya está asociado a otro identity_id."""
    pass


class CorruptedIdentityRecordError(JsonIdentityRepositoryError):
    """Se lanza cuando un archivo persistido está corrupto o tiene un checksum inválido."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonIdentityRepository(IdentityRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para Identity (N.1).
    Organización en disco:
      base_dir/
        identities/
          {identity_id}.json
        index/
          identities_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.identities_dir = self.base_dir / "identities"
        self.index_dir = self.base_dir / "index"

        self.identities_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.identities_index_file = self.index_dir / "identities_index.jsonl"
        self._thread_lock = threading.RLock()
        with self._exclusive_lock():
            self._recover_index_if_needed()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            yield

    def _recover_index_if_needed(self) -> None:
        valid_index = self.identities_index_file.exists()
        if valid_index:
            try:
                with open(self.identities_index_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
            except (OSError, json.JSONDecodeError):
                valid_index = False
        if valid_index:
            return

        entries = []
        for identity_file in sorted(self.identities_dir.glob("*.json")):
            try:
                identity = self._load_identity_file(identity_file)
                entries.append({
                    "identity_id": identity.identity_id,
                    "canonical_identifier": identity.canonical_identifier,
                    "display_name": identity.display_name,
                    "identity_type": identity.identity_type.value,
                    "provider": identity.provider,
                    "external_subject_id": identity.external_subject_id,
                    "status": identity.status.value,
                    "checksum": identity.checksum,
                    "created_at": identity.created_at.isoformat(),
                    "updated_at": identity.updated_at.isoformat(),
                })
            except Exception as e:
                logger.warning(f"Skipping unrecoverable identity file {identity_file}: {e}")

        tmp_path = self.identities_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.identities_index_file)

    def _rebuild_index_unlocked(self) -> None:
        """Reconstruye el índice identities_index.jsonl completamente a partir de los archivos en identities_dir."""
        entries = []
        for identity_file in sorted(self.identities_dir.glob("*.json")):
            try:
                identity = self._load_identity_file(identity_file)
                entries.append({
                    "identity_id": identity.identity_id,
                    "canonical_identifier": identity.canonical_identifier,
                    "display_name": identity.display_name,
                    "identity_type": identity.identity_type.value,
                    "provider": identity.provider,
                    "external_subject_id": identity.external_subject_id,
                    "status": identity.status.value,
                    "checksum": identity.checksum,
                    "created_at": identity.created_at.isoformat(),
                    "updated_at": identity.updated_at.isoformat(),
                })
            except Exception as e:
                logger.warning(f"Skipping unrecoverable identity file {identity_file}: {e}")

        tmp_path = self.identities_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.identities_index_file)

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Escribe un archivo JSON de manera atómica (.tmp -> fsync -> os.replace)."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index(self, index_file: Path, entry: Dict[str, Any]) -> None:
        """Agrega una entrada append-only a un archivo index JSONL con fsync."""
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def _load_identity_file(self, file_path: Path) -> Identity:
        """Carga y valida la integridad estricta por checksum de un archivo de identidad JSON."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            identity = Identity.from_dict(data)

            # Recomputar y validar checksum
            recomputed_checksum = compute_identity_checksum(
                identity_id=identity.identity_id,
                identity_type=identity.identity_type,
                canonical_identifier=identity.canonical_identifier,
                display_name=identity.display_name,
                provider=identity.provider,
                external_subject_id=identity.external_subject_id,
                status=identity.status,
                schema_version=identity.schema_version,
                metadata=identity.metadata,
            )

            if identity.checksum != recomputed_checksum:
                raise CorruptedIdentityRecordError(
                    f"Checksum mismatch in {file_path.name}: file has '{identity.checksum}', recomputed '{recomputed_checksum}'"
                )

            return identity
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise CorruptedIdentityRecordError(f"Corrupted identity record in {file_path}: {e}") from e

    def save_identity(self, identity: Identity) -> Identity:
        """Persiste una identidad de forma atómica e idempotente."""
        validate_safe_identifier(identity.identity_id, field_name="identity_id")

        with self._exclusive_lock():
            # 1. Verificar si ya existe el mismo canonical_identifier bajo otro identity_id
            existing_by_canonical = self._find_by_canonical_identifier_unlocked(identity.canonical_identifier)
            if existing_by_canonical and existing_by_canonical.identity_id != identity.identity_id:
                raise IdentityCanonicalConflictError(
                    f"Canonical identifier '{identity.canonical_identifier}' is already registered under identity_id '{existing_by_canonical.identity_id}'."
                )

            identity_file = self.identities_dir / f"{identity.identity_id}.json"

            if identity_file.exists():
                try:
                    existing_identity = self._load_identity_file(identity_file)
                    if existing_identity.checksum == identity.checksum:
                        return existing_identity
                    else:
                        raise IdentityConflictError(
                            f"Identity '{identity.identity_id}' already exists with different checksum ({existing_identity.checksum} vs {identity.checksum})."
                        )
                except IdentityConflictError:
                    raise
                except Exception as e:
                    raise CorruptedIdentityRecordError(
                        f"Cannot verify existing identity '{identity.identity_id}': {e}"
                    ) from e

            # Escribir archivo de identidad atómicamente
            self._atomic_write_json(identity_file, identity.to_dict())

            # Actualizar índice
            self._append_to_index(
                self.identities_index_file,
                {
                    "identity_id": identity.identity_id,
                    "canonical_identifier": identity.canonical_identifier,
                    "display_name": identity.display_name,
                    "identity_type": identity.identity_type.value,
                    "provider": identity.provider,
                    "external_subject_id": identity.external_subject_id,
                    "status": identity.status.value,
                    "checksum": identity.checksum,
                    "created_at": identity.created_at.isoformat(),
                    "updated_at": identity.updated_at.isoformat(),
                },
            )
            return identity

    def get_identity(self, identity_id: str) -> Optional[Identity]:
        """Obtiene una identidad por su ID exacto."""
        validate_safe_identifier(identity_id, field_name="identity_id")

        with self._exclusive_lock():
            identity_file = self.identities_dir / f"{identity_id}.json"
            if not identity_file.exists():
                return None
            return self._load_identity_file(identity_file)

    def _find_by_canonical_identifier_unlocked(self, canonical_identifier: str) -> Optional[Identity]:
        norm_ci = canonical_identifier.strip().lower()
        if not self.identities_index_file.exists():
            return None

        matched_identity_id = None

        with open(self.identities_index_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("canonical_identifier", "").lower() == norm_ci:
                    matched_identity_id = entry.get("identity_id")

        if matched_identity_id:
            identity_file = self.identities_dir / f"{matched_identity_id}.json"
            if identity_file.exists():
                return self._load_identity_file(identity_file)

        return None

    def find_by_canonical_identifier(self, canonical_identifier: str) -> Optional[Identity]:
        """Busca una identidad por su canonical_identifier exacto."""
        with self._exclusive_lock():
            return self._find_by_canonical_identifier_unlocked(canonical_identifier)

    def find_by_external_subject(self, provider: str, external_subject_id: str) -> Optional[Identity]:
        """Busca una identidad por proveedor y sujeto externo."""
        norm_provider = provider.strip().lower()
        norm_ext_id = external_subject_id.strip()

        with self._exclusive_lock():
            if not self.identities_index_file.exists():
                return None

            matched_id = None
            with open(self.identities_index_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if (
                        (entry.get("provider") or "").lower() == norm_provider
                        and str(entry.get("external_subject_id", "")).strip() == norm_ext_id
                    ):
                        matched_id = entry.get("identity_id")

            if matched_id:
                identity_file = self.identities_dir / f"{matched_id}.json"
                if identity_file.exists():
                    return self._load_identity_file(identity_file)

            return None

    def list_identities(
        self,
        identity_type: Optional[IdentityType] = None,
        provider: Optional[str] = None,
        status: Optional[IdentityStatus] = None,
        limit: int = 100,
    ) -> Sequence[Identity]:
        """Lista identidades aplicando filtros opcionales."""
        with self._exclusive_lock():
            identities_map: Dict[str, Identity] = {}
            for id_file in sorted(self.identities_dir.glob("*.json")):
                ident = self.get_identity(id_file.stem)
                if ident:
                    identities_map[ident.identity_id] = ident

            results = []
            for ident in identities_map.values():
                if identity_type and ident.identity_type != identity_type:
                    continue
                if provider and (ident.provider or "").lower() != provider.lower():
                    continue
                if status and ident.status != status:
                    continue
                results.append(ident)
                if len(results) >= limit:
                    break

            return tuple(results)

    def exists(self, identity_id: str) -> bool:
        """Verifica si existe una identidad."""
        validate_safe_identifier(identity_id, field_name="identity_id")
        with self._exclusive_lock():
            return (self.identities_dir / f"{identity_id}.json").exists()
