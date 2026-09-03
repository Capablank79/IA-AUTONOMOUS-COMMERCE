"""
Tests unitarios para Entity Resolution L.6 (Transversal Data Quality / Governance).

Cubre los 22 requerimientos obligatorios:
1. immutable references/results/policy (frozen=True, mapping proxy, immutability)
2. deterministic normalization (Unicode NFKC, trim, lowercase, punctuation)
3. exact strong ID match (GTIN, EAN, UPC, ISBN, MPN) -> MATCH
4. strong ID mismatch -> NO_MATCH absoluto
5. source-scoped SKU (mismo SKU en misma fuente) -> MATCH si la política lo permite o scoped match
6. same SKU different namespace/source -> SCOPED_IDENTIFIER_CROSS_NAMESPACE (no MATCH)
7. attribute exact match -> MATCH o POSSIBLE_MATCH según pesos y umbrales
8. partial attributes -> POSSIBLE_MATCH / UNKNOWN
9. missing attributes -> penalización determinista de score
10. ambiguity detection (múltiples candidatos compatibles) -> POSSIBLE_MATCH
11. UNKNOWN preserved (UNKNOWN != NO_MATCH)
12. POSSIBLE_MATCH != MATCH
13. Decimal scoring (sin float, aritmética exacta con Decimal)
14. stable canonical entity id (determinista y reproducible)
15. replay determinism (mismos inputs producen idéntico resultado y checksum)
16. policy versioning y validación SemVer
17. checksum recalculation y tampering detection
18. conflict detection en persistencia (mismo ID/versión con distinto contenido)
19. invalid schema input (integración con L.5 -> ERROR/UNKNOWN, nunca MATCH)
20. secret sanitization en metadata y atributos (K.8)
21. no duplicate detection logic (L.6 solo evalúa pares/candidatos dados, no itera datasets globales)
22. no conflict resolution logic (L.6 no selecciona campos ganadores ante discrepancias)
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from pathlib import Path
from types import MappingProxyType

from src.domain.entity_resolution.models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    ResolutionReasonCode,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
    EntityResolutionResult,
    ResolvedEntity,
    normalize_text,
    normalize_identifier_value,
    build_deterministic_canonical_entity_id,
    compute_entity_reference_checksum,
    compute_resolution_policy_checksum,
    compute_resolution_result_checksum,
    compute_resolution_input_fingerprint,
    compute_resolved_entity_checksum,
)
from src.application.entity_resolution.service import (
    EntityResolutionService,
    create_default_product_policy,
)
from src.infrastructure.persistence.data.json.entity_resolution_repository import (
    JsonEntityResolutionPolicyRepository,
    JsonEntityResolutionRepository,
    EntityResolutionConflictError,
    EntityResolutionPolicyConflictError,
    CorruptedResolutionPolicyError,
    CorruptedResolutionResultError,
    CorruptedCanonicalEntityError,
)


class DummyAuditService:
    def __init__(self):
        self.events = []

    def record_event(self, event_type: str, payload: dict, **kwargs):
        self.events.append((event_type, payload))


class TestL6EntityResolutionUnit:
    """Suite de pruebas unitarias exhaustivas para L.6 Entity Resolution."""

    def test_01_immutability_models(self):
        """1. Inmutabilidad estricta de referencias, resultados y políticas."""
        policy = create_default_product_policy()
        with pytest.raises(Exception):
            policy.version = "2.0.0"

        identifier = EntityIdentifier(
            identifier_type=IdentifierType.GTIN,
            value="01234567890123",
            is_strong=True,
        )
        with pytest.raises(Exception):
            identifier.value = "999"

        ref = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_a",
            source_entity_id="p100",
            identifiers=(identifier,),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )
        with pytest.raises(Exception):
            ref.source_id = "supplier_b"
        with pytest.raises(Exception):
            ref.canonical_attributes["brand"] = "Other"

        assert isinstance(ref.canonical_attributes, MappingProxyType)
        assert isinstance(ref.identifiers, tuple)

    def test_02_deterministic_normalization(self):
        """2. Normalización determinista y reproducible de texto e identificadores."""
        # Unicode, mayúsculas, espacios múltiples y trim
        raw_text = "   LÓGITECH   \u00A0  mx-master   3s  \t "
        normalized = normalize_text(raw_text)
        assert normalized == "lógitech mx-master 3s"

        # Puntuación y guiones en identificadores de código de barra
        raw_gtin = " 0-123.456/789 0123 \n"
        norm_gtin = normalize_identifier_value(IdentifierType.GTIN, raw_gtin)
        assert norm_gtin == "01234567890123"

        # Identificadores de texto como MPN preservan guiones y mayúsculas
        raw_mpn = "  mpn-990-a / b  "
        norm_mpn = normalize_identifier_value(IdentifierType.MPN, raw_mpn)
        assert norm_mpn == "mpn-990-a / b"

    def test_03_exact_strong_id_match(self):
        """3. Coincidencia exacta de strong identifier (GTIN) produce MATCH indiscutible."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_a",
            source_entity_id="sup_001",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.GTIN,
                    value="01234567890123",
                    is_strong=True,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="marketplace_meli",
            source_entity_id="MLA987654321",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.GTIN,
                    value="01234567890123",
                    is_strong=True,
                ),
            ),
            canonical_attributes={"title": "Mouse Logitech MX Master 3S Wireless"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)

        assert result.status == MatchStatus.MATCH
        assert result.confidence_score == Decimal("1.0000")
        assert ResolutionReasonCode.EXACT_STRONG_IDENTIFIER_MATCH.value in result.reason_codes
        assert "GTIN:01234567890123" in result.matched_identifiers
        assert result.canonical_entity_id.startswith("canonical_product_gtin_01234567890123")

    def test_04_strong_id_mismatch_forces_no_match(self):
        """4. Strong identifier en conflicto fuerza NO_MATCH absoluto aunque el título sea idéntico."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_a",
            source_entity_id="sup_001",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.GTIN,
                    value="01234567890123",
                    is_strong=True,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="marketplace_meli",
            source_entity_id="MLA987654321",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.GTIN,
                    value="99999999999999",
                    is_strong=True,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)

        assert result.status == MatchStatus.NO_MATCH
        assert result.confidence_score == Decimal("0.0000")
        assert ResolutionReasonCode.CONTRADICTORY_STRONG_IDENTIFIERS.value in result.reason_codes
        assert len(result.mismatched_identifiers) > 0

    def test_05_source_scoped_sku_same_namespace(self):
        """5. SKU con mismo scope/namespace es MATCH si la política lo declara strong identifier."""
        service = EntityResolutionService()
        # Política que declara SKU explícitamente como strong identifier (scoped).
        # Regla §6: same namespace + exact SKU -> MATCH salvo contradicción más fuerte.
        policy = EntityResolutionPolicy(
            policy_id="sku_strong_policy",
            name="SKU Strong Policy",
            version="1.0.0",
            entity_type=EntityType.PRODUCT,
            strong_identifier_types=(IdentifierType.SKU,),
            required_attributes=("brand", "model"),
            attribute_weights={"brand": Decimal("0.35"), "model": Decimal("0.35")},
            match_threshold=Decimal("0.85"),
            possible_match_threshold=Decimal("0.50"),
            allow_cross_source_sku_match=False,
        )

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_alpha",
            source_entity_id="prod_1",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.SKU,
                    value="SKU-ABC-99",
                    namespace="supplier_alpha",
                    is_strong=True,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_alpha",
            source_entity_id="prod_1_alias",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.SKU,
                    value="SKU-ABC-99",
                    namespace="supplier_alpha",
                    is_strong=True,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert result.status == MatchStatus.MATCH
        assert ResolutionReasonCode.EXACT_STRONG_IDENTIFIER_MATCH.value in result.reason_codes

    def test_05b_sku_same_namespace_default_policy_is_not_match(self):
        """5b. Con la policy default, SKU NO es strong: mismo SKU en mismo namespace no basta para MATCH."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_alpha",
            source_entity_id="prod_1",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.SKU,
                    value="SKU-ABC-99",
                    namespace="supplier_alpha",
                    is_strong=False,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_alpha",
            source_entity_id="prod_1_alias",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.SKU,
                    value="SKU-ABC-99",
                    namespace="supplier_alpha",
                    is_strong=False,
                ),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        # Default policy NO declara SKU fuerte y NO permite auto-match por atributos
        # -> misma SKU en mismo namespace es POSSIBLE_MATCH, nunca MATCH automático.
        assert result.status == MatchStatus.POSSIBLE_MATCH
        assert ResolutionReasonCode.SCOPED_IDENTIFIER_MATCH.value in result.reason_codes

    def test_06_same_sku_different_namespace_no_match(self):
        """6. Mismo SKU bajo diferentes fuentes/namespaces no produce MATCH por colisión accidental."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_alpha",
            source_entity_id="p1",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.SKU,
                    value="SKU-100",
                    namespace="supplier_alpha",
                ),
            ),
            canonical_attributes={"brand": "BrandA"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="supplier_beta",
            source_entity_id="p2",
            identifiers=(
                EntityIdentifier(
                    identifier_type=IdentifierType.SKU,
                    value="SKU-100",
                    namespace="supplier_beta",
                ),
            ),
            canonical_attributes={"brand": "BrandB"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        # Diferentes fuentes y brands diferentes -> NO_MATCH
        assert result.status in (MatchStatus.NO_MATCH, MatchStatus.POSSIBLE_MATCH, MatchStatus.UNKNOWN)
        assert result.status != MatchStatus.MATCH
        assert ResolutionReasonCode.SCOPED_IDENTIFIER_CROSS_NAMESPACE.value in result.reason_codes

    def test_07_attribute_exact_match_score(self):
        """7. Emparejamiento por atributos ponderados exactos."""
        service = EntityResolutionService()
        # Política que permite auto-match por atributos con score alto
        policy = EntityResolutionPolicy(
            policy_id="attr_policy",
            name="Attribute Match Policy",
            version="1.0.0",
            entity_type=EntityType.PRODUCT,
            required_attributes=("brand", "model"),
            optional_attributes=("variant", "color"),
            attribute_weights={
                "brand": Decimal("0.50"),
                "model": Decimal("0.50"),
            },
            match_threshold=Decimal("0.90"),
            possible_match_threshold=Decimal("0.50"),
            allow_attribute_only_auto_match=True,
        )

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="source_a",
            source_entity_id="p1",
            canonical_attributes={"brand": "Sony", "model": "WH-1000XM5"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="source_b",
            source_entity_id="p2",
            canonical_attributes={"brand": "Sony", "model": "wh-1000xm5"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert result.status == MatchStatus.MATCH
        assert result.confidence_score == Decimal("1.0000")
        assert ResolutionReasonCode.ATTRIBUTE_HIGH_CONFIDENCE_MATCH.value in result.reason_codes

    def test_08_partial_attributes_possible_match(self):
        """8. Coincidencia parcial de atributos genera POSSIBLE_MATCH."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S", "color": "Graphite"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S", "color": "Pale Gray"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        # default policy has allow_attribute_only_auto_match=False -> POSSIBLE_MATCH
        assert result.status == MatchStatus.POSSIBLE_MATCH
        assert result.confidence_score >= Decimal("0.70")
        assert ResolutionReasonCode.ATTRIBUTE_PARTIAL_MATCH.value in result.reason_codes

    def test_09_missing_attributes_score_penalization(self):
        """9. Atributos requeridos ausentes penalizan deterministamente el score."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            canonical_attributes={"brand": "Logitech"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            canonical_attributes={"brand": "Logitech"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        # Falta "model" que tiene peso 0.35 -> score solo alcanza 0.35 / 0.70 ponderado o inferior
        assert result.status in (MatchStatus.UNKNOWN, MatchStatus.POSSIBLE_MATCH)
        assert "model" in result.missing_attributes
        assert result.status != MatchStatus.MATCH

    def test_10_ambiguity_detection_in_candidates(self):
        """10. Preservación de ambigüedad cuando múltiples candidatos son compatibles."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        target = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="target_src",
            source_entity_id="t1",
            canonical_attributes={"brand": "Apple", "model": "iPhone 15"},
        )
        candidate_1 = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="cand_1",
            source_entity_id="c1",
            canonical_attributes={"brand": "Apple", "model": "iPhone 15", "color": "Black"},
        )
        candidate_2 = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="cand_2",
            source_entity_id="c2",
            canonical_attributes={"brand": "Apple", "model": "iPhone 15", "color": "Blue"},
        )

        cand_results = service.resolve_candidates(target, [candidate_1, candidate_2], policy=policy)
        assert len(cand_results) == 2
        # Ambos son posibles pero ninguno debe auto-fusionarse
        for cand_res in cand_results:
            assert cand_res.status in (MatchStatus.POSSIBLE_MATCH, MatchStatus.UNKNOWN)
            assert cand_res.status != MatchStatus.MATCH

    def test_11_unknown_preserved_vs_no_match(self):
        """11. Semántica UNKNOWN != NO_MATCH por falta de evidencia."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            canonical_attributes={},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            canonical_attributes={},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert result.status == MatchStatus.UNKNOWN
        assert result.status != MatchStatus.NO_MATCH
        assert ResolutionReasonCode.INSUFFICIENT_EVIDENCE.value in result.reason_codes

    def test_12_possible_match_is_not_match(self):
        """12. Semántica POSSIBLE_MATCH != MATCH."""
        assert MatchStatus.POSSIBLE_MATCH != MatchStatus.MATCH
        assert MatchStatus.POSSIBLE_MATCH.value != "MATCH"

    def test_13_decimal_scoring_precision(self):
        """13. Scoring exacto con Decimal, sin errores de punto flotante."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            canonical_attributes={"brand": "Samsung", "model": "Galaxy S24", "title": "Samsung Galaxy S24 256GB"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            canonical_attributes={"brand": "Samsung", "model": "Galaxy S24", "title": "Smartphone Samsung S24"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert isinstance(result.confidence_score, Decimal)
        # Decimal score 0.0000 a 1.0000
        assert Decimal("0.0000") <= result.confidence_score <= Decimal("1.0000")

    def test_14_stable_canonical_entity_id(self):
        """14. Generación determinista y estable del canonical_entity_id."""
        gtin_a = EntityIdentifier(identifier_type=IdentifierType.GTIN, value="01234567890123", is_strong=True)
        gtin_b = EntityIdentifier(identifier_type=IdentifierType.GTIN, value="01234567890123", is_strong=True)

        id_1 = build_deterministic_canonical_entity_id(
            EntityType.PRODUCT,
            identifiers=(gtin_a,),
        )
        id_2 = build_deterministic_canonical_entity_id(
            EntityType.PRODUCT,
            identifiers=(gtin_b,),
        )
        assert id_1 == id_2
        assert id_1.startswith("canonical_product_gtin_01234567890123")

        # Sin strong identifiers, determinista por identificadores ordenados
        sku_p1 = EntityIdentifier(identifier_type=IdentifierType.SKU, value="P1", namespace="src_a")
        sku_p2 = EntityIdentifier(identifier_type=IdentifierType.SKU, value="P2", namespace="src_b")
        cluster_id_1 = build_deterministic_canonical_entity_id(
            EntityType.PRODUCT,
            identifiers=(sku_p1, sku_p2),
        )
        cluster_id_2 = build_deterministic_canonical_entity_id(
            EntityType.PRODUCT,
            identifiers=(sku_p2, sku_p1),  # orden inverso
        )
        assert cluster_id_1 == cluster_id_2
        assert cluster_id_1.startswith("canonical_product_idcluster_")

    def test_15_replay_determinism(self):
        """15. Replay de resolución produce exactamente los mismos checksums y resultados."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7791234567890", is_strong=True),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7791234567890", is_strong=True),
            ),
            canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
        )

        res_1 = service.resolve_pair(ref_a, ref_b, policy=policy, correlation_id="replay-test")
        res_2 = service.resolve_pair(ref_a, ref_b, policy=policy, correlation_id="replay-test")

        assert res_1.resolution_id == res_2.resolution_id
        assert res_1.input_fingerprint == res_2.input_fingerprint
        assert res_1.canonical_entity_id == res_2.canonical_entity_id
        assert res_1.status == res_2.status

    def test_16_policy_versioning(self):
        """16. Versionado semántico y validación de políticas."""
        with pytest.raises(ValueError, match=r"(?i)semver|semantic versioning"):
            EntityResolutionPolicy(
                policy_id="invalid_policy",
                name="Invalid Policy",
                version="v1_beta",
                entity_type=EntityType.PRODUCT,
            )

        valid_policy = EntityResolutionPolicy(
            policy_id="valid_policy",
            name="Valid Policy",
            version="1.2.3",
            entity_type=EntityType.PRODUCT,
        )
        assert valid_policy.version == "1.2.3"
        assert len(compute_resolution_policy_checksum(valid_policy)) == 64

    def test_17_checksum_integrity_and_tamper_detection(self, tmp_path):
        """17. Verificación estricta de checksum y detección de corrupción/tampering."""
        repo = JsonEntityResolutionRepository(tmp_path / "entity_repo")
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="1111111111111", is_strong=True),
            ),
            canonical_attributes={"brand": "BrandX"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="1111111111111", is_strong=True),
            ),
            canonical_attributes={"brand": "BrandX"},
        )

        service = EntityResolutionService(repository=repo)
        result = service.resolve_pair(ref_a, ref_b, policy=policy, persist=True)

        # Cargar ok
        loaded = repo.get_resolution(result.resolution_id)
        assert loaded is not None
        assert loaded.resolution_id == result.resolution_id

        # Alterar archivo manualmente (private path helper como white-box controlado)
        file_path = repo._get_resolution_path(result.resolution_id)
        with open(file_path, "r", encoding="utf-8") as f:
            data = f.read()

        # Inyectar alteración
        tampered_data = data.replace('"MATCH"', '"NO_MATCH"')
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(tampered_data)

        # Debe lanzar CorruptedResolutionResultError
        with pytest.raises(CorruptedResolutionResultError):
            repo.get_resolution(result.resolution_id)

    def test_18_conflict_detection_on_different_payload(self, tmp_path):
        """18. Conflicto explícito si se intenta guardar contenido diferente bajo el mismo resolution_id."""
        repo = JsonEntityResolutionRepository(tmp_path / "entity_repo")
        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
        )

        res_1 = EntityResolutionResult(
            resolution_id="res_same_id",
            entity_type=EntityType.PRODUCT,
            reference_a=ref_a,
            reference_b=ref_b,
            status=MatchStatus.MATCH,
            confidence_score=Decimal("1.0000"),
        )
        repo.save_resolution(res_1)

        # Idempotencia con mismo contenido -> OK
        repo.save_resolution(res_1)

        # Conflicto con diferente status
        res_conflicting = EntityResolutionResult(
            resolution_id="res_same_id",
            entity_type=EntityType.PRODUCT,
            reference_a=ref_a,
            reference_b=ref_b,
            status=MatchStatus.NO_MATCH,
            confidence_score=Decimal("0.0000"),
        )
        with pytest.raises(EntityResolutionConflictError):
            repo.save_resolution(res_conflicting)

    def test_19_invalid_schema_input_fails_or_unknown(self):
        """19. Integración con L.5: SchemaValidation fallido produce ERROR/UNKNOWN, nunca MATCH."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            schema_validation_status="FAIL",  # L.5 schema validation fallido
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="01234567890123", is_strong=True),
            ),
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            schema_validation_status="PASS",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="01234567890123", is_strong=True),
            ),
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert result.status == MatchStatus.ERROR
        assert ResolutionReasonCode.SCHEMA_VALIDATION_FAILED.value in result.reason_codes

    def test_20_secret_sanitization_in_metadata(self):
        """20. Sanitización estricta de secretos en metadata (K.8)."""
        ref = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            metadata={"api_key": "secret_abc_123", "public_info": "safe"},
        )
        assert ref.metadata["api_key"] == "[REDACTED]"
        assert ref.metadata["public_info"] == "safe"

    def test_21_no_duplicate_detection_logic(self):
        """21. L.6 no contiene métodos ni lógica de recorrido de datasets completos (L.7)."""
        service = EntityResolutionService()
        assert not hasattr(service, "detect_duplicates_in_dataset")
        assert not hasattr(service, "find_all_duplicate_groups")
        assert not hasattr(service, "deduplicate_dataset")

    def test_22_no_conflict_resolution_logic(self):
        """22. L.6 no contiene métodos ni lógica de arbitraje de valores comerciales discrepantes (L.8)."""
        service = EntityResolutionService()
        assert not hasattr(service, "resolve_field_conflict")
        assert not hasattr(service, "merge_record_values")
        assert not hasattr(service, "select_winning_field_value")

    # ------------------------------------------------------------------
    # Hardening dirigido adicional (prompt §12)
    # ------------------------------------------------------------------

    def test_23_replay_idempotent_across_clock_instants(self, tmp_path):
        """1. Replay del mismo logical input a distintos instantes de reloj -> idempotente (sin conflicto)."""
        repo = JsonEntityResolutionRepository(tmp_path / "entity_repo")
        service = EntityResolutionService(repository=repo)
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="4012345678901", is_strong=True),
            ),
            canonical_attributes={"brand": "BrandA", "model": "ModelA"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="4012345678901", is_strong=True),
            ),
            canonical_attributes={"brand": "BrandA", "model": "ModelA"},
        )

        res_1 = service.resolve_pair(ref_a, ref_b, policy=policy, persist=True)
        # Forzar otro instante de reloj y hacer replay del mismo logical input.
        res_2 = service.resolve_pair(ref_a, ref_b, policy=policy, persist=True)

        assert res_1.resolution_id == res_2.resolution_id
        # Replay idempotente: la fingerprint lógica no depende de resolved_at.
        assert res_1.input_fingerprint == res_2.input_fingerprint
        stored = repo.get_resolution(res_1.resolution_id)
        assert stored is not None
        assert stored.input_fingerprint == res_1.input_fingerprint

    def test_24_canonical_entity_id_identical_across_replay(self):
        """2. canonical_entity_id idéntico en replay del mismo logical match."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="5012345678901", is_strong=True),
            ),
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            identifiers=(
                EntityIdentifier(identifier_type=IdentifierType.GTIN, value="5012345678901", is_strong=True),
            ),
        )

        res_1 = service.resolve_pair(ref_a, ref_b, policy=policy)
        res_2 = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert res_1.canonical_entity_id == res_2.canonical_entity_id
        assert res_1.canonical_entity_id is not None
        assert res_1.canonical_entity_id == build_deterministic_canonical_entity_id(
            EntityType.PRODUCT,
            identifiers=ref_a.identifiers + ref_b.identifiers,
            attributes=ref_a.canonical_attributes,
        )

    def test_25_gtin_separator_normalization_deterministic(self):
        """3. GTIN normalization determinista con separadores decorativos equiparables."""
        assert normalize_identifier_value(IdentifierType.GTIN, "123/456") == "123456"
        assert normalize_identifier_value(IdentifierType.GTIN, "123456") == "123456"
        assert normalize_identifier_value(IdentifierType.GTIN, " 0-123.456/789 0123 \n") == "01234567890123"
        assert normalize_identifier_value(IdentifierType.EAN, "5-900-1234567-8") == "590012345678"

    def test_26_sku_normalization_conservative(self):
        """4. SKU normalization conservadora: NO elimina '/' ni '-' (posiblemente semánticos)."""
        assert normalize_identifier_value(IdentifierType.SKU, "AB/12") == "ab/12"
        assert normalize_identifier_value(IdentifierType.SKU, "  MPN-990-A / B  ") == "mpn-990-a / b"

    def test_27_sku_same_namespace_policy_governed(self):
        """5. Mismo SKU mismo namespace: resultado gobernado por la policy (fuerte vs default)."""
        service = EntityResolutionService()
        default_policy = create_default_product_policy()
        strong_policy = EntityResolutionPolicy(
            policy_id="sku_strong_2",
            name="SKU Strong Policy 2",
            version="1.0.0",
            entity_type=EntityType.PRODUCT,
            strong_identifier_types=(IdentifierType.SKU,),
            allow_cross_source_sku_match=False,
        )

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.SKU, value="S1", namespace="src_a"),),
            canonical_attributes={"brand": "B", "model": "M"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p2",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.SKU, value="S1", namespace="src_a"),),
            canonical_attributes={"brand": "B", "model": "M"},
        )

        res_default = service.resolve_pair(ref_a, ref_b, policy=default_policy)
        # Default: SKU no es strong -> NO MATCH automático (POSSIBLE_MATCH).
        assert res_default.status != MatchStatus.MATCH

        ref_a_strong = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.SKU, value="S1", namespace="src_a", is_strong=True),),
            canonical_attributes={"brand": "B", "model": "M"},
        )
        ref_b_strong = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p2",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.SKU, value="S1", namespace="src_a", is_strong=True),),
            canonical_attributes={"brand": "B", "model": "M"},
        )
        res_strong = service.resolve_pair(ref_a_strong, ref_b_strong, policy=strong_policy)
        # Policy declara SKU strong: same namespace + exact SKU -> MATCH.
        assert res_strong.status == MatchStatus.MATCH

    def test_28_sku_different_namespace_not_match_even_if_strong(self):
        """6. Mismo SKU en distintos namespaces NO es MATCH automático, incluso con policy SKU-strong."""
        service = EntityResolutionService()
        strong_policy = EntityResolutionPolicy(
            policy_id="sku_strong_3",
            name="SKU Strong Policy 3",
            version="1.0.0",
            entity_type=EntityType.PRODUCT,
            strong_identifier_types=(IdentifierType.SKU,),
            allow_cross_source_sku_match=False,
        )

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.SKU, value="S1", namespace="src_a", is_strong=True),),
            canonical_attributes={"brand": "B", "model": "M"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.SKU, value="S1", namespace="src_b", is_strong=True),),
            canonical_attributes={"brand": "B", "model": "M"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=strong_policy)
        assert result.status != MatchStatus.MATCH
        assert ResolutionReasonCode.SCOPED_IDENTIFIER_CROSS_NAMESPACE.value in result.reason_codes

    def test_29_gtin_conflict_overrides_similar_attributes(self):
        """7. Conflicto de GTIN fuerza NO_MATCH aunque title/model sean idénticos."""
        service = EntityResolutionService()
        policy = create_default_product_policy()

        ref_a = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_a",
            source_entity_id="p1",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.GTIN, value="6001234567890", is_strong=True),),
            canonical_attributes={"brand": "Sony", "model": "WH-1000XM5", "title": "Auriculares Sony"},
        )
        ref_b = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="src_b",
            source_entity_id="p2",
            identifiers=(EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7001234567890", is_strong=True),),
            canonical_attributes={"brand": "Sony", "model": "WH-1000XM5", "title": "Auriculares Sony"},
        )

        result = service.resolve_pair(ref_a, ref_b, policy=policy)
        assert result.status == MatchStatus.NO_MATCH
        assert ResolutionReasonCode.CONTRADICTORY_STRONG_IDENTIFIERS.value in result.reason_codes
        assert result.confidence_score == Decimal("0.0000")

    def test_30_unordered_identifier_mappings_same_canonical_id(self):
        """8. Mapeos de identificadores desordenados producen el mismo canonical ID/checksum semantics."""
        sku_x = EntityIdentifier(identifier_type=IdentifierType.SKU, value="X", namespace="ns")
        sku_y = EntityIdentifier(identifier_type=IdentifierType.SKU, value="Y", namespace="ns")

        cid_1 = build_deterministic_canonical_entity_id(EntityType.PRODUCT, identifiers=(sku_x, sku_y))
        cid_2 = build_deterministic_canonical_entity_id(EntityType.PRODUCT, identifiers=(sku_y, sku_x))
        assert cid_1 == cid_2

        # Checksum de dos referencias con identifiers en distinto orden también es canónico.
        ref_1 = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="s",
            source_entity_id="e",
            identifiers=(sku_x, sku_y),
        )
        ref_2 = EntityReference(
            entity_type=EntityType.PRODUCT,
            source_id="s",
            source_entity_id="e",
            identifiers=(sku_y, sku_x),
        )
        assert ref_1.checksum == ref_2.checksum
