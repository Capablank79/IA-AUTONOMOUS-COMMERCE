"""
JSON Persistent Repository for Financial Limit Policies (Hito N.7 — Financial Limits).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Almacenamiento seguro de políticas de límites financieros con validación de integridad.
- Idempotencia estricta para registros idénticos.
- Detección explícita de corrupción física o manipulación.
- Thread-safe mediante RLock.
- Path safety estricto (prevención de path traversal: .., /, \\, :).
"""

from decimal import Decimal
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence
from contextlib import contextmanager
import threading

from src.domain.financial_limit.models import (
    FinancialLimitPolicy,
    FinancialLimitRule,
    FinancialLimitType,
)
from src.domain.financial_limit.ports import FinancialLimitPolicyRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonFinancialLimitPolicyRepositoryError(Exception):
    """Excepción base para errores en el repositorio de políticas de límites financieros."""
    pass


class FinancialLimitPolicyCorruptionError(JsonFinancialLimitPolicyRepositoryError):
    """Se lanza cuando un archivo de política está corrupto o malformado."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista sanitizando claves sensibles."""
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if str(k) in ("policy_name", "version", "currency", "rules", "rule_id",
                          "limit_type", "max_amount", "min_amount", "allow_approval_override",
                          "target_action", "target_resource_type", "account_id",
                          "default_strict_rejection", "description"):
                cleaned[str(k)] = _encode_json_value(v)
                continue
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonFinancialLimitPolicyRepository(FinancialLimitPolicyRepositoryPort):
    """
    Repositorio persistente y atómico en formato JSON para FinancialLimitPolicy.

    Organización en disco:
      base_dir/
        financial_limit_policies/
          {policy_name}.json
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.policies_dir = self.base_dir / "financial_limit_policies"
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

    def save_policy(self, policy: FinancialLimitPolicy) -> None:
        if not isinstance(policy, FinancialLimitPolicy):
            raise TypeError(f"Expected FinancialLimitPolicy, got {type(policy)}")

        with self._exclusive_lock():
            target_path = self._get_policy_file_path(policy.policy_name)
            temp_path = target_path.with_suffix(f".tmp.{os.getpid()}_{id(policy)}")

            raw_rules = []
            for r in policy.rules:
                raw_rules.append({
                    "rule_id": r.rule_id,
                    "limit_type": r.limit_type.value,
                    "currency": r.currency,
                    "max_amount": str(r.max_amount) if r.max_amount is not None else None,
                    "min_amount": str(r.min_amount) if r.min_amount is not None else None,
                    "allow_approval_override": r.allow_approval_override,
                    "target_action": r.target_action,
                    "target_resource_type": r.target_resource_type,
                    "account_id": r.account_id,
                    "metadata": dict(r.metadata),
                })

            data = {
                "policy_name": policy.policy_name,
                "version": policy.version,
                "currency": policy.currency,
                "rules": raw_rules,
                "default_strict_rejection": policy.default_strict_rejection,
                "description": policy.description,
                "checksum": policy.checksum,
                "metadata": dict(policy.metadata),
            }

            encoded = _encode_json_value(data)
            json_text = json.dumps(encoded, indent=2, sort_keys=True)

            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(json_text)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(temp_path, target_path)
            except Exception as e:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                raise JsonFinancialLimitPolicyRepositoryError(f"Failed to persist financial limit policy: {e}") from e

    def get_policy(self, policy_name: str) -> Optional[FinancialLimitPolicy]:
        target_path = self._get_policy_file_path(policy_name)
        if not target_path.exists():
            return None

        with self._exclusive_lock():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    raise FinancialLimitPolicyCorruptionError("Root JSON is not an object")

                rules_list = []
                for rd in data.get("rules", []):
                    max_amt = Decimal(rd["max_amount"]) if rd.get("max_amount") is not None else None
                    min_amt = Decimal(rd["min_amount"]) if rd.get("min_amount") is not None else None
                    rule = FinancialLimitRule(
                        rule_id=rd["rule_id"],
                        limit_type=FinancialLimitType(rd["limit_type"]),
                        currency=rd["currency"],
                        max_amount=max_amt,
                        min_amount=min_amt,
                        allow_approval_override=bool(rd.get("allow_approval_override", False)),
                        target_action=rd.get("target_action", ""),
                        target_resource_type=rd.get("target_resource_type", ""),
                        account_id=rd.get("account_id", ""),
                        metadata=rd.get("metadata", {}),
                    )
                    rules_list.append(rule)

                return FinancialLimitPolicy(
                    policy_name=data["policy_name"],
                    version=data.get("version", "1.0.0"),
                    currency=data.get("currency", "USD"),
                    rules=tuple(rules_list),
                    default_strict_rejection=bool(data.get("default_strict_rejection", True)),
                    description=data.get("description", ""),
                    checksum=data.get("checksum", ""),
                    metadata=data.get("metadata", {}),
                )
            except Exception as e:
                logger.error(f"Corrupted financial policy file {target_path}: {e}")
                raise FinancialLimitPolicyCorruptionError(f"Failed to load policy {policy_name}: {e}") from e

    def list_policies(self) -> Sequence[FinancialLimitPolicy]:
        with self._exclusive_lock():
            results = []
            for p in self.policies_dir.glob("*.json"):
                policy_name = p.stem
                policy = self.get_policy(policy_name)
                if policy:
                    results.append(policy)
            return tuple(results)
