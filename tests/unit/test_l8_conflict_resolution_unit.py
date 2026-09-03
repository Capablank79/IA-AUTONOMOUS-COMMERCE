"""
Tests unitarios para Conflict Resolution L.8 (Transversal Data Quality / Governance).

Requerimientos mínimos obligatorios:
1. no conflict: 1 solo candidato o valores normalizados idénticos -> NO_CONFLICT
2. conflicting values: 2 fuentes con valores distintos -> detecta conflicto
3. source priority: gana la fuente de mayor prioridad según política
4. freshest wins: gana el candidato con timestamp más reciente / menor age
5. highest confidence wins: gana el candidato con mayor confianza según L.4
6. tie -> unresolved: empates sin tie-breaker quedan UNRESOLVED de forma segura
7. missing policy -> unresolved/unknown: sin política válida devuelve UNRESOLVED
8. duplicate votes not counted twice: duplicados/replays de L.7 no inflan consenso
9. consensus if supported: consenso legítimo (p.ej. 2 de 3 coinciden) resuelve conflicto
10. unknown freshness safe: UNKNOWN freshness no asume ser más fresco
11. unknown confidence safe: UNKNOWN confidence no asume ser HIGH
12. expired vs fresh: dato expirado no gana silenciosamente sobre dato fresco
13. deterministic result: misma entrada y policy -> idéntico resultado y checksum
14. policy versioning: soporte y validación SemVer de versiones de políticas
15. checksum: integridad y detección de manipulaciones
16. idempotency: ejecuciones repetidas producen idénticos resultados
17. no evidence deletion: todos los candidate_ids y valores originales se preservan
18. no hidden winner: nunca se elige un ganador arbitrario sin policy explícita
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from pathlib import Path

from src.domain.conflict_resolution.models import (
    ConflictStatus,
    ConflictReasonCode,
    ResolutionStrategy,
    ConflictCandidate,
    ConflictResolutionPolicy,
    ConflictResolutionResult,
    compute_candidate_checksum,
    compute_conflict_policy_checksum,
    compute_conflict_result_checksum,
    normalize_conflict_value,
)
from src.application.conflict_resolution.service import (
    ConflictResolutionService,
    create_default_source_priority_policy,
    create_default_freshness_policy,
    create_default_confidence_policy,
    create_default_consensus_policy,
)
from src.infrastructure.persistence.data.json.conflict_resolution_repository import (
    JsonConflictResolutionPolicyRepository,
    JsonConflictResolutionRepository,
    CorruptedConflictPolicyError,
    CorruptedConflictResultError,
    ConflictResolutionPolicyConflictError,
    ConflictResolutionConflictError,
)
from src.domain.freshness.models import FreshnessStatus
from src.domain.confidence.models import ConfidenceLevel


class TestL8ConflictResolutionUnit:

    # 1. No Conflict (Single candidate & Identical normalized values)
    def test_01_no_conflict_single_and_identical(self):
        service = ConflictResolutionService()
        policy = create_default_source_priority_policy()
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(
            candidate_id="cand_001",
            source_id="supplier_a",
            record_id="rec_001",
            canonical_entity_id="prod_canon_123",
            field_path="price",
            value=Decimal("100.00"),
            observed_at=now,
        )

        # Single candidate
        res_single = service.resolve_conflict([c1], policy=policy, evaluated_at=now)
        assert res_single.status == ConflictStatus.NO_CONFLICT
        assert res_single.reason_code == ConflictReasonCode.NO_CONFLICT_SINGLE_CANDIDATE
        assert res_single.selected_value == Decimal("100.00")

        # Identical values from 2 sources
        c2 = ConflictCandidate(
            candidate_id="cand_002",
            source_id="supplier_b",
            record_id="rec_002",
            canonical_entity_id="prod_canon_123",
            field_path="price",
            value=Decimal("100.00"),
            observed_at=now,
        )
        res_identical = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res_identical.status == ConflictStatus.NO_CONFLICT
        assert res_identical.reason_code == ConflictReasonCode.NO_CONFLICT_IDENTICAL_VALUES
        assert res_identical.selected_value == Decimal("100.00")

    # 2. Conflicting values detected
    def test_02_conflicting_values_detected(self):
        service = ConflictResolutionService()
        policy = create_default_source_priority_policy(precedence=("supplier_a", "supplier_b"))
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(
            candidate_id="cand_001",
            source_id="supplier_a",
            record_id="rec_001",
            canonical_entity_id="prod_canon_123",
            field_path="price",
            value=Decimal("100.00"),
            observed_at=now,
        )
        c2 = ConflictCandidate(
            candidate_id="cand_002",
            source_id="supplier_b",
            record_id="rec_002",
            canonical_entity_id="prod_canon_123",
            field_path="price",
            value=Decimal("120.00"),
            observed_at=now,
        )

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.selected_candidate_id == "cand_001"
        assert res.selected_value == Decimal("100.00")
        assert "cand_001" in res.candidate_ids
        assert "cand_002" in res.candidate_ids

    # 3. Source Priority
    def test_03_source_priority(self):
        service = ConflictResolutionService()
        policy = create_default_source_priority_policy(precedence=("official_brand", "supplier_feed", "scrape"))
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(
            candidate_id="cand_scrape",
            source_id="scrape",
            record_id="rec_001",
            canonical_entity_id="prod_canon_555",
            field_path="title",
            value="Old Title",
            observed_at=now,
        )
        c2 = ConflictCandidate(
            candidate_id="cand_brand",
            source_id="official_brand",
            record_id="rec_002",
            canonical_entity_id="prod_canon_555",
            field_path="title",
            value="Official Verified Title",
            observed_at=now,
        )

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.reason_code == ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY
        assert res.selected_candidate_id == "cand_brand"
        assert res.selected_value == "Official Verified Title"

    # 4. Freshest wins
    def test_04_freshest_wins(self):
        service = ConflictResolutionService()
        policy = create_default_freshness_policy()
        now = datetime.now(timezone.utc)

        c_old = ConflictCandidate(
            candidate_id="cand_yesterday",
            source_id="source_1",
            record_id="rec_001",
            canonical_entity_id="prod_canon_777",
            field_path="stock",
            value=10,
            observed_at=now - timedelta(hours=5),
            freshness_status=FreshnessStatus.FRESH,
            freshness_age_seconds=Decimal("18000"),
        )
        c_new = ConflictCandidate(
            candidate_id="cand_today",
            source_id="source_2",
            record_id="rec_002",
            canonical_entity_id="prod_canon_777",
            field_path="stock",
            value=25,
            observed_at=now - timedelta(minutes=5),
            freshness_status=FreshnessStatus.FRESH,
            freshness_age_seconds=Decimal("300"),
        )

        res = service.resolve_conflict([c_old, c_new], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.reason_code == ConflictReasonCode.RESOLVED_BY_FRESHEST
        assert res.selected_candidate_id == "cand_today"
        assert res.selected_value == 25

    # 5. Highest Confidence wins
    def test_05_highest_confidence_wins(self):
        service = ConflictResolutionService()
        policy = create_default_confidence_policy()
        now = datetime.now(timezone.utc)

        c_med = ConflictCandidate(
            candidate_id="cand_medium",
            source_id="unverified_api",
            record_id="rec_001",
            canonical_entity_id="prod_canon_888",
            field_path="is_active",
            value=False,
            observed_at=now,
            confidence_level=ConfidenceLevel.MEDIUM,
            confidence_score=Decimal("0.6500"),
        )
        c_high = ConflictCandidate(
            candidate_id="cand_high",
            source_id="verified_supplier",
            record_id="rec_002",
            canonical_entity_id="prod_canon_888",
            field_path="is_active",
            value=True,
            observed_at=now,
            confidence_level=ConfidenceLevel.HIGH,
            confidence_score=Decimal("0.9500"),
        )

        res = service.resolve_conflict([c_med, c_high], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.reason_code == ConflictReasonCode.RESOLVED_BY_HIGHEST_CONFIDENCE
        assert res.selected_candidate_id == "cand_high"
        assert res.selected_value is True

    # 6. Tie -> UNRESOLVED
    def test_06_tie_leads_to_unresolved(self):
        service = ConflictResolutionService()
        policy = ConflictResolutionPolicy(
            policy_id="policy_conf_no_tiebreak",
            name="Confidence Policy without tiebreak",
            version="1.0.0",
            strategy=ResolutionStrategy.HIGHEST_CONFIDENCE,
            tie_break_strategy=None,
        )
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(
            candidate_id="cand_a",
            source_id="source_a",
            record_id="rec_001",
            canonical_entity_id="prod_canon_999",
            field_path="price",
            value=Decimal("100"),
            observed_at=now,
            confidence_level=ConfidenceLevel.HIGH,
            confidence_score=Decimal("0.9000"),
        )
        c2 = ConflictCandidate(
            candidate_id="cand_b",
            source_id="source_b",
            record_id="rec_002",
            canonical_entity_id="prod_canon_999",
            field_path="price",
            value=Decimal("120"),
            observed_at=now,
            confidence_level=ConfidenceLevel.HIGH,
            confidence_score=Decimal("0.9000"),
        )

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.UNRESOLVED
        assert res.reason_code == ConflictReasonCode.UNRESOLVED_TIE
        assert res.selected_candidate_id is None
        assert res.selected_value is None

    # 7. Missing Policy -> UNRESOLVED
    def test_07_missing_policy_unresolved(self):
        service = ConflictResolutionService()
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(
            candidate_id="cand_a",
            source_id="source_a",
            record_id="rec_001",
            canonical_entity_id="prod_canon_111",
            field_path="price",
            value=Decimal("100"),
            observed_at=now,
        )
        c2 = ConflictCandidate(
            candidate_id="cand_b",
            source_id="source_b",
            record_id="rec_002",
            canonical_entity_id="prod_canon_111",
            field_path="price",
            value=Decimal("120"),
            observed_at=now,
        )

        res = service.resolve_conflict([c1, c2], policy=None, evaluated_at=now)
        assert res.status == ConflictStatus.UNRESOLVED
        assert res.reason_code == ConflictReasonCode.MISSING_POLICY
        assert res.selected_value is None

    # 8. Duplicate votes not counted twice in consensus
    def test_08_duplicate_votes_not_counted_twice(self):
        service = ConflictResolutionService()
        policy = create_default_consensus_policy(min_votes=2, min_ratio=Decimal("0.6667"))
        now = datetime.now(timezone.utc)

        # Source A reporta $100 5 veces (replays/duplicados)
        c_a1 = ConflictCandidate(
            candidate_id="cand_a1",
            source_id="source_a",
            record_id="rec_a1",
            canonical_entity_id="prod_canon_222",
            field_path="price",
            value=Decimal("100"),
            deduplication_fingerprint="fp_val_100",
            observed_at=now,
        )
        c_a2 = ConflictCandidate(
            candidate_id="cand_a2",
            source_id="source_a",
            record_id="rec_a2",
            canonical_entity_id="prod_canon_222",
            field_path="price",
            value=Decimal("100"),
            deduplication_fingerprint="fp_val_100",
            is_duplicate=True,
            observed_at=now,
        )
        # Source B reporta $120 una vez
        c_b1 = ConflictCandidate(
            candidate_id="cand_b1",
            source_id="source_b",
            record_id="rec_b1",
            canonical_entity_id="prod_canon_222",
            field_path="price",
            value=Decimal("120"),
            deduplication_fingerprint="fp_val_120",
            observed_at=now,
        )

        # Sin dedupe, A tendría 2 votos y B 1 (ratio 2/3 = 66.67%).
        # Con dedupe, A tiene 1 voto y B tiene 1 voto -> Total 2 votos, ratio 1/2 = 50% < 66.67% -> NO_CONSENSUS / TIE.
        res = service.resolve_conflict([c_a1, c_a2, c_b1], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.UNRESOLVED
        assert res.reason_code in (ConflictReasonCode.UNRESOLVED_NO_CONSENSUS, ConflictReasonCode.UNRESOLVED_TIE)
        assert res.selected_value is None

    # 9. Consensus if supported
    def test_09_consensus_resolves_when_supported(self):
        service = ConflictResolutionService()
        policy = create_default_consensus_policy(min_votes=2, min_ratio=Decimal("0.6000"))
        now = datetime.now(timezone.utc)

        # 3 fuentes independientes: 2 reportan $100, 1 reporta $120
        c1 = ConflictCandidate(candidate_id="cand_1", source_id="src_1", record_id="r1", canonical_entity_id="p1", field_path="price", value=Decimal("100"), observed_at=now)
        c2 = ConflictCandidate(candidate_id="cand_2", source_id="src_2", record_id="r2", canonical_entity_id="p1", field_path="price", value=Decimal("100"), observed_at=now)
        c3 = ConflictCandidate(candidate_id="cand_3", source_id="src_3", record_id="r3", canonical_entity_id="p1", field_path="price", value=Decimal("120"), observed_at=now)

        res = service.resolve_conflict([c1, c2, c3], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.reason_code == ConflictReasonCode.RESOLVED_BY_CONSENSUS
        assert res.selected_value == Decimal("100")

    # 10. Unknown freshness safe
    def test_10_unknown_freshness_safe(self):
        service = ConflictResolutionService()
        policy = create_default_freshness_policy()
        now = datetime.now(timezone.utc)

        # c1 tiene UNKNOWN freshness
        c1 = ConflictCandidate(
            candidate_id="cand_unknown",
            source_id="src_1",
            record_id="r1",
            canonical_entity_id="p1",
            field_path="price",
            value=Decimal("100"),
            observed_at=None,
            freshness_status=FreshnessStatus.UNKNOWN,
        )
        c2 = ConflictCandidate(
            candidate_id="cand_fresh",
            source_id="src_2",
            record_id="r2",
            canonical_entity_id="p1",
            field_path="price",
            value=Decimal("120"),
            observed_at=now - timedelta(minutes=10),
            freshness_status=FreshnessStatus.FRESH,
            freshness_age_seconds=Decimal("600"),
        )

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.selected_candidate_id == "cand_fresh"
        assert res.selected_value == Decimal("120")

    # 11. Unknown confidence safe
    def test_11_unknown_confidence_safe(self):
        service = ConflictResolutionService()
        policy = create_default_confidence_policy(min_confidence_level=ConfidenceLevel.LOW)
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(
            candidate_id="cand_unknown_conf",
            source_id="src_1",
            record_id="r1",
            canonical_entity_id="p1",
            field_path="price",
            value=Decimal("100"),
            observed_at=now,
            confidence_level=ConfidenceLevel.UNKNOWN,
        )
        c2 = ConflictCandidate(
            candidate_id="cand_low_conf",
            source_id="src_2",
            record_id="r2",
            canonical_entity_id="p1",
            field_path="price",
            value=Decimal("120"),
            observed_at=now,
            confidence_level=ConfidenceLevel.LOW,
            confidence_score=Decimal("0.3000"),
        )

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.selected_candidate_id == "cand_low_conf"
        assert res.selected_value == Decimal("120")

    # 12. Expired vs Fresh
    def test_12_expired_vs_fresh(self):
        service = ConflictResolutionService()
        policy = create_default_freshness_policy(max_acceptable_age_seconds=3600)
        now = datetime.now(timezone.utc)

        c_exp = ConflictCandidate(
            candidate_id="cand_expired",
            source_id="src_1",
            record_id="r1",
            canonical_entity_id="p1",
            field_path="price",
            value=Decimal("80"),
            observed_at=now - timedelta(days=2),
            freshness_status=FreshnessStatus.EXPIRED,
            freshness_age_seconds=Decimal("172800"),
        )
        c_fresh = ConflictCandidate(
            candidate_id="cand_fresh",
            source_id="src_2",
            record_id="r2",
            canonical_entity_id="p1",
            field_path="price",
            value=Decimal("95"),
            observed_at=now - timedelta(minutes=5),
            freshness_status=FreshnessStatus.FRESH,
            freshness_age_seconds=Decimal("300"),
        )

        res = service.resolve_conflict([c_exp, c_fresh], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.RESOLVED
        assert res.selected_candidate_id == "cand_fresh"
        assert res.selected_value == Decimal("95")

    # 13. Deterministic result
    def test_13_deterministic_result(self):
        service = ConflictResolutionService()
        policy = create_default_source_priority_policy(precedence=("src_a", "src_b", "src_c"))
        now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        c1 = ConflictCandidate(candidate_id="cand_1", source_id="src_b", record_id="r1", canonical_entity_id="p1", field_path="price", value=Decimal("100"), observed_at=now)
        c2 = ConflictCandidate(candidate_id="cand_2", source_id="src_a", record_id="r2", canonical_entity_id="p1", field_path="price", value=Decimal("110"), observed_at=now)

        res1 = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        res2 = service.resolve_conflict([c2, c1], policy=policy, evaluated_at=now)

        assert res1.conflict_id == res2.conflict_id
        assert res1.checksum == res2.checksum
        assert res1.selected_candidate_id == res2.selected_candidate_id == "cand_2"
        assert res1.selected_value == res2.selected_value == Decimal("110")

    # 14. Policy versioning
    def test_14_policy_versioning(self, tmp_path: Path):
        repo = JsonConflictResolutionPolicyRepository(tmp_path / "policies")
        p1 = ConflictResolutionPolicy(policy_id="pol_price", name="Price Policy v1", version="1.0.0", strategy=ResolutionStrategy.SOURCE_PRIORITY)
        p2 = ConflictResolutionPolicy(policy_id="pol_price", name="Price Policy v2", version="2.0.0", strategy=ResolutionStrategy.FRESHEST)

        repo.save_policy(p1)
        repo.save_policy(p2)

        loaded_v1 = repo.get_policy("pol_price", "1.0.0")
        loaded_v2 = repo.get_policy("pol_price", "2.0.0")
        loaded_latest = repo.get_policy("pol_price")

        assert loaded_v1.strategy == ResolutionStrategy.SOURCE_PRIORITY
        assert loaded_v2.strategy == ResolutionStrategy.FRESHEST
        assert loaded_latest.version == "2.0.0"

    # 15. Checksum & Tampering detection
    def test_15_checksum_and_tampering(self, tmp_path: Path):
        repo = JsonConflictResolutionRepository(tmp_path / "results")
        now = datetime.now(timezone.utc)
        result = ConflictResolutionResult(
            conflict_id="cnf_test_chk",
            canonical_entity_id="prod_1",
            field_path="price",
            candidate_ids=("c1", "c2"),
            strategy=ResolutionStrategy.SOURCE_PRIORITY,
            status=ConflictStatus.RESOLVED,
            reason_code=ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY,
            selected_candidate_id="c1",
            selected_value=Decimal("100"),
            policy_id="pol_1",
            policy_version="1.0.0",
            evaluated_at=now,
            correlation_id="corr_1",
        )
        repo.save_result(result)

        file_path = tmp_path / "results" / "result_cnf_test_chk.json"
        assert file_path.exists()

        # Tamper the file content
        content = file_path.read_text(encoding="utf-8")
        tampered = content.replace('"selected_value": "100"', '"selected_value": "999"')
        file_path.write_text(tampered, encoding="utf-8")

        with pytest.raises(CorruptedConflictResultError):
            repo.get_result("cnf_test_chk")

    # 16. Idempotency
    def test_16_idempotency(self, tmp_path: Path):
        repo = JsonConflictResolutionRepository(tmp_path / "results")
        now = datetime.now(timezone.utc)
        result = ConflictResolutionResult(
            conflict_id="cnf_idemp_1",
            canonical_entity_id="prod_1",
            field_path="price",
            candidate_ids=("c1", "c2"),
            strategy=ResolutionStrategy.SOURCE_PRIORITY,
            status=ConflictStatus.RESOLVED,
            reason_code=ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY,
            selected_candidate_id="c1",
            selected_value=Decimal("100"),
            policy_id="pol_1",
            policy_version="1.0.0",
            evaluated_at=now,
            correlation_id="corr_1",
        )

        res1 = repo.save_result(result)
        res2 = repo.save_result(result)
        assert res1.checksum == res2.checksum

    # 17. No evidence deletion
    def test_17_no_evidence_deletion(self):
        service = ConflictResolutionService()
        policy = create_default_source_priority_policy(precedence=("src_1", "src_2"))
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(candidate_id="cand_1", source_id="src_1", record_id="rec_1", canonical_entity_id="prod_1", field_path="price", value=Decimal("100"), observed_at=now)
        c2 = ConflictCandidate(candidate_id="cand_2", source_id="src_2", record_id="rec_2", canonical_entity_id="prod_1", field_path="price", value=Decimal("120"), observed_at=now)

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        # Todos los candidatos deben estar referenciados en el resultado
        assert len(res.candidate_ids) == 2
        assert "cand_1" in res.candidate_ids
        assert "cand_2" in res.candidate_ids
        # Los objetos candidatos permanecen intactos
        assert c1.value == Decimal("100")
        assert c2.value == Decimal("120")

    # 18. No hidden winner
    def test_18_no_hidden_winner(self):
        service = ConflictResolutionService()
        # Fuentes que no están en la precedencia
        policy = create_default_source_priority_policy(precedence=("src_x", "src_y"))
        now = datetime.now(timezone.utc)

        c1 = ConflictCandidate(candidate_id="cand_unknown_src_1", source_id="src_a", record_id="r1", canonical_entity_id="p1", field_path="stock", value=10, observed_at=now)
        c2 = ConflictCandidate(candidate_id="cand_unknown_src_2", source_id="src_b", record_id="r2", canonical_entity_id="p1", field_path="stock", value=20, observed_at=now)

        res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
        assert res.status == ConflictStatus.UNRESOLVED
        assert res.selected_candidate_id is None
        assert res.selected_value is None
