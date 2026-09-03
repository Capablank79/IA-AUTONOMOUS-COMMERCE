"""
Servicio de Aplicación para Freshness / TTL (Hito L.3 - Transversal Data Quality / Governance).

Responsabilidades:
- Resolver políticas de frescura según precedencia determinista:
    1. Regla a nivel de campo (field_path específico)
    2. Regla a nivel de sujeto/tipo de dato (subject_type)
    3. Regla a nivel de fuente específica (source_id)
    4. Regla a nivel de tipo de fuente (source_type de L.1)
    5. Política global por defecto explícita
- Evaluar temporalmente hechos de negocio, observaciones de mercado, cotizaciones o linajes de datos (L.2 Provenance).
- Integración nativa con L.1 Source Registry (para resolución de source_type) y L.2 Data Provenance (para evaluación sobre provenance_id y linaje derivado).
- Reglas para datos derivados: la frescura derivada no puede superar la de sus padres (evaluación de ancestros / oldest relevant parent).
- Tratamiento estricto de timezones (UTC) y ClockPort / VirtualClock inyectable para determinismo absoluto en tests.
- Fronteras estrictas: responde exclusivamente "¿Este dato sigue siendo suficientemente reciente bajo su TTL?".
  NO calcula confianza (L.4), no valida esquemas (L.5), no resuelve entidades (L.6), no detecta duplicados (L.7), ni resuelve conflictos (L.8).
"""

from datetime import datetime, timezone, timedelta
import hashlib
import logging
from typing import Optional, Sequence, Mapping, Any, Dict, Union, List, Tuple
import uuid

from src.domain.freshness.models import (
    FreshnessStatus,
    FreshnessPolicy,
    FreshnessAssessment,
    compute_assessment_checksum,
)
from src.domain.freshness.ports import (
    FreshnessPolicyRepositoryPort,
    FreshnessAssessmentRepositoryPort,
)
from src.domain.source_registry.ports import SourceRegistryRepositoryPort
from src.domain.data_provenance.ports import ProvenanceRepositoryPort
from src.domain.data_provenance.models import ProvenanceRecord, SubjectType
from src.domain.reliability.ports import ClockPort
from src.domain.reliability.models import SystemHealthState
from src.infrastructure.reliability.reliability_infrastructure import SystemClock
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class FreshnessServiceError(Exception):
    """Excepción base para errores en FreshnessService."""
    pass


class PolicyNotFoundError(FreshnessServiceError):
    """Se lanza cuando no se puede resolver ninguna política aplicable y no hay default."""
    pass


class FreshnessService:
    """
    Servicio de aplicación para evaluación y gobierno de frescura / TTL (Hito L.3).
    """

    def __init__(
        self,
        policy_repository: FreshnessPolicyRepositoryPort,
        assessment_repository: Optional[FreshnessAssessmentRepositoryPort] = None,
        source_registry: Optional[SourceRegistryRepositoryPort] = None,
        provenance_repository: Optional[ProvenanceRepositoryPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        default_policy: Optional[FreshnessPolicy] = None,
    ):
        self.policy_repo = policy_repository
        self.assessment_repo = assessment_repository
        self.source_registry = source_registry
        self.provenance_repo = provenance_repository
        self.audit_repo = audit_repository
        self.clock = clock or SystemClock()
        self.default_policy = default_policy

    def register_policy(self, policy: FreshnessPolicy) -> FreshnessPolicy:
        """Registra una nueva política de frescura."""
        saved = self.policy_repo.save_policy(policy)
        if self.audit_repo:
            try:
                actor = AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="freshness_service")
                audit_record = AuditRecord(
                    record_id=f"audit-pol-{uuid.uuid4().hex[:12]}",
                    record_type=AuditRecordType.SYSTEM_EVENT,
                    action="FRESHNESS_POLICY_REGISTERED",
                    actor=actor,
                    timestamp=self.clock.now(),
                    target_id=policy.policy_id,
                    target_type="FRESHNESS_POLICY",
                    details={
                        "policy_id": policy.policy_id,
                        "name": policy.name,
                        "version": policy.version,
                        "ttl_seconds": policy.ttl_seconds,
                    },
                )
                self.audit_repo.save_record(audit_record)
            except Exception as e:
                logger.warning(f"Failed to record audit event for policy registration: {e}")
        return saved

    def resolve_policy(
        self,
        subject_type: Optional[Union[SubjectType, str]] = None,
        field_path: Optional[str] = None,
        source_id: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> FreshnessPolicy:
        """
        Resuelve la política aplicable con precedencia determinista:
        1. (field_path AND subject_type)
        2. (field_path)
        3. (source_id AND subject_type)
        4. (source_id)
        5. (source_type AND subject_type)
        6. (source_type)
        7. (subject_type)
        8. Política global/catch-all en repositorio (sin filtros)
        9. Default policy inyectado en el servicio
        """
        all_policies = self.policy_repo.list_policies()

        sub_val = subject_type.value if hasattr(subject_type, "value") else (str(subject_type) if subject_type else None)
        src_type_val = source_type.value if hasattr(source_type, "value") else (str(source_type) if source_type else None)

        # Si no se pasó source_type pero sí source_id y tenemos source_registry, buscar source_type
        if source_id and not src_type_val and self.source_registry:
            registered_src = self.source_registry.get_source(source_id)
            if registered_src:
                src_type_val = registered_src.source_type.value if hasattr(registered_src.source_type, "value") else str(registered_src.source_type)

        # 1. Match exacto por field_path + subject_type
        if field_path and sub_val:
            for p in all_policies:
                if p.field_path == field_path and p.subject_type == sub_val:
                    return p

        # 2. Match por field_path solo
        if field_path:
            for p in all_policies:
                if p.field_path == field_path and not p.subject_type and not p.source_id and not p.source_type:
                    return p

        # 3. Match por source_id + subject_type
        if source_id and sub_val:
            for p in all_policies:
                if p.source_id == source_id and p.subject_type == sub_val and not p.field_path:
                    return p

        # 4. Match por source_type + subject_type
        if src_type_val and sub_val:
            for p in all_policies:
                if p.source_type == src_type_val and p.subject_type == sub_val and not p.field_path:
                    return p

        # 5. Match por subject_type solo
        if sub_val:
            for p in all_policies:
                if p.subject_type == sub_val and not p.field_path and not p.source_id and not p.source_type:
                    return p

        # 6. Match por source_id solo
        if source_id:
            for p in all_policies:
                if p.source_id == source_id and not p.field_path and not p.subject_type:
                    return p

        # 7. Match por source_type solo
        if src_type_val:
            for p in all_policies:
                if p.source_type == src_type_val and not p.field_path and not p.subject_type and not p.source_id:
                    return p

        # 8. Política catch-all en repositorio
        for p in all_policies:
            if not p.field_path and not p.subject_type and not p.source_id and not p.source_type:
                return p

        # 9. Default policy inyectado
        if self.default_policy:
            return self.default_policy

        raise PolicyNotFoundError(
            f"No matching freshness policy found for subject_type={sub_val}, field_path={field_path}, "
            f"source_id={source_id}, source_type={src_type_val}"
        )

    def evaluate_timestamp(
        self,
        observed_at: Optional[datetime],
        subject_id: str,
        subject_type: Union[SubjectType, str] = SubjectType.GENERIC_FACT,
        field_path: Optional[str] = None,
        source_id: Optional[str] = None,
        source_type: Optional[str] = None,
        provenance_id: Optional[str] = None,
        custom_policy: Optional[FreshnessPolicy] = None,
        correlation_id: str = "default-correlation",
        metadata: Optional[Mapping[str, Any]] = None,
        persist: bool = False,
    ) -> FreshnessAssessment:
        """
        Evalúa la frescura de un timestamp respecto a la política aplicable y el reloj actual.
        """
        validate_safe_identifier(subject_id, field_name="subject_id")
        sub_val = subject_type.value if hasattr(subject_type, "value") else str(subject_type)

        now = self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Resolver política
        policy = custom_policy or self.resolve_policy(
            subject_type=sub_val,
            field_path=field_path,
            source_id=source_id,
            source_type=source_type,
        )

        # 1. Caso missing timestamp
        if observed_at is None:
            assessment = self._build_assessment(
                subject_id=subject_id,
                subject_type=sub_val,
                field_path=field_path,
                source_id=source_id,
                provenance_id=provenance_id,
                observed_at=None,
                evaluated_at=now,
                ttl_seconds=policy.ttl_seconds,
                age_seconds=None,
                status=FreshnessStatus.UNKNOWN,
                reason="Timestamp is missing or null. Freshness cannot be determined.",
                policy=policy,
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
            return self._maybe_persist_assessment(assessment, persist)

        # Normalizar naive timestamps a UTC con advertencia
        obs_utc = observed_at
        if obs_utc.tzinfo is None:
            obs_utc = obs_utc.replace(tzinfo=timezone.utc)

        # 2. Caso timestamp futuro
        delta_future = (obs_utc - now).total_seconds()
        if delta_future > policy.future_tolerance_seconds:
            assessment = self._build_assessment(
                subject_id=subject_id,
                subject_type=sub_val,
                field_path=field_path,
                source_id=source_id,
                provenance_id=provenance_id,
                observed_at=obs_utc,
                evaluated_at=now,
                ttl_seconds=policy.ttl_seconds,
                age_seconds=None,
                status=FreshnessStatus.ERROR,
                reason=f"Timestamp is in the future by {delta_future:.2f}s (tolerance: {policy.future_tolerance_seconds}s).",
                policy=policy,
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
            return self._maybe_persist_assessment(assessment, persist)

        # 3. Cálculo de age (si delta_future está en la tolerancia, age es 0)
        age_seconds = max(0.0, (now - obs_utc).total_seconds())

        # 4. Evaluación de boundaries
        # age < ttl -> FRESH
        # age >= ttl -> STALE (o EXPIRED si supera stale_threshold_seconds)
        if age_seconds < policy.ttl_seconds:
            status = FreshnessStatus.FRESH
            reason = f"Data is fresh. Age ({age_seconds:.2f}s) is within TTL ({policy.ttl_seconds:.2f}s)."
        elif policy.stale_threshold_seconds is not None and age_seconds >= policy.stale_threshold_seconds:
            status = FreshnessStatus.EXPIRED
            reason = f"Data is expired. Age ({age_seconds:.2f}s) exceeds expiration threshold ({policy.stale_threshold_seconds:.2f}s)."
        else:
            status = FreshnessStatus.STALE
            reason = f"Data is stale. Age ({age_seconds:.2f}s) exceeds TTL ({policy.ttl_seconds:.2f}s)."

        assessment = self._build_assessment(
            subject_id=subject_id,
            subject_type=sub_val,
            field_path=field_path,
            source_id=source_id,
            provenance_id=provenance_id,
            observed_at=obs_utc,
            evaluated_at=now,
            ttl_seconds=policy.ttl_seconds,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
            policy=policy,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        return self._maybe_persist_assessment(assessment, persist)

    def evaluate_provenance(
        self,
        provenance_id: str,
        custom_policy: Optional[FreshnessPolicy] = None,
        correlation_id: str = "default-correlation",
        persist: bool = False,
    ) -> FreshnessAssessment:
        """
        Evalúa la frescura de un registro de procedencia (L.2) incluyendo la verificación
        de datos derivados si tiene ancestros (oldest parent freshness rule).
        """
        if not self.provenance_repo:
            raise FreshnessServiceError("ProvenanceRepository is required to evaluate provenance records.")

        record = self.provenance_repo.get_provenance(provenance_id)
        if not record:
            raise FreshnessServiceError(f"Provenance record '{provenance_id}' not found.")

        # Si es un dato derivado con padres en el DAG:
        if record.parent_provenance_ids:
            return self.evaluate_derived_provenance(
                record=record,
                custom_policy=custom_policy,
                correlation_id=correlation_id,
                persist=persist,
            )

        # Dato atómico directo
        return self.evaluate_timestamp(
            observed_at=record.captured_at,
            subject_id=record.subject_id,
            subject_type=record.subject_type,
            field_path=record.field_path,
            source_id=record.source_id,
            provenance_id=record.provenance_id,
            custom_policy=custom_policy,
            correlation_id=correlation_id,
            metadata=dict(record.metadata),
            persist=persist,
        )

    def evaluate_derived_provenance(
        self,
        record: ProvenanceRecord,
        custom_policy: Optional[FreshnessPolicy] = None,
        correlation_id: str = "default-correlation",
        persist: bool = False,
    ) -> FreshnessAssessment:
        """
        Evalúa la frescura de un dato derivado.
        Regla: la frescura derivada NO puede superar a la de sus padres (oldest parent rule).
        Si algún padre es STALE, EXPIRED, UNKNOWN o ERROR, el derivado hereda el estado más degradado
        o su propia evaluación de timestamp, lo que sea más restrictivo.
        """
        # 1. Evaluar el registro derivado directamente
        direct_eval = self.evaluate_timestamp(
            observed_at=record.captured_at,
            subject_id=record.subject_id,
            subject_type=record.subject_type,
            field_path=record.field_path,
            source_id=record.source_id,
            provenance_id=record.provenance_id,
            custom_policy=custom_policy,
            correlation_id=correlation_id,
            metadata=dict(record.metadata),
            persist=False,
        )

        # 2. Evaluar recursivamente a los padres
        parent_assessments = []
        for p_id in record.parent_provenance_ids:
            p_eval = self.evaluate_provenance(
                provenance_id=p_id,
                correlation_id=correlation_id,
                persist=False,
            )
            parent_assessments.append(p_eval)

        # Jerarquía de severidad / restricción de frescura:
        # ERROR > UNKNOWN > EXPIRED > STALE > FRESH
        severity_order = {
            FreshnessStatus.ERROR: 5,
            FreshnessStatus.UNKNOWN: 4,
            FreshnessStatus.EXPIRED: 3,
            FreshnessStatus.STALE: 2,
            FreshnessStatus.FRESH: 1,
        }

        most_restrictive_status = direct_eval.status
        most_restrictive_reason = direct_eval.reason
        max_age = direct_eval.age_seconds

        for p_eval in parent_assessments:
            # Si el padre tiene mayor severidad (más degradado):
            if severity_order.get(p_eval.status, 0) > severity_order.get(most_restrictive_status, 0):
                most_restrictive_status = p_eval.status
                most_restrictive_reason = (
                    f"Derived data degraded by parent provenance '{p_eval.provenance_id}' ({p_eval.status.value}): {p_eval.reason}"
                )
            # Max age para seguimiento
            if p_eval.age_seconds is not None:
                if max_age is None or p_eval.age_seconds > max_age:
                    max_age = p_eval.age_seconds

        final_assessment = self._build_assessment(
            subject_id=record.subject_id,
            subject_type=record.subject_type.value if hasattr(record.subject_type, "value") else str(record.subject_type),
            field_path=record.field_path,
            source_id=record.source_id,
            provenance_id=record.provenance_id,
            observed_at=record.captured_at,
            evaluated_at=direct_eval.evaluated_at,
            ttl_seconds=direct_eval.ttl_seconds,
            age_seconds=max_age,
            status=most_restrictive_status,
            reason=most_restrictive_reason,
            policy=custom_policy or self.resolve_policy(subject_type=record.subject_type, field_path=record.field_path, source_id=record.source_id),
            correlation_id=correlation_id,
            metadata=dict(record.metadata),
        )

        return self._maybe_persist_assessment(final_assessment, persist)

    def _build_assessment(
        self,
        subject_id: str,
        subject_type: str,
        field_path: Optional[str],
        source_id: Optional[str],
        provenance_id: Optional[str],
        observed_at: Optional[datetime],
        evaluated_at: datetime,
        ttl_seconds: float,
        age_seconds: Optional[float],
        status: FreshnessStatus,
        reason: str,
        policy: FreshnessPolicy,
        correlation_id: str,
        metadata: Mapping[str, Any],
    ) -> FreshnessAssessment:
        # Generar deterministic assessment_id
        seed = (
            f"{subject_id}|{subject_type}|{field_path or ''}|{source_id or ''}|"
            f"{provenance_id or ''}|{evaluated_at.isoformat()}|{policy.policy_id}|{policy.version}"
        )
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        assessment_id = f"fresh-{digest}"

        return FreshnessAssessment(
            assessment_id=assessment_id,
            subject_type=subject_type,
            subject_id=subject_id,
            field_path=field_path,
            source_id=source_id,
            provenance_id=provenance_id,
            observed_at=observed_at,
            evaluated_at=evaluated_at,
            ttl_seconds=ttl_seconds,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            correlation_id=correlation_id,
            metadata=metadata,
        )

    def _maybe_persist_assessment(
        self, assessment: FreshnessAssessment, persist: bool
    ) -> FreshnessAssessment:
        if persist and self.assessment_repo:
            return self.assessment_repo.save_assessment(assessment)
        return assessment
