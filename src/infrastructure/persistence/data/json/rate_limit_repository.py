"""
Adaptadores de Repositorio y Store de Estados para Rate Limit Management (Hito P.11).

Define:
- InMemoryRateLimitPolicyRepository: Almacenamiento en memoria particionado por tenant de políticas.
- JsonRateLimitPolicyRepository: Almacenamiento en archivos JSON por tenant de políticas.
- InMemoryRateLimitStateStore: Token bucket store atómico en memoria con RLock contra condiciones de carrera (TOCTOU).
"""

from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union, Tuple

from src.domain.security.models import validate_safe_identifier
from src.domain.rate_limit.models import (
    RateLimitPolicy,
    RateLimitRule,
    RateLimitScope,
    RateLimitWindowUnit,
    RateLimitState,
    RateLimitPolicyIntegrityError,
    RateLimitStoreError,
)
from src.domain.rate_limit.ports import (
    RateLimitPolicyRepositoryPort,
    RateLimitStateStorePort,
)


class InMemoryRateLimitPolicyRepository(RateLimitPolicyRepositoryPort):
    """Repositorio en memoria thread-safe de políticas de rate limit."""

    def __init__(self):
        self._lock = threading.RLock()
        self._policies_by_tenant: Dict[str, RateLimitPolicy] = {}
        self._global_policy: Optional[RateLimitPolicy] = None

    def get_policy(self, tenant_id: Optional[str] = None) -> Optional[RateLimitPolicy]:
        with self._lock:
            if tenant_id:
                validate_safe_identifier(tenant_id, "tenant_id")
                return self._policies_by_tenant.get(tenant_id)
            return self._global_policy

    def save_policy(self, policy: RateLimitPolicy) -> None:
        if not policy.verify_integrity():
            raise RateLimitPolicyIntegrityError(f"RateLimitPolicy checksum mismatch for {policy.policy_id}")
        with self._lock:
            if policy.tenant_id:
                validate_safe_identifier(policy.tenant_id, "tenant_id")
                self._policies_by_tenant[policy.tenant_id] = policy
            else:
                self._global_policy = policy

    def delete_policy(self, tenant_id: Optional[str] = None) -> bool:
        with self._lock:
            if tenant_id:
                validate_safe_identifier(tenant_id, "tenant_id")
                if tenant_id in self._policies_by_tenant:
                    del self._policies_by_tenant[tenant_id]
                    return True
                return False
            else:
                if self._global_policy is not None:
                    self._global_policy = None
                    return True
                return False


class JsonRateLimitPolicyRepository(RateLimitPolicyRepositoryPort):
    """
    Repositorio JSON en disco para políticas de rate limit:
    - Global: `base_dir / "platform" / "rate_limit" / "policy.json"`
    - Tenant: `base_dir / "tenants" / {tenant_id} / "rate_limit" / "policy.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_policy_file(self, tenant_id: Optional[str]) -> Path:
        if tenant_id:
            validate_safe_identifier(tenant_id, "tenant_id")
            p_dir = self.base_dir / "tenants" / tenant_id / "rate_limit"
        else:
            p_dir = self.base_dir / "platform" / "rate_limit"
        p_dir.mkdir(parents=True, exist_ok=True)
        return p_dir / "policy.json"

    def _rule_to_dict(self, rule: RateLimitRule) -> Dict[str, Any]:
        return {
            "rule_id": rule.rule_id,
            "limit_rate": rule.limit_rate,
            "window_unit": rule.window_unit.value,
            "burst_capacity": rule.burst_capacity,
            "scope": rule.scope.value,
            "target_identifier": rule.target_identifier,
            "is_hard_limit": rule.is_hard_limit,
            "custom_window_seconds": rule.custom_window_seconds,
            "description": rule.description,
        }

    def _dict_to_rule(self, d: Dict[str, Any]) -> RateLimitRule:
        return RateLimitRule(
            rule_id=d["rule_id"],
            limit_rate=d["limit_rate"],
            window_unit=RateLimitWindowUnit(d.get("window_unit", "MINUTE")),
            burst_capacity=d.get("burst_capacity"),
            scope=RateLimitScope(d.get("scope", "TENANT")),
            target_identifier=d.get("target_identifier"),
            is_hard_limit=d.get("is_hard_limit", True),
            custom_window_seconds=d.get("custom_window_seconds"),
            description=d.get("description"),
        )

    def get_policy(self, tenant_id: Optional[str] = None) -> Optional[RateLimitPolicy]:
        filepath = self._get_policy_file(tenant_id)
        with self._lock:
            if not filepath.exists():
                return None
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                rules = tuple(self._dict_to_rule(r) for r in data.get("rules", []))
                policy = RateLimitPolicy(
                    policy_id=data["policy_id"],
                    tenant_id=data.get("tenant_id"),
                    rules=rules,
                    policy_version=data.get("policy_version", "1.0.0"),
                    is_unlimited=data.get("is_unlimited", False),
                    description=data.get("description"),
                    checksum=data.get("checksum", ""),
                )
                if not policy.verify_integrity():
                    raise RateLimitPolicyIntegrityError(f"RateLimitPolicy checksum validation failed for {filepath}")
                return policy
            except Exception as e:
                if isinstance(e, RateLimitPolicyIntegrityError):
                    raise
                raise RateLimitPolicyIntegrityError(f"Corrupt rate limit policy file at {filepath}: {e}")

    def save_policy(self, policy: RateLimitPolicy) -> None:
        if not policy.verify_integrity():
            raise RateLimitPolicyIntegrityError(f"RateLimitPolicy checksum mismatch for {policy.policy_id}")
        filepath = self._get_policy_file(policy.tenant_id)
        payload = {
            "policy_id": policy.policy_id,
            "tenant_id": policy.tenant_id,
            "policy_version": policy.policy_version,
            "is_unlimited": policy.is_unlimited,
            "description": policy.description,
            "checksum": policy.checksum,
            "rules": [self._rule_to_dict(r) for r in policy.rules],
        }
        with self._lock:
            temp_file = filepath.with_suffix(".tmp")
            temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_file.replace(filepath)

    def delete_policy(self, tenant_id: Optional[str] = None) -> bool:
        filepath = self._get_policy_file(tenant_id)
        with self._lock:
            if filepath.exists():
                filepath.unlink()
                return True
            return False


class InMemoryRateLimitStateStore(RateLimitStateStorePort):
    """
    Store en memoria thread-safe para Token Bucket con protección contra TOCTOU.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._states: Dict[str, RateLimitState] = {}

    def get_state(self, key: str) -> Optional[RateLimitState]:
        with self._lock:
            return self._states.get(key)

    def update_state(self, state: RateLimitState) -> None:
        with self._lock:
            self._states[state.key] = state

    def consume_token(
        self,
        key: str,
        cost_units: int,
        refill_rate_per_sec: float,
        burst_capacity: int,
        now: datetime,
    ) -> Tuple[bool, float, int]:
        """
        Operación atómica de rellenado y consumo de tokens.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        with self._lock:
            existing = self._states.get(key)
            if existing is None:
                # Inicialización: bucket lleno hasta burst_capacity
                current_tokens = float(burst_capacity)
                last_refill = now
            else:
                elapsed_seconds = max(0.0, (now - existing.last_refill_at).total_seconds())
                refilled = elapsed_seconds * refill_rate_per_sec
                current_tokens = min(float(burst_capacity), existing.tokens + refilled)
                last_refill = now

            if current_tokens >= float(cost_units):
                remaining_tokens = current_tokens - float(cost_units)
                new_state = RateLimitState(
                    key=key,
                    tokens=remaining_tokens,
                    last_refill_at=last_refill,
                    max_capacity=burst_capacity,
                )
                self._states[key] = new_state
                return True, remaining_tokens, 0
            else:
                # No alcanza: calcular retry_after_seconds determinista
                deficit = float(cost_units) - current_tokens
                if refill_rate_per_sec > 0:
                    retry_after = int(math.ceil(deficit / refill_rate_per_sec))
                else:
                    retry_after = 60
                retry_after = max(1, retry_after)

                new_state = RateLimitState(
                    key=key,
                    tokens=current_tokens,
                    last_refill_at=last_refill,
                    max_capacity=burst_capacity,
                )
                self._states[key] = new_state
                return False, current_tokens, retry_after

    def reset_key(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                del self._states[key]

    def clear_all(self) -> None:
        with self._lock:
            self._states.clear()
