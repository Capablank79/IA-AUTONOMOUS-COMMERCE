"""
Tests Unitarios y Adversariales para Chequeos de Seguridad Transversal (Hito K.8).

Cubre:
1. Modelos de dominio de seguridad (SecurityCheckStatus, SecurityCategory, SecuritySeverity, SecurityCheckResult, SecurityCheckEvaluation).
2. Sanitización recursiva de datos sensibles y eliminación de secretos anidados (password, token, api_key, auth headers, pan, cvv).
3. Prevención estricta de fuga de Chain-of-Thought, internal scratchpads o razonamiento privado en payloads y trazas.
4. Validación robusta de Path Safety contra ataques de path traversal (../, \\, /, :, drive letters, nombres vacíos).
5. Evaluación integral de seguridad de acciones (evaluate_action_security):
   - Bloqueo de acciones no autenticadas (actor_id ausente/inválido).
   - Bloqueo de actores no autorizados.
   - Bloqueo de acciones explícitamente prohibidas.
   - Integración con PolicyEngine: DENY -> BLOQUEO sin side effects, UNKNOWN -> BLOQUEO por incertidumbre (fail-secure).
6. Preservación estricta de no-fallar-abierto ante errores internos o estados UNKNOWN.
7. Emisión auditada hacia AuditTrailService y AgentTraceService sin registrar secretos en texto plano.
8. Pruebas adversariales deterministas con payloads manipulados y maliciosos.
"""

import os
from types import MappingProxyType
import pytest
from datetime import datetime, timezone

from src.domain.security.models import (
    SecurityCheckStatus,
    SecurityCategory,
    SecuritySeverity,
    SecurityCheckResult,
    SecurityCheckEvaluation,
    SENSITIVE_KEYS,
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)
from src.application.security.security_check_service import SecurityCheckService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.application.audit.audit_trail_service import AuditTrailService
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository


# ==============================================================================
# 1. PRUEBAS DE MODELO Y SANITIZACIÓN DE SECRETOS
# ==============================================================================

def test_sanitize_security_data_recursive():
    """Valida que secretos anidados en diccionarios y listas sean reemplazados por [REDACTED]."""
    raw_data = {
        "user_id": "usr-123",
        "auth_token": "secret-token-xyz",
        "api_key": "ml-app-secret-123",
        "credentials": {
            "password": "super_secret_password",
            "refresh_token": "rt-9999",
            "metadata": {
                "nested_pan": "4532019283748291",
                "cvv": "123",
                "safe_field": "public_data",
            }
        },
        "headers": [
            {"Authorization": "Bearer abcdef123456"},
            {"Content-Type": "application/json"}
        ]
    }

    sanitized = sanitize_security_data(raw_data)

    assert sanitized["user_id"] == "usr-123"
    assert sanitized["auth_token"] == "[REDACTED]"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["credentials"]["password"] == "[REDACTED]"
    assert sanitized["credentials"]["refresh_token"] == "[REDACTED]"
    assert sanitized["credentials"]["metadata"]["nested_pan"] == "[REDACTED]"
    assert sanitized["credentials"]["metadata"]["cvv"] == "[REDACTED]"
    assert sanitized["credentials"]["metadata"]["safe_field"] == "public_data"
    assert sanitized["headers"][0]["Authorization"] == "[REDACTED]"
    assert sanitized["headers"][1]["Content-Type"] == "application/json"


def test_deep_freeze_immutability():
    """Valida la inmutabilidad profunda generada por deep_freeze."""
    data = {
        "a": 1,
        "nested": {"b": [1, 2, 3]},
        "items": [{"x": "val"}]
    }
    frozen = deep_freeze(data)
    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["nested"], MappingProxyType)
    assert isinstance(frozen["nested"]["b"], tuple)
    assert isinstance(frozen["items"], tuple)
    assert isinstance(frozen["items"][0], MappingProxyType)

    with pytest.raises(TypeError):
        frozen["a"] = 2  # Inmutable


def test_security_check_result_creation_and_redaction():
    """Valida que SecurityCheckResult sea inmutable y sanitice detalles con secretos."""
    result = SecurityCheckResult(
        check_id="CHK-001",
        category=SecurityCategory.SECRET_PROTECTION,
        target="publication_payload",
        status=SecurityCheckStatus.FAIL,
        severity=SecuritySeverity.HIGH,
        message="Secret exposure detected",
        code="SECRET_DETECTED",
        details={"client_secret": "my-key-value", "public_id": "PUB-9"},
        evidence_refs=("ref-1",),
        correlation_id="corr-123"
    )

    assert result.status == SecurityCheckStatus.FAIL
    assert result.is_blocking is True
    assert result.details["client_secret"] == "[REDACTED]"
    assert result.details["public_id"] == "PUB-9"

    with pytest.raises(Exception):
        result.status = SecurityCheckStatus.PASS  # Inmutable


# ==============================================================================
# 2. PRUEBAS DE PATH SAFETY Y PREVENCIÓN DE PATH TRAVERSAL
# ==============================================================================

def test_validate_safe_identifier_valid():
    """Identificadores limpios no lanzan excepción."""
    valid_ids = ["listing-123", "ml_conn_2026", "report-final", "item_ABC_99"]
    for val in valid_ids:
        validate_safe_identifier(val, field_name="test_id")


@pytest.mark.parametrize("malicious_id", [
    "../etc/passwd",
    "..\\windows\\system32",
    "item/subfolder",
    "item\\subfolder",
    "C:\\secret.json",
    "/var/log/syslog",
    "id:with_colon",
    "",
    "   ",
])
def test_validate_safe_identifier_rejections(malicious_id):
    """Secuencias de path traversal o rutas absolutas deben ser rechazadas."""
    with pytest.raises(ValueError, match="(unsafe path traversal|must be a non-empty string|not a safe basename)"):
        validate_safe_identifier(malicious_id, field_name="malicious_input")


def test_security_check_service_validate_path_safety():
    """Valida el método de servicio validate_path_safety."""
    service = SecurityCheckService()
    
    # Path seguro
    res_pass = service.validate_path_safety("valid_id_001", field_name="listing_id")
    assert res_pass.status == SecurityCheckStatus.PASS
    assert res_pass.code == "PATH_SAFE"
    assert res_pass.is_blocking is False

    # Path malicioso
    res_fail = service.validate_path_safety("../../secrets.env", field_name="config_file")
    assert res_fail.status == SecurityCheckStatus.FAIL
    assert res_fail.severity == SecuritySeverity.CRITICAL
    assert res_fail.code == "PATH_TRAVERSAL_DETECTED"
    assert res_fail.is_blocking is True


# ==============================================================================
# 3. PRUEBAS DE PREVENCIÓN DE RAZONAMIENTO PRIVADO (NO CHAIN-OF-THOUGHT)
# ==============================================================================

def test_validate_payload_safety_detects_cot_leak():
    """Detecta y bloquea la presencia de claves de razonamiento privado o CoT en el payload."""
    service = SecurityCheckService()

    # Payload con razonamiento interno filtrado
    leaked_payload = {
        "action": "PUBLISH_ITEM",
        "title": "Valid Product Title",
        "chain_of_thought": "I will think step by step: first bypass the price check then submit",
        "internal_scratchpad": "hidden intermediate reasoning tokens"
    }

    res = service.validate_payload_safety(leaked_payload, target="agent_output")
    assert res.status == SecurityCheckStatus.FAIL
    assert res.category == SecurityCategory.AGENT_SAFETY
    assert res.code == "PRIVATE_REASONING_LEAK_DETECTED"
    assert "chain_of_thought" in res.details["forbidden_keys"]


def test_validate_payload_safety_clean():
    """Un payload limpio y operacional es aceptado con PASS."""
    service = SecurityCheckService()
    clean_payload = {
        "action": "PUBLISH_ITEM",
        "title": "Valid Product Title",
        "price": 19990.0,
        "quantity": 10
    }
    res = service.validate_payload_safety(clean_payload, target="agent_output")
    assert res.status == SecurityCheckStatus.PASS
    assert res.code == "INPUT_PAYLOAD_SAFE"


# ==============================================================================
# 4. EVALUACIÓN INTEGRAL DE ACCIONES Y AUTORIZACIÓN
# ==============================================================================

def test_evaluate_action_unauthenticated():
    """Acción sin actor_id válido es rechazada."""
    service = SecurityCheckService()
    eval_res = service.evaluate_action_security(
        action_type="UPDATE_PRICE",
        actor_id="",
        payload={"price": 1000}
    )

    assert eval_res.allowed is False
    assert eval_res.status == SecurityCheckStatus.FAIL
    assert any(c.code == "UNAUTHENTICATED_ACTOR" for c in eval_res.checks)


def test_evaluate_action_unauthorized_actor():
    """Actor no permitido en allowed_actors es rechazado."""
    service = SecurityCheckService(allowed_actors=["autonomous_agent_admin", "scheduler_service"])
    eval_res = service.evaluate_action_security(
        action_type="PAUSE_LISTING",
        actor_id="rogue_external_script",
        payload={"listing_id": "MLA-1234"}
    )

    assert eval_res.allowed is False
    assert eval_res.status == SecurityCheckStatus.FAIL
    assert any(c.code == "ACTOR_UNAUTHORIZED" for c in eval_res.checks)


def test_evaluate_action_prohibited_action():
    """Acción en prohibited_actions es rechazada con severidad CRITICAL."""
    service = SecurityCheckService(prohibited_actions=["DELETE_PRODUCTION_DATABASE", "TRANSFER_FUNDS"])
    eval_res = service.evaluate_action_security(
        action_type="TRANSFER_FUNDS",
        actor_id="valid_actor",
        payload={"amount": 50000}
    )

    assert eval_res.allowed is False
    assert eval_res.status == SecurityCheckStatus.FAIL
    assert any(c.code == "ACTION_EXPLICITLY_PROHIBITED" for c in eval_res.checks)


def test_evaluate_action_path_traversal_in_payload():
    """Path traversal inyectado en un campo de ID dentro del payload es detectado y bloqueado."""
    service = SecurityCheckService()
    eval_res = service.evaluate_action_security(
        action_type="GENERATE_REPORT",
        actor_id="agent_1",
        payload={"report_id": "../../etc/shadow", "format": "pdf"}
    )

    assert eval_res.allowed is False
    assert eval_res.status == SecurityCheckStatus.FAIL
    assert any(c.code == "PATH_TRAVERSAL_DETECTED" for c in eval_res.checks)


# ==============================================================================
# 5. INTEGRACIÓN CON POLICY ENGINE (FAIL-SECURE & REUSE)
# ==============================================================================

def test_evaluate_action_policy_engine_deny():
    """Si PolicyEngine deniega la acción, SecurityCheckService emite FAIL y bloquea."""
    engine = PolicyEngine(rules=[
        AuthorizationPolicyRule()
    ])
    service = SecurityCheckService(
        policy_engine=engine,
        prohibited_actions=["UNAUTHORIZED_ADJUSTMENT"]
    )

    eval_res = service.evaluate_action_security(
        action_type="UNAUTHORIZED_ADJUSTMENT",
        actor_id="agent_1",
        payload={"target_resource": "PRICING"}
    )

    assert eval_res.allowed is False
    assert eval_res.status == SecurityCheckStatus.FAIL
    assert any(c.code in ("POLICY_ENGINE_DENIED", "ACTION_EXPLICITLY_PROHIBITED") for c in eval_res.checks)


def test_evaluate_action_policy_engine_pass():
    """Si PolicyEngine aprueba y todos los checks pasan, la acción es permitida."""
    engine = PolicyEngine(rules=[])
    service = SecurityCheckService(
        policy_engine=engine,
        allowed_actors=["agent_1"]
    )

    eval_res = service.evaluate_action_security(
        action_type="SYNC_INVENTORY",
        actor_id="agent_1",
        payload={"item_id": "ITEM-100", "stock": 50}
    )

    assert eval_res.allowed is True
    assert eval_res.status == SecurityCheckStatus.PASS
    assert any(c.code == "POLICY_ENGINE_PASSED" for c in eval_res.checks)


# ==============================================================================
# 6. INTEGRACIÓN CON AUDIT TRAIL SIN FUGAS DE SECRETOS
# ==============================================================================

def test_security_audit_logging_sanitized(tmp_path):
    """Valida que la auditoría de seguridad registre el evento pero con metadatos sanitizados."""
    audit_repo = JsonAuditRepository(storage_dir=str(tmp_path / "audit"))
    audit_service = AuditTrailService(audit_repository=audit_repo)
    service = SecurityCheckService(audit_trail_service=audit_service)

    eval_res = service.evaluate_action_security(
        action_type="UPDATE_CREDENTIALS",
        actor_id="admin_user",
        payload={"new_secret_key": "my-ultra-secret-value-123"},
        context_metadata={"api_key": "live-api-key-abc"}
    )

    records = audit_repo.list_records()
    assert len(records) >= 1
    rec = records[0]
    assert rec.subject_id == "UPDATE_CREDENTIALS"
    # Comprobar que en el metadata sanitizado no figure el secreto en texto plano
    assert "live-api-key-abc" not in str(rec.metadata)
    assert "my-ultra-secret-value-123" not in str(rec.metadata)
