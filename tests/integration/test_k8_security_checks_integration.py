"""
Tests de Integración y E2E para Chequeos de Seguridad Transversal (Hito K.8).

Escenarios evaluados:
1. Unauthorized marketplace action -> Bloqueada antes de llamada o side-effect externo.
2. Policy DENY -> Cero side-effect externo en ejecución.
3. Inyección maliciosa de Path Traversal (../../etc/secret) en identificador de recurso -> Rechazado de inmediato.
4. Secreto inyectado en metadata / payload -> Sanitizado transversalmente a través de Audit Trail y Trace.
5. Manipulación/Corrupción de registro persistido -> Detección de integridad y rechazo seguro (Fail-secure).
6. Mismo idempotency key + payload alterado -> Detección de conflicto / rechazo (K.7 Reliability integration).
7. Payload de evento inválido en Event Bus -> Bloqueado sin ejecución de acciones.
8. Flujo E2E Transversal Seguro:
   - Flow A: Flujo válido y autorizado -> permitido y ejecutado.
   - Flow B: Flujo no autorizado / denegado por política -> bloqueado de forma estricta.
   - Flow C: Payload sensible o malicioso -> interceptado y sanitizado sin side-effects nocivos.
"""

import os
from pathlib import Path
from types import MappingProxyType
import pytest
from datetime import datetime, timezone

from src.domain.security.models import (
    SecurityCheckStatus,
    SecurityCategory,
    SecuritySeverity,
    SecurityCheckResult,
    SecurityCheckEvaluation,
    sanitize_security_data,
    validate_safe_identifier,
)
from src.application.security.security_check_service import SecurityCheckService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.audit.models import AuditRecordType, AuditActor, AuditActorType, AuditRecord
from src.application.audit.audit_trail_service import AuditTrailService
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
    CorruptedAuditRecordError,
)
from src.domain.reliability.models import (
    FailureCategory,
    FailureRecoverability,
    classify_failure,
    RetryPolicy,
)
from src.infrastructure.reliability.reliability_infrastructure import (
    JsonIdempotencyStore,
)
from src.domain.events.models import EventRecord, EventType
from src.domain.events.ports import EventHandlerPort
from src.application.events.event_bus_service import EventBusService
from src.infrastructure.persistence.data.json.event_store import JsonEventStore


# ==============================================================================
# INTEGRATION SCENARIO 1: UNAUTHORIZED MARKETPLACE ACTION BLOCKED BEFORE SIDE EFFECT
# ==============================================================================

def test_integration_unauthorized_marketplace_action_blocked():
    """Valida que un actor no autorizado sea bloqueado antes de ejecutar side-effects en marketplace."""
    called_external_api = []

    def mock_external_marketplace_api(payload):
        called_external_api.append(payload)
        return {"status": "SUCCESS"}

    service = SecurityCheckService(allowed_actors=["trusted_agent_1"])

    evaluation = service.evaluate_action_security(
        action_type="PUBLISH_MARKETPLACE_LISTING",
        actor_id="untrusted_script",
        payload={"title": "Test Item", "price": 1000}
    )

    if evaluation.allowed:
        mock_external_marketplace_api({"title": "Test Item"})

    assert evaluation.allowed is False
    assert evaluation.status == SecurityCheckStatus.FAIL
    assert len(called_external_api) == 0  # Cero side-effects


# ==============================================================================
# INTEGRATION SCENARIO 2: POLICY ENGINE DENY LEADS TO ZERO SIDE EFFECTS
# ==============================================================================

def test_integration_policy_engine_deny_zero_side_effect():
    """Valida que una decisión DENY de PolicyEngine impida side effects."""
    engine = PolicyEngine(rules=[AuthorizationPolicyRule()])
    service = SecurityCheckService(
        policy_engine=engine,
        prohibited_actions=["HIGH_RISK_LIQUIDATION"]
    )

    side_effect_executed = False

    eval_res = service.evaluate_action_security(
        action_type="HIGH_RISK_LIQUIDATION",
        actor_id="portfolio_manager",
        payload={"asset_id": "STK-99"}
    )

    if eval_res.allowed:
        side_effect_executed = True

    assert eval_res.allowed is False
    assert side_effect_executed is False
    assert any(c.status == SecurityCheckStatus.FAIL for c in eval_res.checks)


# ==============================================================================
# INTEGRATION SCENARIO 3: PATH TRAVERSAL ATTEMPT BLOCKED
# ==============================================================================

def test_integration_path_traversal_blocked():
    """Valida que intentos de path traversal sean interceptados inmediatamente."""
    service = SecurityCheckService()

    eval_res = service.evaluate_action_security(
        action_type="EXPORT_DATA",
        actor_id="data_exporter",
        payload={"filename": "../../../etc/shadow", "format": "csv"}
    )

    assert eval_res.allowed is False
    assert any(c.code == "PATH_TRAVERSAL_DETECTED" for c in eval_res.checks)


# ==============================================================================
# INTEGRATION SCENARIO 4: SECRET INJECTED INTO METADATA SANITIZED ACROSS AUDIT
# ==============================================================================

def test_integration_secret_injected_sanitized_in_audit(tmp_path):
    """Valida que secretos inyectados en la carga sean sanitizados en persistencia de auditoría."""
    audit_repo = JsonAuditRepository(storage_dir=str(tmp_path / "audit_repo"))
    audit_service = AuditTrailService(audit_repository=audit_repo)
    service = SecurityCheckService(audit_trail_service=audit_service)

    eval_res = service.evaluate_action_security(
        action_type="REFRESH_ML_TOKEN",
        actor_id="oauth_service",
        payload={"client_id": "app-123"},
        context_metadata={
            "auth_header": "Bearer secret_live_token_777",
            "refresh_token": "rt_live_secret_888",
            "safe_tag": "token_refresh_v1"
        }
    )

    records = audit_repo.list_records()
    assert len(records) >= 1
    rec = records[0]

    raw_metadata_str = str(rec.metadata)
    assert "secret_live_token_777" not in raw_metadata_str
    assert "rt_live_secret_888" not in raw_metadata_str
    assert "[REDACTED]" in raw_metadata_str
    assert rec.metadata.get("context_metadata", {}).get("safe_tag") == "token_refresh_v1"


# ==============================================================================
# INTEGRATION SCENARIO 5: TAMPERED PERSISTED RECORD DETECTED BY INTEGRITY
# ==============================================================================

def test_integration_tampered_audit_record_detected(tmp_path):
    """Valida que un registro persistido manipulado o corrupto sea detectado por checksum."""
    audit_repo = JsonAuditRepository(storage_dir=str(tmp_path / "audit_tamper"))
    rec = AuditRecord(
        audit_id="aud-tamper-01",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=datetime.now(timezone.utc),
        actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="integrity_test"),
        subject_type="ORDER",
        subject_id="ORD-100",
        action_or_operation="CONFIRM_PAYMENT",
        status="SUCCESS",
        correlation_id="corr-1",
        causation_id="cause-1",
        mission_id="mis-1",
        entity_reference="ORD-100",
        provenance="ORDER_SERVICE"
    )
    audit_repo.append(rec)

    # Manipular el archivo JSON directamente en disco (tampering)
    file_path = tmp_path / "audit_tamper" / "audit_records" / "aud-tamper-01.json"
    assert file_path.exists()
    content = file_path.read_text(encoding="utf-8")
    tampered_content = content.replace('"status": "SUCCESS"', '"status": "TAMPERED_STATUS"')
    file_path.write_text(tampered_content, encoding="utf-8")

    # Al recargar en una nueva instancia del repositorio, debe detectar la corrupción
    with pytest.raises(CorruptedAuditRecordError):
        JsonAuditRepository(storage_dir=str(tmp_path / "audit_tamper"))


# ==============================================================================
# INTEGRATION SCENARIO 6: IDEMPOTENCY KEY + ALTERED PAYLOAD -> CONFLICT
# ==============================================================================

def test_integration_idempotency_altered_payload_conflict(tmp_path):
    """Valida que reintentar una misma clave de idempotencia con payload alterado cause CONFLICT."""
    from src.application.reliability.reliability_engine import ReliabilityEngine

    store = JsonIdempotencyStore(storage_dir=str(tmp_path / "idempotency_store"))
    engine = ReliabilityEngine(idempotency_store=store)

    key = "idem-key-12345"
    payload_original = {"action": "UPDATE_PRICE", "price": 100.0}
    payload_altered = {"action": "UPDATE_PRICE", "price": 50.0}

    # Primer registro exitoso
    res1 = engine.execute_with_reliability(
        operation_id="op_1",
        operation_func=lambda: {"status": "UPDATED", "price": 100.0},
        is_side_effect=True,
        idempotency_key=key,
        payload=payload_original,
    )
    assert res1.is_success is True
    assert res1.output["price"] == 100.0

    # Mismo key + mismo payload -> Replay seguro y determinista (retorna resultado previo)
    res2 = engine.execute_with_reliability(
        operation_id="op_2",
        operation_func=lambda: {"status": "SHOULD_NOT_RUN"},
        is_side_effect=True,
        idempotency_key=key,
        payload=payload_original,
    )
    assert res2.is_success is True
    assert res2.output["price"] == 100.0

    # Mismo key + payload alterado -> Conflicto de Idempotencia bloqueante
    res3 = engine.execute_with_reliability(
        operation_id="op_3",
        operation_func=lambda: {"status": "SHOULD_NOT_RUN"},
        is_side_effect=True,
        idempotency_key=key,
        payload=payload_altered,
    )
    assert res3.is_success is False
    assert res3.status == "IDEMPOTENCY_CONFLICT"
    assert res3.failure_category == FailureCategory.CONFLICT


# ==============================================================================
# INTEGRATION SCENARIO 7: EVENT BUS UNKNOWN EVENT TYPE OR INVALID PAYLOAD
# ==============================================================================

def test_integration_event_bus_safety(tmp_path):
    """Valida que eventos malformados o con CoT sean detectados en el bus de eventos."""
    event_store = JsonEventStore(base_dir=tmp_path / "event_store")
    event_bus = EventBusService(event_store=event_store)
    processed_events = []

    class DummyHandler(EventHandlerPort):
        @property
        def handler_id(self) -> str:
            return "dummy_handler"

        def can_handle(self, event_type: EventType) -> bool:
            return event_type == EventType.CHANGE_DETECTED

        def handle(self, event: EventRecord) -> None:
            processed_events.append(event)

    event_bus.register_handler(DummyHandler())

    # Evento con CoT inyectado en metadata
    unsafe_event = EventRecord(
        event_id="evt-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="ORDER",
        subject_id="ORD-123",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-evt-1",
        payload={"order_id": "ORD-123", "chain_of_thought": "hidden reasoning to hijack execution"}
    )

    service = SecurityCheckService()
    payload_check = service.validate_payload_safety(unsafe_event.payload, target="event_payload")
    
    assert payload_check.status == SecurityCheckStatus.FAIL
    assert payload_check.code == "PRIVATE_REASONING_LEAK_DETECTED"

    # Si el check de seguridad falla, el evento NO debe ser despachado al bus
    if not payload_check.is_blocking:
        event_bus.publish(unsafe_event)

    assert len(processed_events) == 0


# ==============================================================================
# INTEGRATION SCENARIO 8: END-TO-END FLOW (SAFE VS DENIED VS MALICIOUS)
# ==============================================================================

def test_integration_e2e_security_pipeline(tmp_path):
    """
    E2E transversal:
    ContinuousMission -> Decision -> Policy -> Security Checks -> ActionExecutor -> Mock Boundary -> Audit
    """
    audit_repo = JsonAuditRepository(storage_dir=str(tmp_path / "e2e_audit"))
    audit_service = AuditTrailService(audit_repository=audit_repo)
    engine = PolicyEngine(rules=[AuthorizationPolicyRule()])
    
    security_service = SecurityCheckService(
        policy_engine=engine,
        audit_trail_service=audit_service,
        allowed_actors=["commerce_agent_v1"],
        prohibited_actions=["DELETE_DATABASE"]
    )

    # Case A: Flujo legítimo autorizado
    eval_a = security_service.evaluate_action_security(
        action_type="ADJUST_LISTING_PRICE",
        actor_id="commerce_agent_v1",
        payload={"listing_id": "MLA-900", "new_price": 25000.0, "mission_id": "MIS-E2E-1"}
    )
    assert eval_a.allowed is True
    assert eval_a.status == SecurityCheckStatus.PASS

    # Case B: Flujo no autorizado por política / prohibido
    eval_b = security_service.evaluate_action_security(
        action_type="DELETE_DATABASE",
        actor_id="commerce_agent_v1",
        payload={"mission_id": "MIS-E2E-2"}
    )
    assert eval_b.allowed is False
    assert eval_b.status == SecurityCheckStatus.FAIL

    # Case C: Flujo malicioso (Path Traversal + Secrets)
    eval_c = security_service.evaluate_action_security(
        action_type="FETCH_FILE",
        actor_id="commerce_agent_v1",
        payload={"file_path": "../../../root/key.pem", "api_key": "raw_exposed_key", "mission_id": "MIS-E2E-3"}
    )
    assert eval_c.allowed is False
    assert eval_c.status == SecurityCheckStatus.FAIL

    # Verificar que el Audit Trail haya registrado los 3 eventos sin filtrar secretos
    records = audit_repo.list_records()
    assert len(records) == 3
    for r in records:
        assert "raw_exposed_key" not in str(r.metadata)
