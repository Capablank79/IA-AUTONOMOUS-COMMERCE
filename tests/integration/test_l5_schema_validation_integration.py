"""
Tests de integración y E2E para Schema Validation L.5 (Transversal Data Quality / Governance).

Escenarios cubiertos:
A. valid MarketObservation -> PASS.
B. missing required field -> FAIL.
C. wrong type -> FAIL.
D. supplier quote valid -> PASS.
E. invalid commercial numeric -> FAIL.
F. nested invalid field -> structured field-path error.
G. unknown schema -> UNKNOWN.
H. schema v1 vs v2 -> deterministic version behavior.
I. restart durability -> schemas and results preserved.
J. tampered schema/result -> corruption detected.
K. E2E Data Quality Flow: Source Registry (L.1) -> Data Provenance (L.2) -> Schema Validation (L.5) -> Freshness (L.3) -> Confidence (L.4).
   Demuestra que un dato con esquema inválido no se propaga como hecho válido ni se calcula indebidamente.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from pathlib import Path

from src.domain.schema_validation.models import (
    ValidationStatus,
    FieldType,
    AdditionalFieldsPolicy,
    FieldDefinition,
    SchemaDefinition,
    ValidationError,
    SchemaValidationResult,
)
from src.application.schema_validation.service import SchemaValidationService
from src.infrastructure.persistence.data.json.schema_repository import (
    JsonSchemaRegistryRepository,
    JsonSchemaValidationRepository,
    CorruptedSchemaRecordError,
)

# Integración con L.1 - L.4
from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository
from src.domain.data_provenance.models import ProvenanceRecord, SubjectType
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository
from src.domain.freshness.models import FreshnessPolicy, FreshnessStatus
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
)
from src.application.freshness.service import FreshnessService
from src.domain.confidence.models import ConfidencePolicy, ConfidenceLevel
from src.infrastructure.persistence.data.json.confidence_repository import (
    JsonConfidencePolicyRepository,
    JsonConfidenceAssessmentRepository,
)
from src.application.confidence.service import ConfidenceService
from src.domain.reliability.ports import ClockPort


class FrozenClock(ClockPort):
    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        pass


def test_scenario_a_valid_market_observation(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    schema = SchemaDefinition(
        schema_id="market_obs_schema",
        name="Market Observation",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        additional_fields_policy=AdditionalFieldsPolicy.FORBID,
        fields=(
            FieldDefinition(field_name="observation_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="marketplace", field_type=FieldType.ENUM, enum_values=("MERCADOLIBRE_CHILE",)),
            FieldDefinition(field_name="price", field_type=FieldType.DECIMAL, min_value=Decimal("0")),
            FieldDefinition(field_name="stock", field_type=FieldType.INTEGER, min_value=Decimal("0")),
            FieldDefinition(field_name="seller_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="captured_at", field_type=FieldType.DATETIME),
        ),
    )
    registry.save_schema(schema)

    service = SchemaValidationService(schema_registry=registry)

    valid_payload = {
        "observation_id": "obs_ml_100",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("19990.00"),
        "stock": 12,
        "seller_id": "seller_cl_99",
        "captured_at": datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc),
    }

    res = service.validate(payload=valid_payload, subject_type="MARKET_OBSERVATION")
    assert res.status == ValidationStatus.PASS
    assert len(res.errors) == 0


def test_scenario_b_missing_required_field(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    schema = SchemaDefinition(
        schema_id="market_obs_schema",
        name="Market Observation",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        fields=(
            FieldDefinition(field_name="observation_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="price", field_type=FieldType.DECIMAL),
        ),
    )
    registry.save_schema(schema)

    service = SchemaValidationService(schema_registry=registry)
    res = service.validate(payload={"observation_id": "obs_101"}, subject_type="MARKET_OBSERVATION")
    assert res.status == ValidationStatus.FAIL
    assert any(e.code == "MISSING_REQUIRED_FIELD" and e.field_path == "price" for e in res.errors)


def test_scenario_c_wrong_type(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    schema = SchemaDefinition(
        schema_id="market_obs_schema",
        name="Market Observation",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        fields=(
            FieldDefinition(field_name="stock", field_type=FieldType.INTEGER),
        ),
    )
    registry.save_schema(schema)

    service = SchemaValidationService(schema_registry=registry)
    # String "15" instead of integer 15
    res = service.validate(payload={"stock": "15"}, subject_type="MARKET_OBSERVATION")
    assert res.status == ValidationStatus.FAIL
    assert any(e.code == "INVALID_TYPE" and e.field_path == "stock" for e in res.errors)


def test_scenario_d_supplier_quote_valid(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    schema = SchemaDefinition(
        schema_id="supplier_quote_schema",
        name="Supplier Commercial Quote",
        version="1.0.0",
        subject_type="SUPPLIER_QUOTE",
        fields=(
            FieldDefinition(field_name="quote_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="supplier_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="sku", field_type=FieldType.STRING),
            FieldDefinition(field_name="unit_cost", field_type=FieldType.DECIMAL, min_value=Decimal("0.01")),
            FieldDefinition(field_name="moq", field_type=FieldType.INTEGER, min_value=Decimal("1")),
            FieldDefinition(field_name="lead_time_days", field_type=FieldType.INTEGER, min_value=Decimal("0")),
            FieldDefinition(field_name="quoted_at", field_type=FieldType.DATETIME),
        ),
    )
    registry.save_schema(schema)

    service = SchemaValidationService(schema_registry=registry)

    quote_payload = {
        "quote_id": "quote_888",
        "supplier_id": "sup_chinadirect_01",
        "sku": "SKU-AUTO-001",
        "unit_cost": Decimal("4.50"),
        "moq": 50,
        "lead_time_days": 14,
        "quoted_at": datetime(2026, 9, 2, 8, 30, 0, tzinfo=timezone.utc),
    }

    res = service.validate(payload=quote_payload, subject_type="SUPPLIER_QUOTE")
    assert res.status == ValidationStatus.PASS
    assert len(res.errors) == 0


def test_scenario_e_invalid_commercial_numeric(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    schema = SchemaDefinition(
        schema_id="supplier_quote_schema",
        name="Supplier Commercial Quote",
        version="1.0.0",
        subject_type="SUPPLIER_QUOTE",
        fields=(
            FieldDefinition(field_name="unit_cost", field_type=FieldType.DECIMAL, min_value=Decimal("0.01")),
            FieldDefinition(field_name="moq", field_type=FieldType.INTEGER, min_value=Decimal("1")),
        ),
    )
    registry.save_schema(schema)

    service = SchemaValidationService(schema_registry=registry)

    # Unit cost 0 (below min 0.01) and negative moq
    res = service.validate(
        payload={"unit_cost": Decimal("0.00"), "moq": -10},
        subject_type="SUPPLIER_QUOTE",
    )
    assert res.status == ValidationStatus.FAIL
    error_paths = {e.field_path: e.code for e in res.errors}
    assert error_paths.get("unit_cost") == "MIN_VALUE_VIOLATION"
    assert error_paths.get("moq") == "MIN_VALUE_VIOLATION"


def test_scenario_f_nested_invalid_field_path(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    schema = SchemaDefinition(
        schema_id="supplier_profile_schema",
        name="Supplier Profile",
        version="1.0.0",
        subject_type="SUPPLIER_PROFILE",
        fields=(
            FieldDefinition(
                field_name="supplier",
                field_type=FieldType.OBJECT,
                nested_fields=(
                    FieldDefinition(
                        field_name="address",
                        field_type=FieldType.OBJECT,
                        nested_fields=(
                            FieldDefinition(field_name="country", field_type=FieldType.STRING, min_length=2),
                            FieldDefinition(field_name="postal_code", field_type=FieldType.STRING),
                        ),
                    ),
                ),
            ),
        ),
    )
    registry.save_schema(schema)

    service = SchemaValidationService(schema_registry=registry)

    payload = {
        "supplier": {
            "address": {
                "country": "A",  # Min length violation (< 2)
                "postal_code": 8320000,  # Invalid type (expected STRING)
            }
        }
    }

    res = service.validate(payload=payload, subject_type="SUPPLIER_PROFILE")
    assert res.status == ValidationStatus.FAIL
    error_paths = {e.field_path: e.code for e in res.errors}
    assert "supplier.address.country" in error_paths
    assert error_paths["supplier.address.country"] == "MIN_LENGTH_VIOLATION"
    assert "supplier.address.postal_code" in error_paths
    assert error_paths["supplier.address.postal_code"] == "INVALID_TYPE"


def test_scenario_g_unknown_schema(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    service = SchemaValidationService(schema_registry=registry)

    res = service.validate(payload={"data": 123}, subject_type="UNREGISTERED_EVENT")
    assert res.status == ValidationStatus.UNKNOWN
    assert res.status != ValidationStatus.PASS


def test_scenario_h_schema_v1_vs_v2(tmp_path):
    registry = JsonSchemaRegistryRepository(tmp_path)
    s_v1 = SchemaDefinition(
        schema_id="order_schema",
        name="Order Schema",
        version="1.0.0",
        subject_type="ORDER",
        fields=(
            FieldDefinition(field_name="order_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="amount", field_type=FieldType.DECIMAL),
        ),
    )
    s_v2 = SchemaDefinition(
        schema_id="order_schema",
        name="Order Schema",
        version="2.0.0",
        subject_type="ORDER",
        fields=(
            FieldDefinition(field_name="order_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="amount", field_type=FieldType.DECIMAL),
            FieldDefinition(field_name="currency", field_type=FieldType.STRING, required=True),
        ),
    )
    registry.save_schema(s_v1)
    registry.save_schema(s_v2)

    service = SchemaValidationService(schema_registry=registry)

    payload_without_currency = {
        "order_id": "ord_101",
        "amount": Decimal("5000"),
    }

    # Validate against v1.0.0 -> PASS
    res_v1 = service.validate(
        payload=payload_without_currency,
        subject_type="ORDER",
        schema_id="order_schema",
        schema_version="1.0.0",
    )
    assert res_v1.status == ValidationStatus.PASS

    # Validate against v2.0.0 -> FAIL (missing currency)
    res_v2 = service.validate(
        payload=payload_without_currency,
        subject_type="ORDER",
        schema_id="order_schema",
        schema_version="2.0.0",
    )
    assert res_v2.status == ValidationStatus.FAIL
    assert any(e.code == "MISSING_REQUIRED_FIELD" and e.field_path == "currency" for e in res_v2.errors)


def test_scenario_i_restart_durability(tmp_path):
    # Phase 1: Write schemas and validation results
    reg1 = JsonSchemaRegistryRepository(tmp_path)
    res_repo1 = JsonSchemaValidationRepository(tmp_path)

    schema = SchemaDefinition(
        schema_id="durable_schema",
        name="Durable Schema",
        version="1.0.0",
        subject_type="DURABLE",
        fields=(FieldDefinition(field_name="token", field_type=FieldType.STRING),),
    )
    reg1.save_schema(schema)

    service1 = SchemaValidationService(schema_registry=reg1, validation_repository=res_repo1)
    service1.validate(
        payload={"token": "abc"},
        subject_type="DURABLE",
        subject_id="sub_999",
        persist=True,
    )

    # Phase 2: Simulate restart with fresh instances pointing to same storage
    reg2 = JsonSchemaRegistryRepository(tmp_path)
    res_repo2 = JsonSchemaValidationRepository(tmp_path)

    recovered_schema = reg2.get_schema("durable_schema", "1.0.0")
    assert recovered_schema is not None
    assert recovered_schema.name == "Durable Schema"

    latest_res = res_repo2.get_latest_by_subject(subject_id="sub_999", subject_type="DURABLE")
    assert latest_res is not None
    assert latest_res.status == ValidationStatus.PASS
    assert latest_res.schema_id == "durable_schema"


def test_scenario_j_tampered_record_corruption(tmp_path):
    res_repo = JsonSchemaValidationRepository(tmp_path)
    res = SchemaValidationResult(
        validation_id="val_corrupt_test",
        schema_id="test_schema",
        schema_version="1.0.0",
        subject_type="TEST",
        status=ValidationStatus.PASS,
        errors=(),
        validated_at=datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    )
    res_repo.save_result(res)

    # Tamper file on disk
    file_path = tmp_path / "schemas" / "results" / "val_corrupt_test.json"
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Change status to FAIL without updating checksum
    tampered_content = content.replace('"status": "PASS"', '"status": "FAIL"')
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(tampered_content)

    with pytest.raises(CorruptedSchemaRecordError):
        JsonSchemaValidationRepository(tmp_path)


def test_scenario_k_e2e_data_quality_governance_pipeline(tmp_path):
    """
    Pipeline completo E2E:
    Source Registry (L.1) -> Data Provenance (L.2) -> Schema Validation (L.5) -> Freshness (L.3) -> Confidence (L.4).
    Demuestra que datos con error de esquema se detienen y no son considerados válidos en los pasos subsiguientes.
    """
    fixed_time = datetime(2026, 9, 2, 14, 0, 0, tzinfo=timezone.utc)
    clock = FrozenClock(fixed_time)

    # 1. Setup Repositories
    src_repo = JsonSourceRegistryRepository(tmp_path)
    prov_repo = JsonProvenanceRepository(tmp_path)
    schema_registry = JsonSchemaRegistryRepository(tmp_path)
    schema_val_repo = JsonSchemaValidationRepository(tmp_path)
    freshness_policy_repo = JsonFreshnessPolicyRepository(tmp_path)
    freshness_assessment_repo = JsonFreshnessAssessmentRepository(tmp_path)
    confidence_policy_repo = JsonConfidencePolicyRepository(tmp_path)
    confidence_assessment_repo = JsonConfidenceAssessmentRepository(tmp_path)

    # 2. Registrar Fuente en L.1
    registered_source = RegisteredSource(
        source_id="src_meli_cl",
        name="Mercado Libre Chile API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace:mercadolibre:cl",
        created_at=fixed_time,
        updated_at=fixed_time,
        status=SourceStatus.ACTIVE,
        version="1.0.0",
    )
    src_repo.save_source(registered_source)

    # 3. Registrar Esquema en L.5
    market_schema = SchemaDefinition(
        schema_id="meli_observation_schema",
        name="Mercado Libre Observation Schema",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        additional_fields_policy=AdditionalFieldsPolicy.FORBID,
        fields=(
            FieldDefinition(field_name="observation_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="item_id", field_type=FieldType.STRING),
            FieldDefinition(field_name="price", field_type=FieldType.DECIMAL, min_value=Decimal("0.01")),
            FieldDefinition(field_name="currency", field_type=FieldType.ENUM, enum_values=("CLP", "USD")),
            FieldDefinition(field_name="stock", field_type=FieldType.INTEGER, min_value=Decimal("0")),
            FieldDefinition(field_name="captured_at", field_type=FieldType.DATETIME),
        ),
    )
    schema_registry.save_schema(market_schema)

    # 4. Registrar Políticas en L.3 y L.4
    freshness_policy = FreshnessPolicy(
        policy_id="freshness_market_obs",
        name="Market Observation TTL",
        version="1.0.0",
        ttl_seconds=3600.0,  # 1 hora
        subject_type="MARKET_OBSERVATION",
    )
    freshness_policy_repo.save_policy(freshness_policy)

    confidence_policy = ConfidencePolicy(
        policy_id="conf_market_obs",
        name="Market Observation Confidence Policy",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        weights={"source": Decimal("0.5"), "provenance": Decimal("0.5")},
        factor_scores={
            "source_active": Decimal("0.90"),
            "provenance_direct": Decimal("0.95"),
            "evidence_present": Decimal("0.90"),
        },
    )
    confidence_policy_repo.save_policy(confidence_policy)

    # 5. Inicializar Servicios
    schema_service = SchemaValidationService(
        schema_registry=schema_registry,
        validation_repository=schema_val_repo,
        clock=clock,
    )
    freshness_service = FreshnessService(
        policy_repository=freshness_policy_repo,
        assessment_repository=freshness_assessment_repo,
        clock=clock,
    )
    confidence_service = ConfidenceService(
        policy_repository=confidence_policy_repo,
        assessment_repository=confidence_assessment_repo,
        source_registry=src_repo,
        provenance_repository=prov_repo,
        freshness_repository=freshness_assessment_repo,
        clock=clock,
    )

    # 6. Caso 1: Flujo Exitoso con Payload Válido
    prov_record_valid = ProvenanceRecord(
        provenance_id="prov_obs_valid_01",
        source_id="src_meli_cl",
        source_version="1.0.0",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs_valid_01",
        captured_at=fixed_time - timedelta(minutes=15),
    )
    prov_repo.save_provenance(prov_record_valid)

    valid_payload = {
        "observation_id": "obs_valid_01",
        "item_id": "MLC12345678",
        "price": Decimal("25990.00"),
        "currency": "CLP",
        "stock": 10,
        "captured_at": fixed_time - timedelta(minutes=15),
    }

    # Step A: Schema Validation L.5
    schema_val_1 = schema_service.validate(
        payload=valid_payload,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs_valid_01",
        provenance_id=prov_record_valid.provenance_id,
        persist=True,
    )
    assert schema_val_1.status == ValidationStatus.PASS

    # Step B: Freshness Assessment L.3
    freshness_ass_1 = freshness_service.evaluate_timestamp(
        observed_at=valid_payload["captured_at"],
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs_valid_01",
        persist=True,
    )
    assert freshness_ass_1.status == FreshnessStatus.FRESH

    # Step C: Confidence Assessment L.4
    confidence_ass_1 = confidence_service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs_valid_01",
        source_id="src_meli_cl",
        provenance_id=prov_record_valid.provenance_id,
        freshness_assessment=freshness_ass_1,
        persist=True,
    )
    assert confidence_ass_1.level == ConfidenceLevel.HIGH

    # 7. Caso 2: Payload Inválido por Tipo y Violación Numérica
    invalid_payload = {
        "observation_id": "obs_invalid_02",
        "item_id": "MLC88888888",
        "price": Decimal("-500.00"),  # INVALID (price < 0.01)
        "currency": "EUR",  # INVALID enum value
        "stock": "in_stock",  # INVALID type (expected int)
        "captured_at": fixed_time - timedelta(minutes=10),
    }

    schema_val_2 = schema_service.validate(
        payload=invalid_payload,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs_invalid_02",
        persist=True,
    )
    assert schema_val_2.status == ValidationStatus.FAIL
    assert len(schema_val_2.errors) == 3

    # Al fallar schema validation, el consumidor detecta el fallo y NO propaga el dato a downstream
    assert schema_val_2.status != ValidationStatus.PASS
