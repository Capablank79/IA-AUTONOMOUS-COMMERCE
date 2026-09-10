"""
Adaptadores de Repositorio para Políticas y Reservas de Quotas SaaS (Hito O.7 — Quota Management).

Define:
- InMemoryQuotaPolicyRepository
- JsonQuotaPolicyRepository
- InMemoryQuotaReservationRepository
- JsonQuotaReservationRepository
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
    QuotaReservation,
    QuotaReservationStatus,
    QuotaPolicyIntegrityError,
    QuotaReservationConflictError,
)
from src.domain.quota_management.ports import (
    QuotaPolicyRepositoryPort,
    QuotaReservationRepositoryPort,
)


class InMemoryQuotaPolicyRepository(QuotaPolicyRepositoryPort):
    """Repositorio en memoria thread-safe de políticas de cuota particionado por tenant."""

    def __init__(self):
        self._lock = threading.RLock()
        self._policies_by_tenant: Dict[str, QuotaPolicy] = {}

    def get_policy(self, tenant_id: str) -> Optional[QuotaPolicy]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            return self._policies_by_tenant.get(tenant_id)

    def save_policy(self, policy: QuotaPolicy) -> None:
        validate_safe_identifier(policy.tenant_id, "tenant_id")
        if not policy.verify_integrity():
            raise QuotaPolicyIntegrityError(f"QuotaPolicy checksum validation failed for {policy.policy_id}")
        with self._lock:
            self._policies_by_tenant[policy.tenant_id] = policy

    def delete_policy(self, tenant_id: str) -> bool:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            if tenant_id in self._policies_by_tenant:
                del self._policies_by_tenant[tenant_id]
                return True
            return False


class JsonQuotaPolicyRepository(QuotaPolicyRepositoryPort):
    """
    Repositorio JSON en disco para políticas de cuota:
    `base_dir / "tenants" / {tenant_id} / "quota" / "policy.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_tenant_policy_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / "tenants" / tenant_id / "quota"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "policy.json"

    def _rule_to_dict(self, rule: QuotaRule) -> Dict[str, Any]:
        return {
            "rule_id": rule.rule_id,
            "quota_type": rule.quota_type.value,
            "limit_value": str(rule.limit_value) if isinstance(rule.limit_value, Decimal) else rule.limit_value,
            "scope": rule.scope.value,
            "window_type": rule.window_type.value,
            "target_identifier": rule.target_identifier,
            "is_hard_limit": rule.is_hard_limit,
            "custom_window_seconds": rule.custom_window_seconds,
            "allow_cache_hit_bypass_token_budget": rule.allow_cache_hit_bypass_token_budget,
        }

    def _dict_to_rule(self, d: Dict[str, Any]) -> QuotaRule:
        q_type = QuotaType(d["quota_type"])
        lim = Decimal(str(d["limit_value"])) if q_type == QuotaType.MAX_COST else int(d["limit_value"])
        return QuotaRule(
            rule_id=d["rule_id"],
            quota_type=q_type,
            limit_value=lim,
            scope=QuotaScope(d.get("scope", QuotaScope.TENANT.value)),
            window_type=QuotaWindowType(d.get("window_type", QuotaWindowType.DAY.value)),
            target_identifier=d.get("target_identifier"),
            is_hard_limit=d.get("is_hard_limit", True),
            custom_window_seconds=d.get("custom_window_seconds"),
            allow_cache_hit_bypass_token_budget=d.get("allow_cache_hit_bypass_token_budget", True),
        )

    def get_policy(self, tenant_id: str) -> Optional[QuotaPolicy]:
        validate_safe_identifier(tenant_id, "tenant_id")
        file_path = self._get_tenant_policy_file(tenant_id)
        with self._lock:
            if not file_path.exists():
                return None
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                rules = [self._dict_to_rule(r) for r in data.get("rules", [])]
                policy = QuotaPolicy(
                    policy_id=data["policy_id"],
                    tenant_id=data["tenant_id"],
                    rules=tuple(rules),
                    policy_version=data.get("policy_version", "1.0.0"),
                    is_unlimited=data.get("is_unlimited", False),
                    description=data.get("description"),
                    checksum=data.get("checksum", ""),
                )
                if not policy.verify_integrity():
                    raise QuotaPolicyIntegrityError(f"Corrupt policy checksum on disk for tenant {tenant_id}")
                return policy
            except (json.JSONDecodeError, KeyError, ValueError) as ex:
                raise QuotaPolicyIntegrityError(f"Failed to load policy for tenant {tenant_id}: {ex}")

    def save_policy(self, policy: QuotaPolicy) -> None:
        validate_safe_identifier(policy.tenant_id, "tenant_id")
        if not policy.verify_integrity():
            raise QuotaPolicyIntegrityError(f"QuotaPolicy checksum validation failed for {policy.policy_id}")

        file_path = self._get_tenant_policy_file(policy.tenant_id)
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
            temp_file = file_path.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            temp_file.replace(file_path)

    def delete_policy(self, tenant_id: str) -> bool:
        validate_safe_identifier(tenant_id, "tenant_id")
        file_path = self._get_tenant_policy_file(tenant_id)
        with self._lock:
            if file_path.exists():
                file_path.unlink()
                return True
            return False


class InMemoryQuotaReservationRepository(QuotaReservationRepositoryPort):
    """Repositorio en memoria thread-safe de reservas de cuota."""

    def __init__(self):
        self._lock = threading.RLock()
        # Dict[tenant_id, Dict[reservation_id, QuotaReservation]]
        self._reservations_by_tenant: Dict[str, Dict[str, QuotaReservation]] = {}
        # Dict[tenant_id, Dict[correlation_id, reservation_id]]
        self._correlation_map: Dict[str, Dict[str, str]] = {}

    def save_reservation(self, reservation: QuotaReservation) -> None:
        validate_safe_identifier(reservation.tenant_id, "tenant_id")
        with self._lock:
            tenant_id = reservation.tenant_id
            if tenant_id not in self._reservations_by_tenant:
                self._reservations_by_tenant[tenant_id] = {}
                self._correlation_map[tenant_id] = {}

            tenant_res = self._reservations_by_tenant[tenant_id]
            tenant_corr = self._correlation_map[tenant_id]

            if reservation.correlation_id:
                existing_res_id = tenant_corr.get(reservation.correlation_id)
                if existing_res_id and existing_res_id != reservation.reservation_id:
                    existing = tenant_res.get(existing_res_id)
                    if existing and existing.status == QuotaReservationStatus.RESERVED:
                        raise QuotaReservationConflictError(
                            f"Active reservation already exists for correlation_id '{reservation.correlation_id}'"
                        )

            tenant_res[reservation.reservation_id] = reservation
            if reservation.correlation_id:
                tenant_corr[reservation.correlation_id] = reservation.reservation_id

    def get_reservation(self, reservation_id: str) -> Optional[QuotaReservation]:
        validate_safe_identifier(reservation_id, "reservation_id")
        with self._lock:
            for tenant_res in self._reservations_by_tenant.values():
                if reservation_id in tenant_res:
                    return tenant_res[reservation_id]
            return None

    def get_by_correlation_id(self, tenant_id: str, correlation_id: str) -> Optional[QuotaReservation]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            corr_map = self._correlation_map.get(tenant_id, {})
            res_id = corr_map.get(correlation_id)
            if res_id:
                return self._reservations_by_tenant.get(tenant_id, {}).get(res_id)
            return None

    def list_active_reservations(
        self,
        tenant_id: str,
        current_time: datetime,
        identity_id: Optional[str] = None,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> List[QuotaReservation]:
        validate_safe_identifier(tenant_id, "tenant_id")
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        with self._lock:
            tenant_res = self._reservations_by_tenant.get(tenant_id, {})
            active: List[QuotaReservation] = []
            for res in tenant_res.values():
                if res.status == QuotaReservationStatus.RESERVED and not res.is_expired(current_time):
                    if identity_id and res.identity_id != identity_id:
                        continue
                    if model_id and res.model_id != model_id:
                        continue
                    if provider and res.provider != provider:
                        continue
                    active.append(res)
            return active


class JsonQuotaReservationRepository(QuotaReservationRepositoryPort):
    """
    Repositorio JSON en disco para reservas de cuota:
    `base_dir / "tenants" / {tenant_id} / "quota" / "reservations" / {reservation_id}.json`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_reservation_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        r_dir = self.base_dir / "tenants" / tenant_id / "quota" / "reservations"
        r_dir.mkdir(parents=True, exist_ok=True)
        return r_dir

    def _reservation_to_dict(self, r: QuotaReservation) -> Dict[str, Any]:
        return {
            "reservation_id": r.reservation_id,
            "tenant_id": r.tenant_id,
            "identity_id": r.identity_id,
            "model_id": r.model_id,
            "provider": r.provider,
            "estimated_requests": r.estimated_requests,
            "estimated_input_tokens": r.estimated_input_tokens,
            "estimated_output_tokens": r.estimated_output_tokens,
            "estimated_total_tokens": r.estimated_total_tokens,
            "estimated_cost": str(r.estimated_cost),
            "created_at": r.created_at.isoformat(),
            "expires_at": r.expires_at.isoformat(),
            "status": r.status.value,
            "correlation_id": r.correlation_id,
            "source_decision_id": r.source_decision_id,
            "reconciled_at": r.reconciled_at.isoformat() if r.reconciled_at else None,
            "checksum": r.checksum,
        }

    def _dict_to_reservation(self, d: Dict[str, Any]) -> QuotaReservation:
        return QuotaReservation(
            reservation_id=d["reservation_id"],
            tenant_id=d["tenant_id"],
            identity_id=d.get("identity_id"),
            model_id=d.get("model_id"),
            provider=d.get("provider"),
            estimated_requests=int(d.get("estimated_requests", 1)),
            estimated_input_tokens=int(d.get("estimated_input_tokens", 0)),
            estimated_output_tokens=int(d.get("estimated_output_tokens", 0)),
            estimated_total_tokens=int(d.get("estimated_total_tokens", 0)),
            estimated_cost=Decimal(str(d.get("estimated_cost", "0.00"))),
            created_at=datetime.fromisoformat(d["created_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
            status=QuotaReservationStatus(d["status"]),
            correlation_id=d.get("correlation_id"),
            source_decision_id=d.get("source_decision_id"),
            reconciled_at=datetime.fromisoformat(d["reconciled_at"]) if d.get("reconciled_at") else None,
            checksum=d.get("checksum", ""),
        )

    def save_reservation(self, reservation: QuotaReservation) -> None:
        validate_safe_identifier(reservation.tenant_id, "tenant_id")
        validate_safe_identifier(reservation.reservation_id, "reservation_id")

        r_dir = self._get_reservation_dir(reservation.tenant_id)
        file_path = r_dir / f"{reservation.reservation_id}.json"

        with self._lock:
            # Idempotencia por correlation_id si aplica
            if reservation.correlation_id:
                for existing_file in r_dir.glob("*.json"):
                    if existing_file.name == f"{reservation.reservation_id}.json":
                        continue
                    try:
                        with open(existing_file, "r", encoding="utf-8") as f:
                            d = json.load(f)
                        if d.get("correlation_id") == reservation.correlation_id and d.get("status") == QuotaReservationStatus.RESERVED.value:
                            # Verificar si expiró
                            exp_dt = datetime.fromisoformat(d["expires_at"])
                            if exp_dt > datetime.now(timezone.utc):
                                raise QuotaReservationConflictError(
                                    f"Active reservation {d['reservation_id']} exists for correlation_id '{reservation.correlation_id}'"
                                )
                    except (json.JSONDecodeError, KeyError):
                        continue

            payload = self._reservation_to_dict(reservation)
            temp_file = file_path.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            temp_file.replace(file_path)

    def get_reservation(self, reservation_id: str) -> Optional[QuotaReservation]:
        validate_safe_identifier(reservation_id, "reservation_id")
        with self._lock:
            tenants_root = self.base_dir / "tenants"
            if not tenants_root.exists():
                return None
            for t_dir in tenants_root.iterdir():
                if t_dir.is_dir():
                    res_file = t_dir / "quota" / "reservations" / f"{reservation_id}.json"
                    if res_file.exists():
                        try:
                            with open(res_file, "r", encoding="utf-8") as f:
                                return self._dict_to_reservation(json.load(f))
                        except Exception:
                            return None
            return None

    def get_by_correlation_id(self, tenant_id: str, correlation_id: str) -> Optional[QuotaReservation]:
        validate_safe_identifier(tenant_id, "tenant_id")
        r_dir = self._get_reservation_dir(tenant_id)
        with self._lock:
            for file_path in r_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("correlation_id") == correlation_id:
                        return self._dict_to_reservation(d)
                except Exception:
                    continue
            return None

    def list_active_reservations(
        self,
        tenant_id: str,
        current_time: datetime,
        identity_id: Optional[str] = None,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> List[QuotaReservation]:
        validate_safe_identifier(tenant_id, "tenant_id")
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        r_dir = self._get_reservation_dir(tenant_id)
        active: List[QuotaReservation] = []
        with self._lock:
            for file_path in r_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        res = self._dict_to_reservation(json.load(f))
                    if res.status == QuotaReservationStatus.RESERVED and not res.is_expired(current_time):
                        if identity_id and res.identity_id != identity_id:
                            continue
                        if model_id and res.model_id != model_id:
                            continue
                        if provider and res.provider != provider:
                            continue
                        active.append(res)
                except Exception:
                    continue
        return active
