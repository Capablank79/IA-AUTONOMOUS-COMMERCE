"""
Servicio de aplicación para Duplicate Detection (Hito L.7 - Transversal Data Quality / Governance).

Implementa:
- DuplicateDetectionService:
  * evaluate_pair: Evalúa si dos DuplicateCandidate constituyen un duplicado lógico.
  * evaluate_candidate: Evalúa un DuplicateCandidate contra un conjunto existente de registros o repositorio.
  * create_default_product_dedup_policy: Política estándar para deduplicación de observaciones de catálogo.
  * create_default_replay_policy: Política estándar para replays idempotentes.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import logging
from types import MappingProxyType
from typing import Optional, Sequence, List, Dict, Tuple, Mapping, Any, Union
import uuid

from src.domain.duplicate_detection.models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    DuplicateDetectionResult,
    DuplicateGroup,
    compute_semantic_fingerprint,
    compute_duplicate_result_checksum,
    compute_duplicate_group_checksum,
)
from src.domain.duplicate_detection.ports import (
    DuplicateDetectionPolicyRepositoryPort,
    DuplicateDetectionRepositoryPort,
)
from src.domain.entity_resolution.models import MatchStatus, ResolvedEntity
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)

logger = logging.getLogger(__name__)


def create_default_product_dedup_policy() -> DuplicateDetectionPolicy:
    """Crea política estándar de deduplicación para registros de producto / catálogo."""
    return DuplicateDetectionPolicy(
        policy_id="default_product_dedup_policy_v1",
        name="Default Product Duplicate Detection Policy v1.0.0",
        version="1.0.0",
        identity_fields=("brand", "model", "price", "currency", "availability"),
        ignored_fields=("created_at", "updated_at", "trace_id", "span_id", "row_id", "_id"),
        require_same_source=True,
        allow_cross_source_duplicates=False,
        temporal_window_seconds=86400,  # 24h
        allow_replay_idempotency=True,
    )


def create_default_replay_policy() -> DuplicateDetectionPolicy:
    """Crea política estándar para detección de replays lógicos idénticos."""
    return DuplicateDetectionPolicy(
        policy_id="default_replay_policy_v1",
        name="Default Replay Duplicate Detection Policy v1.0.0",
        version="1.0.0",
        identity_fields=(),
        ignored_fields=("trace_id", "span_id", "timestamp", "received_at"),
        require_same_source=False,
        allow_cross_source_duplicates=True,
        temporal_window_seconds=None,  # Infinito / sin ventana
        allow_replay_idempotency=True,
    )


class DuplicateDetectionService:
    """
    Servicio determinista de detección de duplicados.

    Principios L.7:
    - SAME ENTITY != DUPLICATE: Si dos registros corresponden a la misma entidad canónica (L.6),
      se evalúa si representan el mismo hecho lógico idéntico o si son observaciones separadas en el tiempo/fuente.
    - NO colapsa historia: Dos observaciones con distinto timestamp fuera de la ventana configurada son NOT_DUPLICATE.
    - NO mezcla fuentes como duplicados automáticos: Evidencias de distintas fuentes se preservan independientes.
    - REPLAY EXACTO: Mismo logical record_id/idempotency_key con mismo payload semántico es REPLAY_DUPLICATE / EXACT_DUPLICATE.
    - NO invade L.8: No elige winners, no fusiona valores, no borra registros.
    - Preserva UNKNOWN: UNKNOWN != NOT_DUPLICATE y UNKNOWN != DUPLICATE.
    """

    def __init__(
        self,
        repository: Optional[DuplicateDetectionRepositoryPort] = None,
        policy_repository: Optional[DuplicateDetectionPolicyRepositoryPort] = None,
        entity_resolution_service: Optional[Any] = None,
        audit_service: Optional[Any] = None,
        clock: Optional[Any] = None,
    ):
        self._repository = repository
        self._policy_repository = policy_repository
        self._entity_resolution_service = entity_resolution_service
        self._audit_service = audit_service
        self._clock = clock

    def get_now(self) -> datetime:
        if self._clock and hasattr(self._clock, "now"):
            return self._clock.now()
        return datetime.now(timezone.utc)

    def resolve_policy(
        self,
        policy_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> DuplicateDetectionPolicy:
        """Obtiene la política de deduplicación solicitada o la default."""
        if self._policy_repository and policy_id:
            pol = self._policy_repository.get_policy(policy_id, version)
            if pol:
                return pol

        if policy_id == "default_replay_policy_v1":
            return create_default_replay_policy()

        return create_default_product_dedup_policy()

    def evaluate_pair(
        self,
        record_a: DuplicateCandidate,
        record_b: DuplicateCandidate,
        policy: Optional[DuplicateDetectionPolicy] = None,
    ) -> DuplicateDetectionResult:
        """
        Evalúa si dos registros candidatos representan el mismo hecho lógico repetido (duplicado).
        """
        active_policy = policy or self.resolve_policy()
        evaluated_at = self.get_now()

        # Generar fingerprints con la política activa
        fp_a = compute_semantic_fingerprint(
            payload=record_a.payload,
            identity_fields=active_policy.identity_fields,
            ignored_fields=active_policy.ignored_fields,
            canonical_entity_id=record_a.canonical_entity_id,
        )
        fp_b = compute_semantic_fingerprint(
            payload=record_b.payload,
            identity_fields=active_policy.identity_fields,
            ignored_fields=active_policy.ignored_fields,
            canonical_entity_id=record_b.canonical_entity_id,
        )

        result_id = f"dup_res_{hashlib.sha256(f'{record_a.record_id}:{record_b.record_id}:{active_policy.policy_id}:{active_policy.version}:{fp_a}:{fp_b}'.encode('utf-8')).hexdigest()[:16]}"

        # 0. Datos insuficientes -> UNKNOWN
        if not record_a.payload or not record_b.payload:
            return DuplicateDetectionResult(
                result_id=result_id,
                primary_record_id=record_a.record_id,
                secondary_record_id=record_b.record_id,
                status=DuplicateStatus.UNKNOWN,
                reason_code=DuplicateReasonCode.INSUFFICIENT_DATA,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                primary_fingerprint=fp_a,
                secondary_fingerprint=fp_b,
                evaluated_at=evaluated_at,
                is_exact_replay=False,
                confidence_score=Decimal("0.5000"),
                details={"reason": "Empty payload on one or both records"},
            )

        # 1. Caso Replay / Idempotencia exacta (mismo record_id o misma idempotency_key)
        is_same_id = (record_a.record_id == record_b.record_id)
        is_same_idemp = bool(
            record_a.idempotency_key and
            record_b.idempotency_key and
            record_a.idempotency_key == record_b.idempotency_key
        )

        if (is_same_id or is_same_idemp) and active_policy.allow_replay_idempotency:
            if fp_a == fp_b:
                return DuplicateDetectionResult(
                    result_id=result_id,
                    primary_record_id=record_a.record_id,
                    secondary_record_id=record_b.record_id,
                    status=DuplicateStatus.REPLAY_DUPLICATE,
                    reason_code=DuplicateReasonCode.REPLAY_PAYLOAD_MATCH,
                    policy_id=active_policy.policy_id,
                    policy_version=active_policy.version,
                    primary_fingerprint=fp_a,
                    secondary_fingerprint=fp_b,
                    evaluated_at=evaluated_at,
                    is_exact_replay=True,
                    confidence_score=Decimal("1.0000"),
                    details={"match_type": "exact_replay", "same_id": is_same_id, "same_idemp": is_same_idemp},
                )
            else:
                # Mismo ID pero payload alterado = Error / conflicto de replay
                return DuplicateDetectionResult(
                    result_id=result_id,
                    primary_record_id=record_a.record_id,
                    secondary_record_id=record_b.record_id,
                    status=DuplicateStatus.ERROR,
                    reason_code=DuplicateReasonCode.POLICY_MISMATCH,
                    policy_id=active_policy.policy_id,
                    policy_version=active_policy.version,
                    primary_fingerprint=fp_a,
                    secondary_fingerprint=fp_b,
                    evaluated_at=evaluated_at,
                    is_exact_replay=False,
                    confidence_score=Decimal("0.0000"),
                    details={"error": "Replay attempt with modified semantic payload (conflict)"},
                )

        # 2. Evaluación de Canonical Entity (L.6 boundary)
        # Si ambos tienen canonical_entity_id y son diferentes -> NOT_DUPLICATE
        if (
            record_a.canonical_entity_id and
            record_b.canonical_entity_id and
            record_a.canonical_entity_id != record_b.canonical_entity_id
        ):
            return DuplicateDetectionResult(
                result_id=result_id,
                primary_record_id=record_a.record_id,
                secondary_record_id=record_b.record_id,
                status=DuplicateStatus.NOT_DUPLICATE,
                reason_code=DuplicateReasonCode.DIFFERENT_CANONICAL_ENTITY,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                primary_fingerprint=fp_a,
                secondary_fingerprint=fp_b,
                evaluated_at=evaluated_at,
                is_exact_replay=False,
                confidence_score=Decimal("1.0000"),
                details={
                    "canonical_a": record_a.canonical_entity_id,
                    "canonical_b": record_b.canonical_entity_id,
                },
            )

        # 3. Cross-Source Check
        is_cross_source = (record_a.source_id != record_b.source_id)
        if is_cross_source:
            if active_policy.require_same_source or not active_policy.allow_cross_source_duplicates:
                # Distinta fuente = Evidencias independientes, NO duplicado automático
                return DuplicateDetectionResult(
                    result_id=result_id,
                    primary_record_id=record_a.record_id,
                    secondary_record_id=record_b.record_id,
                    status=DuplicateStatus.NOT_DUPLICATE,
                    reason_code=DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE,
                    policy_id=active_policy.policy_id,
                    policy_version=active_policy.version,
                    primary_fingerprint=fp_a,
                    secondary_fingerprint=fp_b,
                    evaluated_at=evaluated_at,
                    is_exact_replay=False,
                    confidence_score=Decimal("0.9500"),
                    details={
                        "source_a": record_a.source_id,
                        "source_b": record_b.source_id,
                        "reason": "Independent sources preserved as separate evidence",
                    },
                )

        # 4. Fingerprint Mismatch Check
        if fp_a != fp_b:
            return DuplicateDetectionResult(
                result_id=result_id,
                primary_record_id=record_a.record_id,
                secondary_record_id=record_b.record_id,
                status=DuplicateStatus.NOT_DUPLICATE,
                reason_code=DuplicateReasonCode.SEMANTIC_PAYLOAD_MISMATCH,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                primary_fingerprint=fp_a,
                secondary_fingerprint=fp_b,
                evaluated_at=evaluated_at,
                is_exact_replay=False,
                confidence_score=Decimal("1.0000"),
                details={"fp_a": fp_a, "fp_b": fp_b},
            )

        # 5. Temporal Window Check (si fingerprints son iguales y es la misma fuente/entidad)
        if active_policy.temporal_window_seconds is not None:
            if record_a.observed_at and record_b.observed_at:
                delta_seconds = abs((record_a.observed_at - record_b.observed_at).total_seconds())
                if delta_seconds > active_policy.temporal_window_seconds:
                    # Fuera de la ventana temporal: son observaciones legítimas en tiempos distintos (ej. precio lunes vs martes)
                    return DuplicateDetectionResult(
                        result_id=result_id,
                        primary_record_id=record_a.record_id,
                        secondary_record_id=record_b.record_id,
                        status=DuplicateStatus.NOT_DUPLICATE,
                        reason_code=DuplicateReasonCode.SAME_ENTITY_DISTINCT_TEMPORAL_EVENT,
                        policy_id=active_policy.policy_id,
                        policy_version=active_policy.version,
                        primary_fingerprint=fp_a,
                        secondary_fingerprint=fp_b,
                        evaluated_at=evaluated_at,
                        is_exact_replay=False,
                        confidence_score=Decimal("0.9000"),
                        details={
                            "delta_seconds": delta_seconds,
                            "window_seconds": active_policy.temporal_window_seconds,
                            "observed_a": record_a.observed_at.isoformat(),
                            "observed_b": record_b.observed_at.isoformat(),
                        },
                    )
            elif (record_a.observed_at is None) ^ (record_b.observed_at is None):
                # Uno tiene fecha y el otro no -> posible duplicado o UNKNOWN
                return DuplicateDetectionResult(
                    result_id=result_id,
                    primary_record_id=record_a.record_id,
                    secondary_record_id=record_b.record_id,
                    status=DuplicateStatus.POSSIBLE_DUPLICATE,
                    reason_code=DuplicateReasonCode.AMBIGUOUS_EVIDENCE,
                    policy_id=active_policy.policy_id,
                    policy_version=active_policy.version,
                    primary_fingerprint=fp_a,
                    secondary_fingerprint=fp_b,
                    evaluated_at=evaluated_at,
                    is_exact_replay=False,
                    confidence_score=Decimal("0.6000"),
                    details={"reason": "Missing observed_at on one record with temporal policy enabled"},
                )

        # 6. Duplicado Exacto Confirmado dentro de la misma fuente/ventana
        return DuplicateDetectionResult(
            result_id=result_id,
            primary_record_id=record_a.record_id,
            secondary_record_id=record_b.record_id,
            status=DuplicateStatus.DUPLICATE,
            reason_code=DuplicateReasonCode.EXACT_SEMANTIC_MATCH,
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            primary_fingerprint=fp_a,
            secondary_fingerprint=fp_b,
            evaluated_at=evaluated_at,
            is_exact_replay=False,
            confidence_score=Decimal("1.0000"),
            details={"match_type": "exact_semantic_duplicate"},
        )

    def detect_in_batch(
        self,
        candidates: Sequence[DuplicateCandidate],
        policy: Optional[DuplicateDetectionPolicy] = None,
    ) -> Tuple[Sequence[DuplicateDetectionResult], Sequence[DuplicateGroup]]:
        """
        Evalúa un lote de candidatos y detecta relaciones de duplicación, construyendo DuplicateGroups.
        """
        active_policy = policy or self.resolve_policy()
        results: List[DuplicateDetectionResult] = []
        # Agrupación por canonical_fingerprint
        groups_by_fp: Dict[str, List[DuplicateCandidate]] = {}

        for i in range(len(candidates)):
            cand_a = candidates[i]
            for j in range(i + 1, len(candidates)):
                cand_b = candidates[j]
                res = self.evaluate_pair(cand_a, cand_b, active_policy)
                results.append(res)
                if res.status in (DuplicateStatus.DUPLICATE, DuplicateStatus.EXACT_DUPLICATE, DuplicateStatus.REPLAY_DUPLICATE):
                    # Ambos pertenecen al mismo grupo de duplicados
                    fp = res.primary_fingerprint
                    if fp not in groups_by_fp:
                        groups_by_fp[fp] = []
                    if cand_a not in groups_by_fp[fp]:
                        groups_by_fp[fp].append(cand_a)
                    if cand_b not in groups_by_fp[fp]:
                        groups_by_fp[fp].append(cand_b)

        # Construir DuplicateGroups
        duplicate_groups: List[DuplicateGroup] = []
        now = self.get_now()
        for fp, group_members in groups_by_fp.items():
            member_ids = tuple(sorted(set(m.record_id for m in group_members)))
            canonical_entity_id = group_members[0].canonical_entity_id if group_members else None
            group_id = f"dupgroup_{hashlib.sha256(f'{fp}:{canonical_entity_id}'.encode('utf-8')).hexdigest()[:16]}"
            group = DuplicateGroup(
                group_id=group_id,
                canonical_fingerprint=fp,
                member_record_ids=member_ids,
                canonical_entity_id=canonical_entity_id,
                created_at=now,
                updated_at=now,
                metadata={"member_count": len(member_ids)},
            )
            duplicate_groups.append(group)

        # Si hay repositorio, persistir resultados y grupos
        if self._repository:
            for r in results:
                self._repository.save_result(r)
            for g in duplicate_groups:
                self._repository.save_group(g)

        return tuple(results), tuple(duplicate_groups)
