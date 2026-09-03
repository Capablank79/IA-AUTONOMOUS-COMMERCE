"""
Servicio de aplicación para Conflict Resolution (Hito L.8 - Transversal Data Quality / Governance).

Implementa:
- ConflictResolutionService:
  * resolve_conflict: Resuelve conflictos entre dos o más ConflictCandidate aplicando una ConflictResolutionPolicy.
  * create_default_source_priority_policy: Política basada en precedencia explícita de fuentes.
  * create_default_freshness_policy: Política basada en dato más fresco (L.3).
  * create_default_confidence_policy: Política basada en mayor confianza (L.4).
  * create_default_consensus_policy: Política basada en consenso entre fuentes independientes (L.7 duplicate-safe).

Principios L.8:
- REUSE > EXTEND > CREATE: Reutiliza evaluaciones de L.3 Freshness, L.4 Confidence y L.7 Duplicate Detection.
- Preservar evidencia original: No borrar registros, no colapsar fuentes.
- No ganador arbitrario: Ante empates, falta de evidencia o missing policy -> UNRESOLVED.
- UNKNOWN seguro: UNKNOWN en freshness o confidence nunca gana sobre FRESH o HIGH ni se asume como tal.
- Duplicados protegidos (L.7): Votos duplicados/replays no inflan el consenso (1 source replay = 1 logical vote).
- Determinismo total: Ordenamiento determinista de candidatos y políticas.
"""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
from typing import Optional, Sequence, List, Dict, Tuple, Mapping, Any, Union

from src.domain.conflict_resolution.models import (
    ConflictStatus,
    ConflictReasonCode,
    ResolutionStrategy,
    ConflictCandidate,
    ConflictResolutionPolicy,
    ConflictResolutionResult,
    normalize_conflict_value,
    compute_conflict_result_checksum,
)
from src.domain.conflict_resolution.ports import (
    ConflictResolutionPolicyRepositoryPort,
    ConflictResolutionRepositoryPort,
)
from src.domain.freshness.models import FreshnessStatus
from src.domain.confidence.models import ConfidenceLevel
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


def create_default_source_priority_policy(
    policy_id: str = "default_source_priority_policy_v1",
    precedence: Sequence[str] = ("primary_catalog", "supplier_direct", "secondary_feed"),
    field_path: Optional[str] = None,
    subject_type: Optional[str] = None,
) -> ConflictResolutionPolicy:
    """Crea una política estándar basada en prioridad explícita de fuentes."""
    return ConflictResolutionPolicy(
        policy_id=policy_id,
        name="Default Source Priority Conflict Resolution Policy v1.0.0",
        version="1.0.0",
        applicable_subject_type=subject_type,
        applicable_field_path=field_path,
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        source_precedence=tuple(precedence),
        require_freshness=False,
        allow_unresolved=True,
    )


def create_default_freshness_policy(
    policy_id: str = "default_freshness_policy_v1",
    max_acceptable_age_seconds: Optional[int] = 86400,
    field_path: Optional[str] = None,
    subject_type: Optional[str] = None,
) -> ConflictResolutionPolicy:
    """Crea una política estándar basada en frescura temporal (L.3)."""
    return ConflictResolutionPolicy(
        policy_id=policy_id,
        name="Default Freshness Conflict Resolution Policy v1.0.0",
        version="1.0.0",
        applicable_subject_type=subject_type,
        applicable_field_path=field_path,
        strategy=ResolutionStrategy.FRESHEST,
        require_freshness=True,
        max_acceptable_age_seconds=max_acceptable_age_seconds,
        allow_unresolved=True,
    )


def create_default_confidence_policy(
    policy_id: str = "default_confidence_policy_v1",
    min_confidence_level: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    field_path: Optional[str] = None,
    subject_type: Optional[str] = None,
) -> ConflictResolutionPolicy:
    """Crea una política estándar basada en nivel de confianza (L.4)."""
    return ConflictResolutionPolicy(
        policy_id=policy_id,
        name="Default Confidence Conflict Resolution Policy v1.0.0",
        version="1.0.0",
        applicable_subject_type=subject_type,
        applicable_field_path=field_path,
        strategy=ResolutionStrategy.HIGHEST_CONFIDENCE,
        min_confidence_level=min_confidence_level,
        allow_unresolved=True,
    )


def create_default_consensus_policy(
    policy_id: str = "default_consensus_policy_v1",
    min_votes: int = 2,
    min_ratio: Decimal = Decimal("0.6667"),
    field_path: Optional[str] = None,
    subject_type: Optional[str] = None,
) -> ConflictResolutionPolicy:
    """Crea una política estándar basada en consenso determinista libre de duplicados (L.7)."""
    return ConflictResolutionPolicy(
        policy_id=policy_id,
        name="Default Consensus Conflict Resolution Policy v1.0.0",
        version="1.0.0",
        applicable_subject_type=subject_type,
        applicable_field_path=field_path,
        strategy=ResolutionStrategy.CONSENSUS,
        consensus_min_votes=min_votes,
        consensus_min_ratio=min_ratio,
        allow_unresolved=True,
    )


class ConflictResolutionService:
    """
    Servicio de dominio/aplicación determinista para resolución de conflictos (Hito L.8).
    """

    def __init__(
        self,
        policy_repo: Optional[ConflictResolutionPolicyRepositoryPort] = None,
        result_repo: Optional[ConflictResolutionRepositoryPort] = None,
    ):
        self._policy_repo = policy_repo
        self._result_repo = result_repo

    def resolve_conflict(
        self,
        candidates: Sequence[ConflictCandidate],
        policy: Optional[ConflictResolutionPolicy] = None,
        policy_id: Optional[str] = None,
        policy_version: Optional[str] = None,
        correlation_id: Optional[str] = None,
        evaluated_at: Optional[datetime] = None,
    ) -> ConflictResolutionResult:
        """
        Resuelve deterministamente un conflicto entre candidatos.
        """
        now = evaluated_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        corr_id = correlation_id or "corr_default"
        validate_safe_identifier(corr_id, "correlation_id")

        # 1. Validación de candidatos básicos
        if not candidates:
            res_id = self._generate_deterministic_result_id("empty", "none", now)
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id="unknown_entity",
                field_path="unknown_field",
                candidate_ids=(),
                strategy=ResolutionStrategy.SOURCE_PRIORITY,
                status=ConflictStatus.ERROR,
                reason_code=ConflictReasonCode.INVALID_CANDIDATES,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id if policy else (policy_id or "no_policy"),
                policy_version=policy.version if policy else (policy_version or "1.0.0"),
                evaluated_at=now,
                correlation_id=corr_id,
                details={"error": "no candidates provided"},
            )

        # Ordenar candidatos deterministamente por candidate_id
        sorted_candidates = tuple(sorted(candidates, key=lambda c: (c.canonical_entity_id, c.field_path, c.candidate_id)))
        first_c = sorted_candidates[0]
        canonical_entity_id = first_c.canonical_entity_id
        field_path = first_c.field_path
        candidate_ids = tuple(c.candidate_id for c in sorted_candidates)

        res_id = self._generate_deterministic_result_id(canonical_entity_id, field_path, now, candidate_ids)

        # 2. Verificar si todos los candidatos apuntan a la misma entidad canónica
        different_entities = any(c.canonical_entity_id != canonical_entity_id for c in sorted_candidates)
        if different_entities:
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy if policy else ResolutionStrategy.SOURCE_PRIORITY,
                status=ConflictStatus.NO_CONFLICT,
                reason_code=ConflictReasonCode.NO_CONFLICT_DIFFERENT_ENTITIES,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id if policy else (policy_id or "no_policy"),
                policy_version=policy.version if policy else (policy_version or "1.0.0"),
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "candidates belong to different canonical entities; no conflict to resolve"},
            )

        # 3. Verificar si todos los candidatos apuntan al mismo field_path
        different_fields = any(c.field_path != field_path for c in sorted_candidates)
        if different_fields:
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy if policy else ResolutionStrategy.SOURCE_PRIORITY,
                status=ConflictStatus.ERROR,
                reason_code=ConflictReasonCode.INVALID_CANDIDATES,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id if policy else (policy_id or "no_policy"),
                policy_version=policy.version if policy else (policy_version or "1.0.0"),
                evaluated_at=now,
                correlation_id=corr_id,
                details={"error": "candidates have different field_paths"},
            )

        # 4. Obtener la política
        active_policy = policy
        if active_policy is None and policy_id is not None and self._policy_repo is not None:
            active_policy = self._policy_repo.get_policy(policy_id, policy_version)

        if active_policy is None:
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=ResolutionStrategy.SOURCE_PRIORITY,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.MISSING_POLICY,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy_id or "missing_policy",
                policy_version=policy_version or "1.0.0",
                evaluated_at=now,
                correlation_id=corr_id,
                details={"error": "no valid resolution policy provided or found"},
            )

        # 5. Caso: Un solo candidato
        if len(sorted_candidates) == 1:
            cand = sorted_candidates[0]
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=active_policy.strategy,
                status=ConflictStatus.NO_CONFLICT,
                reason_code=ConflictReasonCode.NO_CONFLICT_SINGLE_CANDIDATE,
                selected_candidate_id=cand.candidate_id,
                selected_value=cand.value,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "single candidate provided"},
            )

        # 6. Caso: Valores idénticos normalizados (NO_CONFLICT)
        normalized_values = [normalize_conflict_value(c.value) for c in sorted_candidates]
        first_norm = normalized_values[0]
        if all(nv == first_norm for nv in normalized_values):
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=active_policy.strategy,
                status=ConflictStatus.NO_CONFLICT,
                reason_code=ConflictReasonCode.NO_CONFLICT_IDENTICAL_VALUES,
                selected_candidate_id=sorted_candidates[0].candidate_id,
                selected_value=sorted_candidates[0].value,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "all candidates have identical normalized values"},
            )

        # 7. Aplicar Estrategia según la Política
        strategy = active_policy.strategy

        if strategy == ResolutionStrategy.MANUAL_REQUIRED:
            return self._create_and_persist_result(
                conflict_id=res_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.MANUAL_RESOLUTION_REQUIRED,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "policy requires manual resolution"},
            )

        if strategy == ResolutionStrategy.SOURCE_PRIORITY:
            return self._resolve_by_source_priority(
                res_id, canonical_entity_id, field_path, sorted_candidates, active_policy, now, corr_id
            )

        if strategy == ResolutionStrategy.FRESHEST:
            return self._resolve_by_freshest(
                res_id, canonical_entity_id, field_path, sorted_candidates, active_policy, now, corr_id
            )

        if strategy == ResolutionStrategy.HIGHEST_CONFIDENCE:
            return self._resolve_by_confidence(
                res_id, canonical_entity_id, field_path, sorted_candidates, active_policy, now, corr_id
            )

        if strategy == ResolutionStrategy.CONSENSUS:
            return self._resolve_by_consensus(
                res_id, canonical_entity_id, field_path, sorted_candidates, active_policy, now, corr_id
            )

        # Estrategia desconocida / no soportada
        return self._create_and_persist_result(
            conflict_id=res_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=candidate_ids,
            strategy=strategy,
            status=ConflictStatus.ERROR,
            reason_code=ConflictReasonCode.INVALID_CANDIDATES,
            selected_candidate_id=None,
            selected_value=None,
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            evaluated_at=now,
            correlation_id=corr_id,
            details={"error": f"unsupported strategy {strategy}"},
        )

    def _resolve_by_source_priority(
        self,
        conflict_id: str,
        canonical_entity_id: str,
        field_path: str,
        candidates: Sequence[ConflictCandidate],
        policy: ConflictResolutionPolicy,
        now: datetime,
        corr_id: str,
    ) -> ConflictResolutionResult:
        precedence = policy.source_precedence
        candidate_ids = tuple(c.candidate_id for c in candidates)

        # Mapear source_id a su índice de precedencia
        source_rank: Dict[str, int] = {src: idx for idx, src in enumerate(precedence)}

        # Filtrar candidatos que cumplan requisitos si require_freshness o min_confidence están definidos
        eligible_candidates: List[ConflictCandidate] = []
        for c in candidates:
            if not self._check_candidate_eligibility(c, policy):
                continue
            eligible_candidates.append(c)

        if not eligible_candidates:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_INSUFFICIENT_EVIDENCE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "no candidates met policy eligibility requirements"},
            )

        # Determinar el mejor rank de fuente
        # Las fuentes en precedence tienen rank 0, 1, 2... Las no mencionadas tienen infinito
        ranked_candidates: List[Tuple[int, ConflictCandidate]] = []
        for c in eligible_candidates:
            rank = source_rank.get(c.source_id, 999999)
            ranked_candidates.append((rank, c))

        ranked_candidates.sort(key=lambda x: (x[0], x[1].candidate_id))
        best_rank = ranked_candidates[0][0]

        if best_rank == 999999:
            # Ningún candidato pertenece a la lista de precedencia
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_INSUFFICIENT_EVIDENCE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "none of the candidate sources are in the precedence list"},
            )

        # Candidatos empatados con el mejor rank
        top_candidates = [c for rank, c in ranked_candidates if rank == best_rank]

        # Si hay más de un candidato de la misma fuente con valores distintos
        distinct_vals = {json.dumps(normalize_conflict_value(c.value), sort_keys=True) for c in top_candidates}
        if len(distinct_vals) > 1:
            # Empate irresoluble bajo source_priority pura
            if policy.tie_break_strategy:
                # Intentar tie-break recursivo/delegado
                return self._apply_tie_break(
                    conflict_id, canonical_entity_id, field_path, top_candidates, policy, now, corr_id
                )
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_TIE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": f"multiple contradictory candidates from top-priority source {top_candidates[0].source_id}"},
            )

        winner = top_candidates[0]
        return self._create_and_persist_result(
            conflict_id=conflict_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=candidate_ids,
            strategy=policy.strategy,
            status=ConflictStatus.RESOLVED,
            reason_code=ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY,
            selected_candidate_id=winner.candidate_id,
            selected_value=winner.value,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            evaluated_at=now,
            correlation_id=corr_id,
            details={"winning_source": winner.source_id, "rank": best_rank},
        )

    def _resolve_by_freshest(
        self,
        conflict_id: str,
        canonical_entity_id: str,
        field_path: str,
        candidates: Sequence[ConflictCandidate],
        policy: ConflictResolutionPolicy,
        now: datetime,
        corr_id: str,
    ) -> ConflictResolutionResult:
        candidate_ids = tuple(c.candidate_id for c in candidates)

        # Reglas L.8:
        # Expired data no debe ganar sobre fresh data.
        # UNKNOWN freshness -> no asumir que es más fresco.
        # Comparar FreshnessAssessment / observed_at o freshness_age_seconds válidos.

        valid_candidates: List[ConflictCandidate] = []
        for c in candidates:
            # Descartar EXPIRED, ERROR o UNKNOWN si require_freshness está activo o no tiene timestamp
            if c.freshness_status in (FreshnessStatus.EXPIRED, FreshnessStatus.ERROR, FreshnessStatus.UNKNOWN):
                continue
            if c.observed_at is None and c.freshness_age_seconds is None:
                continue
            if policy.max_acceptable_age_seconds is not None and c.freshness_age_seconds is not None:
                if c.freshness_age_seconds > Decimal(str(policy.max_acceptable_age_seconds)):
                    continue
            valid_candidates.append(c)

        if not valid_candidates:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_EXPIRED_OR_STALE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "no candidates with valid fresh timestamps / assessments"},
            )

        # Ordenar por frescura: menor freshness_age_seconds o fecha más reciente (mayor observed_at)
        # Convertimos todo a timestamp datetime comparable
        def get_sort_key(c: ConflictCandidate) -> Tuple[datetime, str]:
            dt = c.observed_at or datetime.min.replace(tzinfo=timezone.utc)
            return (dt, c.candidate_id)

        sorted_by_freshness = sorted(valid_candidates, key=get_sort_key, reverse=True)
        top_candidate = sorted_by_freshness[0]
        top_dt = top_candidate.observed_at

        # Verificar empates de frescura con valores discrepantes
        tied_candidates = [
            c for c in sorted_by_freshness
            if c.observed_at == top_dt and json.dumps(normalize_conflict_value(c.value), sort_keys=True) != json.dumps(normalize_conflict_value(top_candidate.value), sort_keys=True)
        ]

        if tied_candidates:
            if policy.tie_break_strategy:
                return self._apply_tie_break(
                    conflict_id, canonical_entity_id, field_path, [top_candidate] + tied_candidates, policy, now, corr_id
                )
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_TIE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "tie in freshness between contradictory candidates"},
            )

        return self._create_and_persist_result(
            conflict_id=conflict_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=candidate_ids,
            strategy=policy.strategy,
            status=ConflictStatus.RESOLVED,
            reason_code=ConflictReasonCode.RESOLVED_BY_FRESHEST,
            selected_candidate_id=top_candidate.candidate_id,
            selected_value=top_candidate.value,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            evaluated_at=now,
            correlation_id=corr_id,
            details={"observed_at": top_candidate.observed_at.isoformat() if top_candidate.observed_at else None},
        )

    def _resolve_by_confidence(
        self,
        conflict_id: str,
        canonical_entity_id: str,
        field_path: str,
        candidates: Sequence[ConflictCandidate],
        policy: ConflictResolutionPolicy,
        now: datetime,
        corr_id: str,
    ) -> ConflictResolutionResult:
        candidate_ids = tuple(c.candidate_id for c in candidates)

        # Reglas L.8:
        # HIGH > MEDIUM > LOW
        # UNKNOWN confidence -> no tratar como HIGH ni LOW válido si min_confidence está configurado.
        level_map = {
            ConfidenceLevel.HIGH: 3,
            ConfidenceLevel.MEDIUM: 2,
            ConfidenceLevel.LOW: 1,
            ConfidenceLevel.UNKNOWN: 0,
            ConfidenceLevel.ERROR: -1,
        }

        valid_candidates: List[ConflictCandidate] = []
        for c in candidates:
            if c.confidence_level in (ConfidenceLevel.UNKNOWN, ConfidenceLevel.ERROR, None):
                continue
            if policy.min_confidence_level is not None:
                min_req = level_map.get(policy.min_confidence_level, 0)
                curr = level_map.get(c.confidence_level, 0)
                if curr < min_req:
                    continue
            if policy.min_confidence_score is not None and c.confidence_score is not None:
                if c.confidence_score < policy.min_confidence_score:
                    continue
            valid_candidates.append(c)

        if not valid_candidates:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_UNKNOWN_ASSESSMENTS,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "no candidates meet minimum confidence requirements"},
            )

        # Ordenar por level_map y luego por confidence_score
        def get_sort_key(c: ConflictCandidate) -> Tuple[int, Decimal, str]:
            lvl = level_map.get(c.confidence_level, 0)
            score = c.confidence_score if c.confidence_score is not None else Decimal("0")
            return (lvl, score, c.candidate_id)

        sorted_by_confidence = sorted(valid_candidates, key=get_sort_key, reverse=True)
        top_candidate = sorted_by_confidence[0]
        top_lvl = level_map.get(top_candidate.confidence_level, 0)
        top_score = top_candidate.confidence_score

        # Verificar empates con valores contradictorios
        tied_candidates = [
            c for c in sorted_by_confidence
            if level_map.get(c.confidence_level, 0) == top_lvl
            and c.confidence_score == top_score
            and json.dumps(normalize_conflict_value(c.value), sort_keys=True) != json.dumps(normalize_conflict_value(top_candidate.value), sort_keys=True)
        ]

        if tied_candidates:
            if policy.tie_break_strategy:
                return self._apply_tie_break(
                    conflict_id, canonical_entity_id, field_path, [top_candidate] + tied_candidates, policy, now, corr_id
                )
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_TIE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "tie in confidence score/level between contradictory candidates"},
            )

        return self._create_and_persist_result(
            conflict_id=conflict_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=candidate_ids,
            strategy=policy.strategy,
            status=ConflictStatus.RESOLVED,
            reason_code=ConflictReasonCode.RESOLVED_BY_HIGHEST_CONFIDENCE,
            selected_candidate_id=top_candidate.candidate_id,
            selected_value=top_candidate.value,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            evaluated_at=now,
            correlation_id=corr_id,
            details={
                "confidence_level": top_candidate.confidence_level.value if top_candidate.confidence_level else None,
                "confidence_score": str(top_candidate.confidence_score) if top_candidate.confidence_score is not None else None,
            },
        )

    def _resolve_by_consensus(
        self,
        conflict_id: str,
        canonical_entity_id: str,
        field_path: str,
        candidates: Sequence[ConflictCandidate],
        policy: ConflictResolutionPolicy,
        now: datetime,
        corr_id: str,
    ) -> ConflictResolutionResult:
        candidate_ids = tuple(c.candidate_id for c in candidates)

        # Regla L.8 + L.7:
        # Duplicados/replays NO se cuentan como votos independientes.
        # Agrupar por fuente y/o fingerprint semántico/deduplication_fingerprint para consolidar evidencias independientes.
        # Cada fuente independiente aporta a lo sumo 1 voto por valor lógico.

        # Filtramos primero duplicados explícitos marcados con is_duplicate=True
        deduped_candidates: List[ConflictCandidate] = []
        seen_source_fingerprint: set = set()

        for c in candidates:
            if c.is_duplicate:
                continue
            # Key de deduplicación de voto: (source_id, fingerprint o normalized_value)
            norm_val_str = json.dumps(normalize_conflict_value(c.value), sort_keys=True)
            vote_key = (c.source_id, c.deduplication_fingerprint or norm_val_str)
            if vote_key in seen_source_fingerprint:
                continue
            seen_source_fingerprint.add(vote_key)
            deduped_candidates.append(c)

        if not deduped_candidates:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_INSUFFICIENT_EVIDENCE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "no distinct non-duplicate votes available"},
            )

        # Conteo de votos por valor normalizado
        value_votes: Dict[str, List[ConflictCandidate]] = defaultdict(list)
        for c in deduped_candidates:
            norm_val_str = json.dumps(normalize_conflict_value(c.value), sort_keys=True)
            value_votes[norm_val_str].append(c)

        total_votes = len(deduped_candidates)

        # Ordenar los grupos de valores por cantidad de votos decreciente, y deterministamente por el valor serializado
        sorted_groups = sorted(
            value_votes.items(),
            key=lambda item: (len(item[1]), item[0]),
            reverse=True,
        )

        top_val_str, top_group = sorted_groups[0]
        top_votes = len(top_group)
        vote_ratio = Decimal(str(top_votes)) / Decimal(str(total_votes))

        # Verificar si cumple quorum mínimo de votos y ratio mínimo
        if top_votes < policy.consensus_min_votes or vote_ratio < policy.consensus_min_ratio:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_NO_CONSENSUS,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={
                    "total_votes": total_votes,
                    "top_votes": top_votes,
                    "vote_ratio": str(vote_ratio),
                    "required_ratio": str(policy.consensus_min_ratio),
                    "required_votes": policy.consensus_min_votes,
                },
            )

        # Verificar empate en primer lugar con otro valor
        if len(sorted_groups) > 1 and len(sorted_groups[1][1]) == top_votes:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=candidate_ids,
                strategy=policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_TIE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "consensus tie between leading values"},
            )

        # El ganador del grupo representativo
        winner = sorted(top_group, key=lambda c: c.candidate_id)[0]

        return self._create_and_persist_result(
            conflict_id=conflict_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=candidate_ids,
            strategy=policy.strategy,
            status=ConflictStatus.RESOLVED,
            reason_code=ConflictReasonCode.RESOLVED_BY_CONSENSUS,
            selected_candidate_id=winner.candidate_id,
            selected_value=winner.value,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            evaluated_at=now,
            correlation_id=corr_id,
            details={
                "total_votes": total_votes,
                "consensus_votes": top_votes,
                "vote_ratio": str(vote_ratio),
            },
        )

    def _apply_tie_break(
        self,
        conflict_id: str,
        canonical_entity_id: str,
        field_path: str,
        candidates: Sequence[ConflictCandidate],
        parent_policy: ConflictResolutionPolicy,
        now: datetime,
        corr_id: str,
    ) -> ConflictResolutionResult:
        """Aplica la estrategia de desempate secundaria configurada en la política."""
        tie_break_strat = parent_policy.tie_break_strategy
        if not tie_break_strat:
            return self._create_and_persist_result(
                conflict_id=conflict_id,
                canonical_entity_id=canonical_entity_id,
                field_path=field_path,
                candidate_ids=tuple(c.candidate_id for c in candidates),
                strategy=parent_policy.strategy,
                status=ConflictStatus.UNRESOLVED,
                reason_code=ConflictReasonCode.UNRESOLVED_TIE,
                selected_candidate_id=None,
                selected_value=None,
                policy_id=parent_policy.policy_id,
                policy_version=parent_policy.version,
                evaluated_at=now,
                correlation_id=corr_id,
                details={"info": "tie without tie-break strategy"},
            )

        # Crear sub-política temporal sin ciclos
        sub_policy = ConflictResolutionPolicy(
            policy_id=f"{parent_policy.policy_id}_tiebreak",
            name=f"Tie break policy ({tie_break_strat.value})",
            version=parent_policy.version,
            strategy=tie_break_strat,
            source_precedence=parent_policy.source_precedence,
            require_freshness=parent_policy.require_freshness,
            max_acceptable_age_seconds=parent_policy.max_acceptable_age_seconds,
            min_confidence_level=parent_policy.min_confidence_level,
            min_confidence_score=parent_policy.min_confidence_score,
            consensus_min_votes=parent_policy.consensus_min_votes,
            consensus_min_ratio=parent_policy.consensus_min_ratio,
            tie_break_strategy=None,  # Prevenir recursión infinita
            allow_unresolved=True,
        )

        if tie_break_strat == ResolutionStrategy.SOURCE_PRIORITY:
            return self._resolve_by_source_priority(conflict_id, canonical_entity_id, field_path, candidates, sub_policy, now, corr_id)
        if tie_break_strat == ResolutionStrategy.FRESHEST:
            return self._resolve_by_freshest(conflict_id, canonical_entity_id, field_path, candidates, sub_policy, now, corr_id)
        if tie_break_strat == ResolutionStrategy.HIGHEST_CONFIDENCE:
            return self._resolve_by_confidence(conflict_id, canonical_entity_id, field_path, candidates, sub_policy, now, corr_id)
        if tie_break_strat == ResolutionStrategy.CONSENSUS:
            return self._resolve_by_consensus(conflict_id, canonical_entity_id, field_path, candidates, sub_policy, now, corr_id)

        return self._create_and_persist_result(
            conflict_id=conflict_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=tuple(c.candidate_id for c in candidates),
            strategy=parent_policy.strategy,
            status=ConflictStatus.UNRESOLVED,
            reason_code=ConflictReasonCode.UNRESOLVED_TIE,
            selected_candidate_id=None,
            selected_value=None,
            policy_id=parent_policy.policy_id,
            policy_version=parent_policy.version,
            evaluated_at=now,
            correlation_id=corr_id,
            details={"info": f"unhandled tie break strategy {tie_break_strat}"},
        )

    def _check_candidate_eligibility(self, candidate: ConflictCandidate, policy: ConflictResolutionPolicy) -> bool:
        """Verifica si un candidato cumple los requerimientos mínimos de la política."""
        if policy.require_freshness:
            if candidate.freshness_status in (FreshnessStatus.EXPIRED, FreshnessStatus.ERROR, FreshnessStatus.UNKNOWN):
                return False
            if policy.max_acceptable_age_seconds is not None and candidate.freshness_age_seconds is not None:
                if candidate.freshness_age_seconds > Decimal(str(policy.max_acceptable_age_seconds)):
                    return False

        if policy.min_confidence_level is not None:
            level_map = {
                ConfidenceLevel.HIGH: 3,
                ConfidenceLevel.MEDIUM: 2,
                ConfidenceLevel.LOW: 1,
                ConfidenceLevel.UNKNOWN: 0,
                ConfidenceLevel.ERROR: -1,
            }
            cand_lvl = level_map.get(candidate.confidence_level, 0)
            req_lvl = level_map.get(policy.min_confidence_level, 0)
            if cand_lvl < req_lvl:
                return False

        if policy.min_confidence_score is not None and candidate.confidence_score is not None:
            if candidate.confidence_score < policy.min_confidence_score:
                return False

        return True

    def _generate_deterministic_result_id(
        self, canonical_entity_id: str, field_path: str, evaluated_at: datetime, candidate_ids: Sequence[str] = ()
    ) -> str:
        payload = {
            "canonical_entity_id": canonical_entity_id,
            "field_path": field_path,
            "evaluated_at": evaluated_at.astimezone(timezone.utc).isoformat(),
            "candidate_ids": list(candidate_ids),
        }
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        return f"cnf_{digest}"

    def _create_and_persist_result(
        self,
        conflict_id: str,
        canonical_entity_id: str,
        field_path: str,
        candidate_ids: Sequence[str],
        strategy: ResolutionStrategy,
        status: ConflictStatus,
        reason_code: ConflictReasonCode,
        selected_candidate_id: Optional[str],
        selected_value: Any,
        policy_id: str,
        policy_version: str,
        evaluated_at: datetime,
        correlation_id: str,
        details: Mapping[str, Any],
    ) -> ConflictResolutionResult:
        result = ConflictResolutionResult(
            conflict_id=conflict_id,
            canonical_entity_id=canonical_entity_id,
            field_path=field_path,
            candidate_ids=tuple(candidate_ids),
            strategy=strategy,
            status=status,
            reason_code=reason_code,
            selected_candidate_id=selected_candidate_id,
            selected_value=selected_value,
            policy_id=policy_id,
            policy_version=policy_version,
            evaluated_at=evaluated_at,
            correlation_id=correlation_id,
            details=details,
        )

        if self._result_repo is not None:
            self._result_repo.save_result(result)

        return result
