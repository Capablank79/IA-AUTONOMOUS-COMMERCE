"""
Tests unitarios para Schema Validation L.5 (Transversal Data Quality / Governance).

Cubre:
1. immutable schema
2. immutable result
3. required fields
4. optional fields
5. nullable semantics
6. strict type validation (string, int, decimal, bool, datetime, enum, array, object)
7. no unsafe coercion (e.g. "10" is not decimal automatically if strict / int does not allow bool True)
8. Decimal numeric constraints (min, max)
9. negative price/stock detection
10. enum validation
11. string constraints (min_length, max_length, pattern)
12. datetime validation (timezone-aware UTC requirement, invalid string parsing)
13. nested field errors with explicit field path
14. additional fields ALLOW
15. additional fields FORBID
16. UNKNOWN schema handling
17. versioning idempotence and conflict detection
18. checksum recalculation and tampering detection
19. conflict on same version with different payload
20. sanitization of sensitive data in errors / metadata
21. provenance linkage
22. strict orthogonal boundary: no freshness or confidence calculation
"""

from datetime import datetime, timezone
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
    compute_schema_checksum,
    compute_validation_result_checksum,
)
from src.domain.schema_validation.ports import (
    SchemaRegistryPort,
    SchemaValidationRepositoryPort,
)
from src.application.schema_validation.service import (
    SchemaValidationService,
    _convert_payload_to_dict,
)
from src.infrastructure.persistence.data.json.schema_repository import (
    JsonSchemaRegistryRepository,
    JsonSchemaValidationRepository,
    SchemaConflictError,
    CorruptedSchemaRecordError,
)
from src.domain.reliability.ports import ClockPort


class MockClock(ClockPort):
    def __init__(self, now: datetime):
        self._now = now

    def now_utc(self) -> datetime:
        return self._now


def create_sample_observation_schema() -> SchemaDefinition:
    return SchemaDefinition(
        schema_id="market_obs_schema",
        name="Market Observation Schema",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        additional_fields_policy=AdditionalFieldsPolicy.FORBID,
        fields=(
            FieldDefinition(field_name="observation_id", field_type=FieldType.STRING, min_length=3),
            FieldDefinition(field_name="marketplace", field_type=FieldType.ENUM, enum_values=("MERCADOLIBRE_CHILE", "AMAZON_US")),
            FieldDefinition(
                field_name="price",
                field_type=FieldType.DECIMAL,
                min_value=Decimal("0.01"),
                max_value=Decimal("100000000.00"),
            ),
            FieldDefinition(field_name="stock", field_type=FieldType.INTEGER, min_value=Decimal("0")),
            FieldDefinition(field_name="is_active", field_type=FieldType.BOOLEAN),
            FieldDefinition(field_name="captured_at", field_type=FieldType.DATETIME),
            FieldDefinition(field_name="tags", field_type=FieldType.ARRAY, item_type=FieldType.STRING, required=False),
            FieldDefinition(
                field_name="seller",
                field_type=FieldType.OBJECT,
                required=False,
                nested_fields=(
                    FieldDefinition(field_name="seller_id", field_type=FieldType.STRING),
                    FieldDefinition(field_name="rating", field_type=FieldType.DECIMAL, required=False, nullable=True),
                ),
            ),
        ),
    )


def test_immutable_schema_definition():
    schema = create_sample_observation_schema()
    with pytest.raises(Exception):
        schema.name = "Modified"  # type: ignore


def test_immutable_validation_result():
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    res = SchemaValidationResult(
        validation_id="val_12345",
        schema_id="market_obs_schema",
        schema_version="1.0.0",
        subject_type="MARKET_OBSERVATION",
        status=ValidationStatus.PASS,
        errors=(),
        validated_at=now,
    )
    with pytest.raises(Exception):
        res.status = ValidationStatus.FAIL  # type: ignore


def test_required_fields_and_missing_field_detection(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    # Missing marketplace and price
    payload = {
        "observation_id": "obs_999",
        "stock": 10,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.FAIL
    error_codes = [e.code for e in result.errors]
    assert "MISSING_REQUIRED_FIELD" in error_codes
    missing_fields = [e.field_path for e in result.errors if e.code == "MISSING_REQUIRED_FIELD"]
    assert "marketplace" in missing_fields
    assert "price" in missing_fields


def test_optional_fields_behavior(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    # Optional fields 'tags' and 'seller' omitted -> should PASS
    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.PASS
    assert len(result.errors) == 0


def test_nullable_semantics(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    # seller.rating is nullable=True -> valid
    payload_valid = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        "seller": {
            "seller_id": "seller_01",
            "rating": None,
        },
    }
    res_valid = service.validate(payload=payload_valid, subject_type="MARKET_OBSERVATION")
    assert res_valid.status == ValidationStatus.PASS

    # price is nullable=False -> should FAIL
    payload_invalid = dict(payload_valid)
    payload_invalid["price"] = None
    res_invalid = service.validate(payload=payload_invalid, subject_type="MARKET_OBSERVATION")
    assert res_invalid.status == ValidationStatus.FAIL
    assert any(e.code == "NON_NULLABLE_FIELD" and e.field_path == "price" for e in res_invalid.errors)


def test_strict_type_validation_no_unsafe_coercion(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    # Boolean passed as stock (in Python bool is subclass of int, but validator must reject it)
    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": True,  # INVALID integer
        "is_active": "true",  # INVALID boolean
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.FAIL
    paths = [e.field_path for e in result.errors if e.code == "INVALID_TYPE"]
    assert "stock" in paths
    assert "is_active" in paths


def test_decimal_numeric_constraints_and_negative_price_stock(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    # Negative price and negative stock
    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("-10.00"),
        "stock": -5,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.FAIL
    violations = [e.field_path for e in result.errors if e.code == "MIN_VALUE_VIOLATION"]
    assert "price" in violations
    assert "stock" in violations


def test_enum_validation(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    payload = {
        "observation_id": "obs_999",
        "marketplace": "INVALID_MARKETPLACE",
        "price": Decimal("100"),
        "stock": 1,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.FAIL
    assert any(e.code == "INVALID_ENUM_VALUE" and e.field_path == "marketplace" for e in result.errors)


def test_string_constraints(tmp_path):
    schema = SchemaDefinition(
        schema_id="user_code_schema",
        name="User Code",
        version="1.0.0",
        subject_type="USER_CODE",
        fields=(
            FieldDefinition(
                field_name="sku",
                field_type=FieldType.STRING,
                min_length=3,
                max_length=10,
                pattern=r"^[A-Z0-9]+$",
            ),
        ),
    )
    repo = JsonSchemaRegistryRepository(tmp_path)
    repo.save_schema(schema)
    service = SchemaValidationService(schema_registry=repo)

    # Too short and invalid pattern
    res_short = service.validate(payload={"sku": "a"}, subject_type="USER_CODE")
    assert res_short.status == ValidationStatus.FAIL
    codes = [e.code for e in res_short.errors]
    assert "MIN_LENGTH_VIOLATION" in codes
    assert "PATTERN_MISMATCH" in codes


def test_datetime_validation_timezone_aware(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    # Naive datetime (no tzinfo)
    naive_dt = datetime(2026, 9, 2, 12, 0, 0)
    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": naive_dt,
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.FAIL
    assert any(e.code == "DATETIME_TZ_MISSING" for e in result.errors)


def test_nested_field_errors_with_path(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=repo)

    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        "seller": {
            "seller_id": 12345,  # INVALID type (expected STRING)
            "rating": "not_a_decimal",  # INVALID decimal format
        },
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    assert result.status == ValidationStatus.FAIL
    error_paths = {e.field_path: e.code for e in result.errors}
    assert "seller.seller_id" in error_paths
    assert error_paths["seller.seller_id"] == "INVALID_TYPE"
    assert "seller.rating" in error_paths
    assert error_paths["seller.rating"] == "INVALID_DECIMAL_FORMAT"


def test_additional_fields_policy_allow_and_forbid(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    schema_forbid = create_sample_observation_schema()  # policy=FORBID
    repo.save_schema(schema_forbid)

    schema_allow = SchemaDefinition(
        schema_id="market_obs_allow",
        name="Market Observation Schema Allow",
        version="1.0.0",
        subject_type="MARKET_OBSERVATION_EXTENSIBLE",
        additional_fields_policy=AdditionalFieldsPolicy.ALLOW,
        fields=(
            FieldDefinition(field_name="observation_id", field_type=FieldType.STRING),
        ),
    )
    repo.save_schema(schema_allow)

    service = SchemaValidationService(schema_registry=repo)

    payload_with_extra = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        "extra_unrecognized_field": "some_value",
    }

    # FORBID should FAIL
    res_forbid = service.validate(payload=payload_with_extra, subject_type="MARKET_OBSERVATION")
    assert res_forbid.status == ValidationStatus.FAIL
    assert any(e.code == "FORBIDDEN_ADDITIONAL_FIELD" and e.field_path == "extra_unrecognized_field" for e in res_forbid.errors)

    # ALLOW should PASS
    payload_allow = {"observation_id": "obs_999", "extra_unrecognized_field": "some_value"}
    res_allow = service.validate(payload=payload_allow, subject_type="MARKET_OBSERVATION_EXTENSIBLE")
    assert res_allow.status == ValidationStatus.PASS


def test_unknown_schema_handling(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    service = SchemaValidationService(schema_registry=repo)

    result = service.validate(payload={"some": "data"}, subject_type="NON_EXISTENT_TYPE")
    assert result.status == ValidationStatus.UNKNOWN
    assert result.status != ValidationStatus.PASS
    assert any(e.code == "UNKNOWN_SCHEMA" for e in result.errors)


def test_versioning_and_conflict_detection(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    s1 = SchemaDefinition(
        schema_id="simple_schema",
        name="Simple Schema v1",
        version="1.0.0",
        subject_type="SIMPLE",
        fields=(FieldDefinition(field_name="id", field_type=FieldType.STRING),),
    )
    repo.save_schema(s1)

    # Same content and version -> idempotent
    s1_same = SchemaDefinition(
        schema_id="simple_schema",
        name="Simple Schema v1",
        version="1.0.0",
        subject_type="SIMPLE",
        fields=(FieldDefinition(field_name="id", field_type=FieldType.STRING),),
    )
    saved = repo.save_schema(s1_same)
    assert saved.checksum == s1.checksum

    # Same version but different fields -> ConflictError
    s1_conflict = SchemaDefinition(
        schema_id="simple_schema",
        name="Simple Schema v1 Altered",
        version="1.0.0",
        subject_type="SIMPLE",
        fields=(
            FieldDefinition(field_name="id", field_type=FieldType.STRING),
            FieldDefinition(field_name="name", field_type=FieldType.STRING),
        ),
    )
    with pytest.raises(SchemaConflictError):
        repo.save_schema(s1_conflict)


def test_checksum_tampering_detection(tmp_path):
    repo = JsonSchemaRegistryRepository(tmp_path)
    s1 = SchemaDefinition(
        schema_id="tamper_schema",
        name="Tamper Schema",
        version="1.0.0",
        subject_type="TAMPER",
        fields=(FieldDefinition(field_name="id", field_type=FieldType.STRING),),
    )
    repo.save_schema(s1)

    # Alter file on disk
    file_path = tmp_path / "schemas" / "definitions" / "tamper_schema_v1.0.0.json"
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    tampered_content = content.replace("Tamper Schema", "Tampered Title")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(tampered_content)

    # Loading corrupted repo should raise CorruptedSchemaRecordError
    with pytest.raises(CorruptedSchemaRecordError):
        JsonSchemaRegistryRepository(tmp_path)


def test_sanitization_of_secrets_in_errors_and_metadata():
    error = ValidationError(
        field_path="auth.token",
        code="INVALID_TOKEN",
        message="Invalid token=super_secret_password_12345 provided",
    )
    assert "super_secret_password_12345" not in error.message
    assert "[REDACTED]" in error.message

    schema = SchemaDefinition(
        schema_id="secure_schema",
        name="Secure Schema",
        version="1.0.0",
        subject_type="SECURE",
        fields=(FieldDefinition(field_name="id", field_type=FieldType.STRING),),
        metadata={"api_key": "raw_secret_key_999"},
    )
    assert schema.metadata["api_key"] == "[REDACTED]"


def test_provenance_linkage(tmp_path):
    schema_repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    schema_repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=schema_repo)

    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
    }

    result = service.validate(
        payload=payload,
        subject_type="MARKET_OBSERVATION",
        provenance_id="prov_ml_chile_001",
        subject_id="obs_999",
        correlation_id="corr_tx_555",
    )
    assert result.provenance_id == "prov_ml_chile_001"
    assert result.subject_id == "obs_999"
    assert result.correlation_id == "corr_tx_555"
    assert result.status == ValidationStatus.PASS


def test_no_freshness_or_confidence_logic_in_l5(tmp_path):
    schema_repo = JsonSchemaRegistryRepository(tmp_path)
    schema = create_sample_observation_schema()
    schema_repo.save_schema(schema)

    service = SchemaValidationService(schema_registry=schema_repo)

    # Extremely old timestamp: schema validation only checks datetime validity/type, NOT freshness TTL
    old_time = datetime(1990, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    payload = {
        "observation_id": "obs_999",
        "marketplace": "MERCADOLIBRE_CHILE",
        "price": Decimal("15000"),
        "stock": 5,
        "is_active": True,
        "captured_at": old_time,
    }

    result = service.validate(payload=payload, subject_type="MARKET_OBSERVATION")
    # L.5 only validates structure: PASS (freshness remains strictly L.3 responsibility)
    assert result.status == ValidationStatus.PASS
