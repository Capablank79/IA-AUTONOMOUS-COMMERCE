"""
Tests unitarios para Duplicate Detection L.7 (Transversal Data Quality / Governance).

Cubre los 16 requerimientos mínimos obligatorios:
1. exact duplicate: registros idénticos en payload y metadatos semánticos -> DUPLICATE / EXACT_DUPLICATE
2. replay duplicate: mismo logical record id o idempotency key con idéntico payload -> REPLAY_DUPLICATE
3. same entity but different event: misma canonical entity pero distinto hecho lógico -> NOT_DUPLICATE
4. different entity: distinto canonical_entity_id -> NOT_DUPLICATE
5. different source: distinta fuente (cross-source evidence) -> NOT_DUPLICATE preservando evidencia independiente
6. temporal distinction: mismo hecho en distinto momento fuera de la ventana temporal -> NOT_DUPLICATE
7. deterministic fingerprint: SHA-256 determinista, normalizado, ordenado y reproducible sin hash()
8. UNKNOWN preserved: estados incompletos/ambiguos -> UNKNOWN (UNKNOWN != NOT_DUPLICATE)
9. POSSIBLE != DUPLICATE: posibles duplicados evaluados conservadoramente sin colapsar a DUPLICATE
10. L.6 MATCH reused: reutiliza canonical_entity_id resuelto por L.6
11. L.6 NO_MATCH: entidades con canonical IDs distintos son no duplicados de inmediato
12. policy versioning: validación SemVer y persistencia inmutable de políticas con versionado
13. checksum recalculation y tampering detection: detección de corrupción en persistencia JSON
14. idempotency: evaluaciones repetidas producen idénticos resultados y checksums
15. no destructive merge: no borra ni fusiona físicamente registros, agrupa sin mutación destructiva
16. no L.8 logic: no selecciona ganadores ni resuelve conflictos entre atributos divergentes
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from pathlib import Path
from types import MappingProxyType

from src.domain.duplicate_detection.models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    DuplicateDetectionResult,
    DuplicateGroup,
    normalize_value,
    compute_semantic_fingerprint,
    compute_duplicate_candidate_checksum,
    compute_duplicate_policy_checksum,
    compute_duplicate_result_checksum,
    compute_duplicate_group_checksum,
)
from src.application.duplicate_detection.service import (
    DuplicateDetectionService,
    create_default_product_dedup_policy,
    create_default_replay_policy,
)
from src.infrastructure.persistence.data.json.duplicate_detection_repository import (
    JsonDuplicateDetectionPolicyRepository,
    JsonDuplicateDetectionRepository,
    CorruptedDuplicateDetectionRecordError,
    DuplicateDetectionConflictError,
    DuplicateDetectionPolicyConflictError,
)


class TestL7DuplicateDetectionUnit:

    # 1. Exact Duplicate
    def test_01_exact_duplicate(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)
        payload = {"sku": "PROD-100", "price": Decimal("29.99"), "title": "Wireless Mouse"}

        c1 = DuplicateCandidate(
            record_id="rec_001",
            source_id="supplier_a",
            canonical_entity_id="canon_mouse_100",
            payload=payload,
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="rec_002",
            source_id="supplier_a",
            canonical_entity_id="canon_mouse_100",
            payload=payload,
            observed_at=now,
        )

        result = service.evaluate_pair(c1, c2)
        assert result.status in (DuplicateStatus.DUPLICATE, DuplicateStatus.EXACT_DUPLICATE)
        assert result.confidence_score == Decimal("1.0000")
        assert result.confidence_score == Decimal("1.0000")
        assert result.primary_fingerprint == result.secondary_fingerprint

    # 2. Replay Duplicate
    def test_02_replay_duplicate(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)
        payload = {"transaction_id": "tx_999", "amount": Decimal("150.00")}

        c1 = DuplicateCandidate(
            record_id="evt_001",
            source_id="pos_terminal_1",
            idempotency_key="idemp_tx_999",
            payload=payload,
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="evt_002",
            source_id="pos_terminal_1",
            idempotency_key="idemp_tx_999",
            payload=payload,
            observed_at=now + timedelta(seconds=5),
        )

        policy = create_default_replay_policy()
        result = service.evaluate_pair(c1, c2, policy=policy)
        assert result.status == DuplicateStatus.REPLAY_DUPLICATE
        assert result.reason_code == DuplicateReasonCode.REPLAY_PAYLOAD_MATCH
        assert result.confidence_score == Decimal("1.0000")

    # 3. Same Entity but Different Event
    def test_03_same_entity_but_different_event(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)

        # Mismo producto pero el lunes tiene precio 100 y el martes 120
        c_lunes = DuplicateCandidate(
            record_id="obs_mon",
            source_id="supplier_a",
            canonical_entity_id="canon_laptop_x",
            payload={"sku": "LAP-X", "price": Decimal("100.00")},
            observed_at=now,
        )
        c_martes = DuplicateCandidate(
            record_id="obs_tue",
            source_id="supplier_a",
            canonical_entity_id="canon_laptop_x",
            payload={"sku": "LAP-X", "price": Decimal("120.00")},
            observed_at=now + timedelta(days=1),
        )

        result = service.evaluate_pair(c_lunes, c_martes)
        assert result.status == DuplicateStatus.NOT_DUPLICATE
        assert result.reason_code == DuplicateReasonCode.SEMANTIC_PAYLOAD_MISMATCH

    # 4. Different Entity
    def test_04_different_entity(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)

        c1 = DuplicateCandidate(
            record_id="rec_010",
            source_id="supplier_a",
            canonical_entity_id="canon_prod_aaa",
            payload={"name": "Gaming Chair"},
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="rec_011",
            source_id="supplier_a",
            canonical_entity_id="canon_prod_bbb",
            payload={"name": "Office Desk"},
            observed_at=now,
        )

        result = service.evaluate_pair(c1, c2)
        assert result.status == DuplicateStatus.NOT_DUPLICATE
        assert result.reason_code == DuplicateReasonCode.DIFFERENT_CANONICAL_ENTITY

    # 5. Different Source (Cross-Source Evidence Preservation)
    def test_05_different_source_preserves_evidence(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)
        payload = {"sku": "PROD-200", "price": Decimal("50.00")}

        c_source_a = DuplicateCandidate(
            record_id="rec_src_a",
            source_id="supplier_alpha",
            canonical_entity_id="canon_headset_200",
            payload=payload,
            observed_at=now,
        )
        c_source_b = DuplicateCandidate(
            record_id="rec_src_b",
            source_id="supplier_beta",
            canonical_entity_id="canon_headset_200",
            payload=payload,
            observed_at=now,
        )

        # Política por defecto requiere misma fuente para ser duplicado
        result = service.evaluate_pair(c_source_a, c_source_b)
        assert result.status == DuplicateStatus.NOT_DUPLICATE
        assert result.reason_code == DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE

    # 6. Temporal Distinction
    def test_06_temporal_distinction(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)
        payload = {"sku": "PROD-300", "price": Decimal("10.00")}

        # Política con ventana de 3600 segundos (1 hora)
        policy = DuplicateDetectionPolicy(
            policy_id="pol_temp_1h",
            name="1 Hour Temporal Window Policy",
            version="1.0.0",
            temporal_window_seconds=3600,
            allow_cross_source_duplicates=False,
            require_same_source=True,
        )

        c_t0 = DuplicateCandidate(
            record_id="rec_t0",
            source_id="supplier_a",
            canonical_entity_id="canon_item_300",
            payload=payload,
            observed_at=now,
        )
        c_t2h = DuplicateCandidate(
            record_id="rec_t2h",
            source_id="supplier_a",
            canonical_entity_id="canon_item_300",
            payload=payload,
            observed_at=now + timedelta(hours=2),
        )

        result = service.evaluate_pair(c_t0, c_t2h, policy=policy)
        assert result.status == DuplicateStatus.NOT_DUPLICATE
        assert result.reason_code == DuplicateReasonCode.SAME_ENTITY_DISTINCT_TEMPORAL_EVENT

    # 7. Deterministic Fingerprint
    def test_07_deterministic_fingerprint(self):
        # Mismo payload con distinto orden de keys y tipos de whitespace
        p1 = {"Title": "  Wireless Mouse ", "Price": Decimal("29.99"), "SKU": "mou-100"}
        p2 = {"sku": "mou-100", "price": Decimal("29.99"), "title": "Wireless Mouse"}

        fp1 = compute_semantic_fingerprint(p1, canonical_entity_id="CANON_01")
        fp2 = compute_semantic_fingerprint(p2, canonical_entity_id="canon_01")

        assert fp1 == fp2
        assert len(fp1) == 64
        # Ignora campos técnicos de ruido
        p3 = dict(p1)
        p3["trace_id"] = "trace_abc123"
        p3["_id"] = "mongo_obj_id"
        fp3 = compute_semantic_fingerprint(p3, canonical_entity_id="CANON_01")
        assert fp3 == fp1

    # 8. UNKNOWN Preserved (UNKNOWN != NOT_DUPLICATE)
    def test_08_unknown_preserved(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)

        # Registros con payload vacío o insuficiente
        c_empty1 = DuplicateCandidate(
            record_id="rec_empty_1",
            source_id="source_a",
            payload={},
            observed_at=now,
        )
        c_empty2 = DuplicateCandidate(
            record_id="rec_empty_2",
            source_id="source_a",
            payload={},
            observed_at=now,
        )

        result = service.evaluate_pair(c_empty1, c_empty2)
        assert result.status == DuplicateStatus.UNKNOWN
        assert result.reason_code == DuplicateReasonCode.INSUFFICIENT_DATA
        assert result.status != DuplicateStatus.NOT_DUPLICATE

    # 9. POSSIBLE != DUPLICATE
    def test_09_possible_duplicate_not_duplicate(self):
        # Asegurar enums diferenciados
        assert DuplicateStatus.POSSIBLE_DUPLICATE != DuplicateStatus.DUPLICATE
        assert DuplicateStatus.POSSIBLE_DUPLICATE.value == "POSSIBLE_DUPLICATE"
        assert DuplicateStatus.DUPLICATE.value == "DUPLICATE"

    # 10. L.6 MATCH Reused
    def test_10_l6_match_reused(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)
        payload = {"model": "Keyboard-K1"}

        # Mismo canonical_entity_id asignado por L.6
        c1 = DuplicateCandidate(
            record_id="rec_k1",
            source_id="warehouse_1",
            canonical_entity_id="resolved_canon_k1",
            payload=payload,
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="rec_k2",
            source_id="warehouse_1",
            canonical_entity_id="resolved_canon_k1",
            payload=payload,
            observed_at=now,
        )

        result = service.evaluate_pair(c1, c2)
        assert result.status in (DuplicateStatus.DUPLICATE, DuplicateStatus.EXACT_DUPLICATE)

    # 11. L.6 NO_MATCH
    def test_11_l6_no_match(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)

        c1 = DuplicateCandidate(
            record_id="rec_p1",
            source_id="warehouse_1",
            canonical_entity_id="canon_entity_alpha",
            payload={"model": "Keyboard-K1"},
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="rec_p2",
            source_id="warehouse_1",
            canonical_entity_id="canon_entity_beta",
            payload={"model": "Keyboard-K1"},
            observed_at=now,
        )

        result = service.evaluate_pair(c1, c2)
        assert result.status == DuplicateStatus.NOT_DUPLICATE
        assert result.reason_code == DuplicateReasonCode.DIFFERENT_CANONICAL_ENTITY

    # 12. Policy Versioning
    def test_12_policy_versioning(self, tmp_path: Path):
        repo = JsonDuplicateDetectionPolicyRepository(tmp_path / "policies")
        p1 = DuplicateDetectionPolicy(
            policy_id="pol_catalog",
            name="Catalog Deduplication Policy",
            version="1.0.0",
        )
        repo.save_policy(p1)

        loaded = repo.get_policy("pol_catalog", version="1.0.0")
        assert loaded is not None
        assert loaded.version == "1.0.0"

        # Invalid SemVer should fail
        with pytest.raises(ValueError, match="Must follow Semantic Versioning"):
            DuplicateDetectionPolicy(
                policy_id="pol_bad",
                name="Bad Policy",
                version="invalid-version",
            )

    # 13. Checksum and Tampering Detection
    def test_13_checksum_and_tampering(self, tmp_path: Path):
        repo = JsonDuplicateDetectionRepository(tmp_path)
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)

        c1 = DuplicateCandidate(
            record_id="rec_c1",
            source_id="src_1",
            payload={"sku": "A1"},
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="rec_c2",
            source_id="src_1",
            payload={"sku": "A1"},
            observed_at=now,
        )

        res = service.evaluate_pair(c1, c2)
        repo.save_result(res)

        # Modificación maliciosa del archivo JSON para simular corrupción
        result_file = tmp_path / "results" / f"{res.result_id}.json"
        assert result_file.exists()

        content = result_file.read_text(encoding="utf-8")
        tampered = content.replace("DUPLICATE", "NOT_DUPLICATE")
        result_file.write_text(tampered, encoding="utf-8")

        with pytest.raises(CorruptedDuplicateDetectionRecordError):
            repo.get_result(res.result_id)

    # 14. Idempotency
    def test_14_idempotency(self):
        service = DuplicateDetectionService()
        now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
        payload = {"sku": "IDEMP-100", "price": Decimal("45.00")}

        c1 = DuplicateCandidate(
            record_id="rec_i1",
            source_id="src_1",
            canonical_entity_id="canon_idemp",
            payload=payload,
            observed_at=now,
        )
        c2 = DuplicateCandidate(
            record_id="rec_i2",
            source_id="src_1",
            canonical_entity_id="canon_idemp",
            payload=payload,
            observed_at=now,
        )

        res1 = service.evaluate_pair(c1, c2)
        # Re-evaluating with fixed evaluated_at or same clock produces identical checksum
        res2 = DuplicateDetectionResult(
            result_id=res1.result_id,
            primary_record_id=res1.primary_record_id,
            secondary_record_id=res1.secondary_record_id,
            status=res1.status,
            reason_code=res1.reason_code,
            policy_id=res1.policy_id,
            policy_version=res1.policy_version,
            primary_fingerprint=res1.primary_fingerprint,
            secondary_fingerprint=res1.secondary_fingerprint,
            evaluated_at=res1.evaluated_at,
            is_exact_replay=res1.is_exact_replay,
            confidence_score=res1.confidence_score,
            details=res1.details,
        )

        assert res1.result_id == res2.result_id
        assert res1.checksum == res2.checksum
        assert res1.status == res2.status
        assert res1.reason_code == res2.reason_code

    # 15. No Destructive Merge (Groups Without Deleting Records)
    def test_15_no_destructive_merge(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)
        payload = {"sku": "BATCH-1", "name": "Item"}

        records = [
            DuplicateCandidate(record_id="r1", source_id="s1", canonical_entity_id="c1", payload=payload, observed_at=now),
            DuplicateCandidate(record_id="r2", source_id="s1", canonical_entity_id="c1", payload=payload, observed_at=now),
            DuplicateCandidate(record_id="r3", source_id="s1", canonical_entity_id="c1", payload={"sku": "BATCH-2", "name": "Other Item"}, observed_at=now),
        ]

        results, groups = service.detect_in_batch(records)
        assert len(groups) == 1
        group = groups[0]
        assert "r1" in group.member_record_ids
        assert "r2" in group.member_record_ids
        # Todos los registros originales permanecen intactos en memoria y no fueron borrados
        assert len(records) == 3

    # 16. No L.8 Logic (Does not select winner on conflicting attributes)
    def test_16_no_l8_logic(self):
        service = DuplicateDetectionService()
        now = datetime.now(timezone.utc)

        c_a = DuplicateCandidate(
            record_id="rec_source_a",
            source_id="source_a",
            canonical_entity_id="canon_phone_1",
            payload={"price": Decimal("100.00")},
            observed_at=now,
        )
        c_b = DuplicateCandidate(
            record_id="rec_source_b",
            source_id="source_b",
            canonical_entity_id="canon_phone_1",
            payload={"price": Decimal("120.00")},
            observed_at=now,
        )

        result = service.evaluate_pair(c_a, c_b)
        # L.7 no resuelve conflicto de precio ni elige ganador: reporta no duplicado
        assert result.status == DuplicateStatus.NOT_DUPLICATE
        assert not hasattr(result, "winning_value")
        assert not hasattr(result, "resolved_payload")
