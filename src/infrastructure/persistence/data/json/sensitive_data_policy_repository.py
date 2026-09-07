"""
JSON Persistent Repository for Sensitive Data Handling Policies (Hito N.9).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Almacenamiento seguro de políticas de manejo de datos con validación de integridad SHA-256.
- Idempotencia estricta para registros idénticos.
- Detección explícita de corrupción física o manipulación.
- Thread-safe mediante RLock.
- Path safety estricto (prevención de path traversal: .., /, \\, :).
"""

import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence
from contextlib import contextmanager
import threading

from src.domain.security.sensitive_data_models import (
    DataHandlingPolicy,
    DataClassification,
    SensitiveCategory,
    DataHandlingPurpose,
    PersistenceHandlingMode,
    CacheHandlingMode,
)
from src.domain.security.sensitive_data_ports import DataHandlingPolicyRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonDataHandlingPolicyRepositoryError(Exception):
    """Excepción base para errores en el repositorio de políticas de datos sensibles."""
    pass


class DataHandlingPolicyCorruptionError(JsonDataHandlingPolicyRepositoryError):
    """Se lanza cuando un archivo de política está corrupto o malformado."""
    pass


def _encode_policy_json(val: Any) -> Any:
    """Serializa valores de forma determinista para la política."""
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        return {str(k): _encode_policy_json(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_encode_policy_json(v) for v in val]
    return val


class JsonDataHandlingPolicyRepository(DataHandlingPolicyRepositoryPort):
    """
    Repositorio persistente y atómico en formato JSON para DataHandlingPolicy (N.9).

    Organización en disco:
      base_dir/
        sensitive_data_policies/
          {policy_name}.json
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.policies_dir = self.base_dir / "sensitive_data_policies"
        self.policies_dir.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            yield

    def _get_policy_file_path(self, policy_name: str) -> Path:
        validate_safe_identifier(policy_name, "policy_name")
        target_path = (self.policies_dir / f"{policy_name}.json").resolve()
        policies_dir_resolved = self.policies_dir.resolve()
        if not str(target_path).startswith(str(policies_dir_resolved)):
            raise ValueError("Path traversal attempt detected in policy_name.")
        return target_path

    def save_policy(self, policy: DataHandlingPolicy) -> None:
        """
        Persiste una política de forma atómica y segura contra caídas (crash-safe).
        """
        with self._exclusive_lock():
            file_path = self._get_policy_file_path(policy.policy_name)
            tmp_path = file_path.with_suffix(".tmp")

            raw_dict = {
                "policy_name": policy.policy_name,
                "version": policy.version,
                "description": policy.description,
                "default_classification": policy.default_classification.value,
                "allowed_purposes": [p.value for p in policy.allowed_purposes],
                "logging_allowed_classes": [c.value for c in policy.logging_allowed_classes],
                "cache_allowed_classes": [c.value for c in policy.cache_allowed_classes],
                "external_transfer_allowed_classes": [c.value for c in policy.external_transfer_allowed_classes],
                "persistence_mode": policy.persistence_mode.value,
                "cache_mode": policy.cache_mode.value,
                "prohibited_categories_for_logging": [c.value for c in policy.prohibited_categories_for_logging],
                "prohibited_categories_for_cache": [c.value for c in policy.prohibited_categories_for_cache],
                "allowed_fields_override_by_purpose": {
                    k: list(v) for k, v in policy.allowed_fields_override_by_purpose.items()
                },
                "checksum": policy.checksum,
            }

            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(raw_dict, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(tmp_path, file_path)
            except Exception as e:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                raise JsonDataHandlingPolicyRepositoryError(f"Failed to persist policy '{policy.policy_name}': {e}") from e

    def get_policy(self, policy_name: str) -> Optional[DataHandlingPolicy]:
        """
        Obtiene y valida la integridad de una política persistida.
        """
        try:
            file_path = self._get_policy_file_path(policy_name)
        except ValueError:
            return None

        if not file_path.exists():
            return None

        with self._exclusive_lock():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                policy = DataHandlingPolicy(
                    policy_name=data["policy_name"],
                    version=data.get("version", "1.0.0"),
                    description=data.get("description", ""),
                    default_classification=DataClassification(data.get("default_classification", "INTERNAL")),
                    allowed_purposes=tuple(
                        DataHandlingPurpose(p) for p in data.get("allowed_purposes", [])
                    ),
                    logging_allowed_classes=tuple(
                        DataClassification(c) for c in data.get("logging_allowed_classes", [])
                    ),
                    cache_allowed_classes=tuple(
                        DataClassification(c) for c in data.get("cache_allowed_classes", [])
                    ),
                    external_transfer_allowed_classes=tuple(
                        DataClassification(c) for c in data.get("external_transfer_allowed_classes", [])
                    ),
                    persistence_mode=PersistenceHandlingMode(data.get("persistence_mode", "ALLOW")),
                    cache_mode=CacheHandlingMode(data.get("cache_mode", "SANITIZED_ONLY")),
                    prohibited_categories_for_logging=tuple(
                        SensitiveCategory(c) for c in data.get("prohibited_categories_for_logging", [])
                    ),
                    prohibited_categories_for_cache=tuple(
                        SensitiveCategory(c) for c in data.get("prohibited_categories_for_cache", [])
                    ),
                    allowed_fields_override_by_purpose=data.get("allowed_fields_override_by_purpose", {}),
                    checksum=data.get("checksum", ""),
                )

                if not policy.verify_integrity():
                    raise DataHandlingPolicyCorruptionError(f"Integrity checksum mismatch for policy '{policy_name}'")

                return policy
            except Exception as e:
                logger.error(f"Error loading policy '{policy_name}': {e}")
                raise DataHandlingPolicyCorruptionError(f"Failed to read or corrupted policy file '{policy_name}': {e}") from e

    def list_policies(self) -> Sequence[DataHandlingPolicy]:
        """Lista todas las políticas almacenadas válidas."""
        policies = []
        with self._exclusive_lock():
            for p_file in self.policies_dir.glob("*.json"):
                policy_name = p_file.stem
                try:
                    pol = self.get_policy(policy_name)
                    if pol is not None:
                        policies.append(pol)
                except Exception as e:
                    logger.warning(f"Skipping corrupted policy file '{p_file}': {e}")
        return tuple(policies)
