"""
Unit Tests for Hito N.9 — Sensitive Data Handling (Transversal N — Security, Governance & Safety).

Cubre exhaustivamente los 16 requerimientos mínimos:
1. PUBLIC data unchanged
2. SENSITIVE classified
3. UNKNOWN != PUBLIC (fail-secure)
4. email masking
5. phone masking
6. nested structure redaction
7. data minimization
8. restricted logging blocked
9. restricted cache blocked
10. safe cache fingerprint
11. private prompt not logged / redacted
12. secret recognized/redacted without replacing N.5
13. deterministic decision
14. policy versioning and integrity
15. no plaintext sensitive data in repr/result
16. no N.10/N.11 implementation
"""

import pytest
from types import MappingProxyType
from typing import Dict, Any

from src.domain.security.sensitive_data_models import (
    DataClassification,
    SensitiveCategory,
    DataHandlingPurpose,
    PersistenceHandlingMode,
    CacheHandlingMode,
    DataHandlingReasonCode,
    SensitiveFieldDescriptor,
    SensitiveDataClassification,
    DataHandlingPolicy,
    DataHandlingRequest,
    RedactionResult,
    DataHandlingDecision,
    mask_email,
    mask_phone,
    mask_rut_dni,
    mask_credit_card,
    compute_deterministic_fingerprint,
)
from src.domain.security.sensitive_data_engine import (
    DeterministicSensitiveDataClassifier,
    DeterministicSensitiveDataRedactor,
)
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService


# 1. PUBLIC data unchanged
def test_public_data_unchanged():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    public_payload = {
        "item_title": "Laptop Gamer 16GB",
        "category_id": "MLA1055",
        "price": 1200.50,
        "is_active": True,
    }

    req = DataHandlingRequest(payload=public_payload, purpose=DataHandlingPurpose.GENERAL)
    decision = service.evaluate(req)

    assert decision.is_allowed_for_purpose is True
    assert decision.classification.overall_classification == DataClassification.PUBLIC
    assert decision.redaction_result is not None
    assert decision.redaction_result.sanitized_payload == public_payload
    assert decision.redaction_result.fields_redacted_count == 0


# 2. SENSITIVE classified
def test_sensitive_data_classified_accurately():
    classifier = DeterministicSensitiveDataClassifier()

    payload = {
        "order_id": "ord_9988",
        "buyer_name": "Carlos Gomez",
        "buyer_email": "carlos.gomez@example.com",
        "shipping_address": "Av. Siempre Viva 742",
    }

    classification = classifier.classify(payload)

    assert classification.overall_classification == DataClassification.SENSITIVE
    assert classification.is_sensitive is True
    assert classification.requires_redaction is True
    assert SensitiveCategory.CONTACT_DATA in classification.all_categories
    assert SensitiveCategory.PERSONAL_DATA in classification.all_categories
    assert SensitiveCategory.ADDRESS_DATA in classification.all_categories
    assert len(classification.detected_fields) >= 3


# 3. UNKNOWN != PUBLIC (fail-secure)
def test_unknown_classification_is_fail_secure_and_not_public():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    # Objeto con clasificación forzada UNKNOWN
    unknown_classification = SensitiveDataClassification(
        overall_classification=DataClassification.UNKNOWN,
        primary_category=SensitiveCategory.UNKNOWN,
        all_categories=(),
        detected_fields=(),
        is_sensitive=True,
        requires_redaction=True,
    )

    class CustomMockClassifier:
        def classify(self, data, context_purpose=None):
            return unknown_classification

    service_fail_secure = SensitiveDataHandlingService(classifier=CustomMockClassifier(), redactor=redactor)

    req = DataHandlingRequest(payload={"some_raw_uninspected_data": 123}, purpose=DataHandlingPurpose.GENERAL)
    decision = service_fail_secure.evaluate(req)

    assert decision.is_allowed_for_purpose is False
    assert decision.logging_allowed is False
    assert decision.cache_allowed is False
    assert decision.external_transfer_allowed is False
    assert decision.persistence_mode == PersistenceHandlingMode.DENY
    assert decision.cache_mode == CacheHandlingMode.DENY
    assert decision.reason_code == DataHandlingReasonCode.UNKNOWN_CLASSIFICATION_FAIL_SECURE


# 4. email masking
def test_email_masking_deterministic():
    assert mask_email("john.doe@example.com") == "jo***@example.com"
    assert mask_email("a@b.com") == "a***@b.com"
    assert mask_email("user@domain.org") == "us***@domain.org"
    assert mask_email("invalid-email") == "[REDACTED_EMAIL]"


# 5. phone masking
def test_phone_masking_deterministic():
    assert mask_phone("+56 9 1234 5678") == "******5678"
    assert mask_phone("+1-800-555-0199") == "******0199"
    assert mask_phone("1234") == "******1234"
    assert mask_phone("55") == "******55"


# 6. nested structure redaction
def test_nested_structure_recursive_redaction():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    nested_payload = {
        "order": {
            "id": "ord_1001",
            "customer": {
                "name": "Maria Lopez",
                "contacts": [
                    {"type": "primary", "email": "maria.lopez@example.com"},
                    {"type": "secondary", "phone": "+56987654321"},
                ],
                "address": {
                    "street_name": "Alameda",
                    "street_number": "1050",
                    "city": "Santiago",
                    "zipcode": "8320000",
                },
            },
            "financial": {
                "card_number": "4532 1111 2222 3333",
                "payment_method_id": "visa",
            },
        },
        "metadata": {
            "tags": ["vip", "priority"],
        },
    }

    req = DataHandlingRequest(payload=nested_payload, purpose=DataHandlingPurpose.AUDIT)
    decision = service.evaluate(req)

    sanitized = decision.redaction_result.sanitized_payload
    assert sanitized["order"]["customer"]["contacts"][0]["email"] == "ma***@example.com"
    assert sanitized["order"]["customer"]["contacts"][1]["phone"] == "******4321"
    assert sanitized["order"]["customer"]["address"]["street_name"] == "[REDACTED_ADDRESS]"
    assert sanitized["order"]["financial"]["card_number"] == "[REDACTED_FINANCIAL]"
    # Verificación de que la estructura no sensible sigue intacta
    assert sanitized["order"]["id"] == "ord_1001"
    assert sanitized["order"]["customer"]["address"]["city"] == "Santiago"
    assert sanitized["metadata"]["tags"] == ["vip", "priority"]


# 7. data minimization
def test_data_minimization_keeps_only_required_fields():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    large_payload = {
        "order_id": "ord_555",
        "city": "Valparaíso",
        "buyer_name": "Juan Perez",
        "shipping_address": "Av. Brasil 2950",
        "notes": "Dejar en conserjería",
    }

    # Solo requerimos order_id y city para el análisis
    req = DataHandlingRequest(
        payload=large_payload,
        purpose=DataHandlingPurpose.INFERENCE,
        required_fields=("order_id", "city"),
    )
    decision = service.evaluate(req)

    sanitized = decision.redaction_result.sanitized_payload
    assert sanitized["order_id"] == "ord_555"
    assert sanitized["city"] == "Valparaíso"
    assert sanitized["buyer_name"] == "[MINIMIZED_FIELD]"
    assert sanitized["shipping_address"] == "[MINIMIZED_FIELD]"


# 8. restricted logging blocked
def test_restricted_logging_blocked():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    restricted_payload = {
        "card_number": "4111 2222 3333 4444",
        "cvv": "123",
        "user_email": "admin@example.com",
    }

    req = DataHandlingRequest(payload=restricted_payload, purpose=DataHandlingPurpose.LOGGING)
    decision = service.evaluate(req)

    assert decision.logging_allowed is False


# 9. restricted cache blocked
def test_restricted_cache_blocked():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    restricted_payload = {
        "auth_header": "Bearer eyJhbGciOi...",
        "bank_account": "CL02001928374",
    }

    req = DataHandlingRequest(payload=restricted_payload, purpose=DataHandlingPurpose.CACHE)
    decision = service.evaluate(req)

    assert decision.cache_allowed is False
    cached_val = service.sanitize_for_cache(restricted_payload)
    assert cached_val is None


# 10. safe cache fingerprint
def test_safe_cache_fingerprint_generation():
    service = SensitiveDataHandlingService()

    payload = {
        "buyer_id": "MLU_998877",
        "customer_id": "cust_12345",
        "email": "buyer@example.com",
    }

    req = DataHandlingRequest(payload=payload, purpose=DataHandlingPurpose.CACHE)
    decision = service.evaluate(req)

    assert "safe_buyer_id_fingerprint" in decision.safe_identifiers
    assert "safe_customer_id_fingerprint" in decision.safe_identifiers
    assert "safe_email_fingerprint" in decision.safe_identifiers

    # El fingerprint debe ser un hash opaco y determinista (no texto plano)
    fp = decision.safe_identifiers["safe_buyer_id_fingerprint"]
    assert fp != "MLU_998877"
    assert len(fp) == 24
    assert compute_deterministic_fingerprint("MLU_998877") == fp


# 11. private prompt not logged / redacted
def test_private_prompt_context_redacted_and_not_logged():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    reasoning_payload = {
        "user_query": "Optimizar margen de producto X",
        "chain_of_thought": "Thinking step: competitor has stock at $10, so we can undercut by 2%",
        "reasoning": "Internal strategic calculation",
    }

    req = DataHandlingRequest(payload=reasoning_payload, purpose=DataHandlingPurpose.AUDIT)
    decision = service.evaluate(req)

    assert decision.logging_allowed is False
    sanitized = decision.redaction_result.sanitized_payload
    assert sanitized["chain_of_thought"] == "[REDACTED_INTERNAL_REASONING]"
    assert sanitized["reasoning"] == "[REDACTED_INTERNAL_REASONING]"
    assert sanitized["user_query"] == "Optimizar margen de producto X"


# 12. secret recognized/redacted without replacing N.5
def test_secret_recognized_and_redacted_without_replacing_n5():
    classifier = DeterministicSensitiveDataClassifier()
    redactor = DeterministicSensitiveDataRedactor()
    service = SensitiveDataHandlingService(classifier=classifier, redactor=redactor)

    payload_with_secret = {
        "endpoint": "https://api.mercadolibre.com/items",
        "api_key": "sec_live_998877665544332211",
        "access_token": "APP_USR-88273619283",
        "item_id": "MLA123456",
    }

    req = DataHandlingRequest(payload=payload_with_secret, purpose=DataHandlingPurpose.LOGGING)
    decision = service.evaluate(req)

    assert decision.classification.primary_category == SensitiveCategory.TECHNICAL_SECRET
    sanitized = decision.redaction_result.sanitized_payload
    assert sanitized["api_key"] == "[REDACTED_SECRET]"
    assert sanitized["access_token"] == "[REDACTED_SECRET]"
    assert sanitized["endpoint"] == "https://api.mercadolibre.com/items"
    assert sanitized["item_id"] == "MLA123456"


# 13. deterministic decision
def test_deterministic_decision_reproducibility():
    service = SensitiveDataHandlingService()

    payload = {
        "email": "test@domain.com",
        "rut": "12.345.678-9",
    }

    req1 = DataHandlingRequest(payload=payload, purpose=DataHandlingPurpose.AUDIT)
    req2 = DataHandlingRequest(payload=payload, purpose=DataHandlingPurpose.AUDIT)

    dec1 = service.evaluate(req1)
    dec2 = service.evaluate(req2)

    assert dec1.classification.overall_classification == dec2.classification.overall_classification
    assert dec1.classification.primary_category == dec2.classification.primary_category
    assert dec1.redaction_result.sanitized_payload == dec2.redaction_result.sanitized_payload
    assert dec1.logging_allowed == dec2.logging_allowed
    assert dec1.cache_allowed == dec2.cache_allowed


# 14. policy versioning and integrity
def test_policy_versioning_and_integrity():
    policy = DataHandlingPolicy(
        policy_name="custom_strict_policy",
        version="2.1.0",
        default_classification=DataClassification.SENSITIVE,
        persistence_mode=PersistenceHandlingMode.REDACT,
        cache_mode=CacheHandlingMode.DENY,
    )

    assert policy.version == "2.1.0"
    assert policy.verify_integrity() is True
    assert len(policy.checksum) == 64


# 15. no plaintext sensitive data in repr/result
def test_no_plaintext_sensitive_data_in_repr():
    redaction_res = RedactionResult(
        sanitized_payload={"email": "jo***@example.com"},
        fields_redacted_count=1,
        redacted_field_paths=("order.customer.email",),
    )

    repr_str = repr(redaction_res)
    str_str = str(redaction_res)

    # El repr/str de RedactionResult no debe volcar el payload sanitizado ni el original
    assert "jo***@example.com" not in repr_str
    assert "fields_redacted_count=1" in repr_str
    assert "order.customer.email" in str_str


# 16. no N.10/N.11 implementation
def test_no_n10_n11_leakage():
    # Asegurar que no se hayan introducido clases de compliance, certificación o emergency stop en N.9
    import src.domain.security.sensitive_data_models as n9_models

    n9_symbols = dir(n9_models)
    forbidden_terms = [
        "ComplianceReport",
        "RegulatoryCertification",
        "EmergencyStop",
        "KillSwitch",
        "LegalConsentEngine",
    ]
    for term in forbidden_terms:
        assert term not in n9_symbols, f"N.9 must not implement {term} (reserved for N.10/N.11)"
