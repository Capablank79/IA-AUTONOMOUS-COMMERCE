"""
JSON Persistent Repository for Tool Access Policies (Hito N.8 — Tool Allowlist / Denylist).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Almacenamiento seguro de políticas de acceso a herramientas con validación de integridad SHA-256.
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

from src.domain.tool_policy.models import (
    ToolPolicy,
    ToolPolicyRule,
    ToolRuleAction,
)
from src.domain.tool.models import ToolSideEffectLevel
from src.domain.tool_policy.ports import ToolPolicyRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonToolPolicyRepositoryError(Exception):
    """Excepción base para errores en el repositorio de políticas de herramientas."""
    pass


class ToolPolicyCorruptionError(JsonToolPolicyRepositoryError):
    """Se lanza cuando un archivo de política está corrupto o malformado."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista sanitizando claves sensibles."""
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if str(k) in (
                "policy_name", "version", "rules", "rule_id", "action",
                "tool_id_pattern", "provider_pattern", "operation_pattern",
                "allowed_side_effect_levels", "prohibited_side_effect_levels",
                "allowed_roles", "denied_roles", "allowed_scopes",
                "target_account_id", "target_mission_id", "default_action",
                "description", "checksum"
            ):
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


class JsonToolPolicyRepository(ToolPolicyRepositoryPort):
    """
    Repositorio persistente y atómico en formato JSON para ToolPolicy (N.8).

    Organización en disco:
      base_dir/
        tool_policies/
          {policy_name}.json
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.policies_dir = self.base_dir / "tool_policies"
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

    def save_policy(self, policy: ToolPolicy) -> None:
        if not isinstance(policy, ToolPolicy):
            raise TypeError(f"Expected ToolPolicy, got {type(policy)}")

        with self._exclusive_lock():
            target_path = self._get_policy_file_path(policy.policy_name)
            temp_path = target_path.with_suffix(f".tmp.{os.getpid()}_{id(policy)}")

            raw_rules = []
            for r in policy.rules:
                raw_rules.append({
                    "rule_id": r.rule_id,
                    "action": r.action.value,
                    "tool_id_pattern": r.tool_id_pattern,
                    "provider_pattern": r.provider_pattern,
                    "operation_pattern": r.operation_pattern,
                    "allowed_side_effect_levels": [lvl.value for lvl in r.allowed_side_effect_levels],
                    "prohibited_side_effect_levels": [lvl.value for lvl in r.prohibited_side_effect_levels],
                    "allowed_roles": list(r.allowed_roles),
                    "denied_roles": list(r.denied_roles),
                    "allowed_scopes": list(r.allowed_scopes),
                    "target_account_id": r.target_account_id,
                    "target_mission_id": r.target_mission_id,
                    "description": r.description,
                    "metadata": dict(r.metadata),
                })

            data = {
                "policy_name": policy.policy_name,
                "version": policy.version,
                "default_action": policy.default_action.value,
                "rules": raw_rules,
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
                raise JsonToolPolicyRepositoryError(f"Failed to persist tool access policy: {e}") from e

    def get_policy(self, policy_name: str) -> Optional[ToolPolicy]:
        target_path = self._get_policy_file_path(policy_name)
        if not target_path.exists():
            return None

        with self._exclusive_lock():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    raise ToolPolicyCorruptionError("Root JSON is not an object")

                rules_list = []
                for rd in data.get("rules", []):
                    allowed_eff = [ToolSideEffectLevel(lvl) for lvl in rd.get("allowed_side_effect_levels", [])]
                    prohib_eff = [ToolSideEffectLevel(lvl) for lvl in rd.get("prohibited_side_effect_levels", [])]

                    rule = ToolPolicyRule(
                        rule_id=rd["rule_id"],
                        action=ToolRuleAction(rd["action"]),
                        tool_id_pattern=rd.get("tool_id_pattern", "*"),
                        provider_pattern=rd.get("provider_pattern", "*"),
                        operation_pattern=rd.get("operation_pattern", "*"),
                        allowed_side_effect_levels=tuple(allowed_eff),
                        prohibited_side_effect_levels=tuple(prohib_eff),
                        allowed_roles=tuple(rd.get("allowed_roles", [])),
                        denied_roles=tuple(rd.get("denied_roles", [])),
                        allowed_scopes=tuple(rd.get("allowed_scopes", [])),
                        target_account_id=rd.get("target_account_id"),
                        target_mission_id=rd.get("target_mission_id"),
                        description=rd.get("description", ""),
                        metadata=rd.get("metadata", {}),
                    )
                    rules_list.append(rule)

                return ToolPolicy(
                    policy_name=data["policy_name"],
                    version=data.get("version", "1.0.0"),
                    rules=tuple(rules_list),
                    default_action=ToolRuleAction(data.get("default_action", "DENY")),
                    checksum=data.get("checksum", ""),
                    metadata=data.get("metadata", {}),
                )
            except Exception as e:
                logger.error(f"Corrupted tool policy file {target_path}: {e}")
                raise ToolPolicyCorruptionError(f"Failed to load policy {policy_name}: {e}") from e

    def list_policies(self) -> Sequence[ToolPolicy]:
        with self._exclusive_lock():
            results = []
            for p in self.policies_dir.glob("*.json"):
                policy_name = p.stem
                try:
                    pol = self.get_policy(policy_name)
                    if pol is not None:
                        results.append(pol)
                except Exception as e:
                    logger.warning(f"Skipping corrupted policy file {p}: {e}")
            return tuple(results)
