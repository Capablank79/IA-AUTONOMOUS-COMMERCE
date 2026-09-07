"""
Integration Tests for Hito N.9 — Sensitive Data Handling (Transversal N — Security, Governance & Safety).

Cubre los escenarios de integración A a J y el pipeline E2E multicapa:
- Escenario A: Order/customer payload -> classification -> audit receives redacted metadata only.
- Escenario B: LLM inference context -> unnecessary PII removed/redacted -> required business facts preserved.
- Escenario C: M.4 cache -> restricted data not persisted / safe fingerprint.
- Escenario D: N.8 allowed tool -> receives only permitted/minimized fields.
- Escenario E: MercadoLibre operation requiring customer/shipping field -> required transfer preserved by explicit policy.
- Escenario F: N.5 secret accidentally present in metadata -> redacted/no leak.
- Escenario G: nested payload -> recursive redaction.
- Escenario H: UNKNOWN classification -> fail-safe behavior.
- Escenario I: restart/persistence path -> JSON repository crash-safety and integrity validation.
- Escenario J: Audit/Trace safe -> K.1 integration without raw sensitive retention.
- E2E Pipeline: N.1 -> N.2 -> N.4 -> N.3 -> N.8 -> N.9 -> N.7 -> N.6 -> Delegate Execution.
"""

import os
import shutil
import tempfile
import pytest
from decimal import Decimal
from typing import Dict, Any

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.authentication.models import PrincipalContext, AuthenticationResult, AuthenticationMethod, AuthenticationStatus
from src.domain.authorization.models import AuthorizationDecision, AuthorizationStatus
from src.domain.tool_policy.models import ToolReference
from src.domain.tool.models import ToolSideEffectLevel

from src.domain.security.sensitive_data_models import (
    DataClassification,
    SensitiveCategory,
    DataHandlingPurpose,
    PersistenceHandlingMode,
    CacheHandlingMode,
    DataHandlingReasonCode,
    DataHandlingPolicy,
    DataHandlingRequest,
    DataHandlingDecision,
    compute_deterministic_fingerprint,
)
from src.domain.security.sensitive_data_engine import (
    DeterministicSensitiveDataClassifier,
    DeterministicSensitiveDataRedactor,
)
from src.infrastructure.persistence.data.json.sensitive_data_policy_repository import (
    JsonDataHandlingPolicyRepository,
)
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)
from src.application.rbac.rbac_service import RBACService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.application.approval.approval_policy_service import ApprovalPolicyService

from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.domain.audit.models import AuditRecordType


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.executed_decisions = []
        self.external_calls_count = 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.executed_decisions.append((decision, state))
        self.external_calls_count += 1
        return {
            "execution_status": "SUCCESS",
            "received_parameters": dict(decision.parameters) if decision.parameters else {},
        }


@pytest.fixture
def tmp_dir():
    temp_path = tempfile.mkdtemp(prefix="test_n9_")
    yield temp_path
    shutil.rmtree(temp_path, ignore_errors=True)


@pytest.fixture
def policy_repo(tmp_dir):
    repo_file = os.path.join(tmp_dir, "sensitive_policies.json")
    return JsonDataHandlingPolicyRepository(repo_file)


@pytest.fixture
def audit_repo(tmp_dir):
    audit_file = os.path.join(tmp_dir, "audit_trail.json")
    return JsonAuditRepository(audit_file)


@pytest.fixture
def sensitive_service(policy_repo, audit_repo):
    # Guardar política por defecto
    default_policy = DataHandlingPolicy(
        policy_name="default_sensitive_data_policy",
        version="1.0.0",
        description="Standard data handling policy",
        default_classification=DataClassification.INTERNAL,
        allowed_purposes=(
            DataHandlingPurpose.INFERENCE,
            DataHandlingPurpose.AUDIT,
            DataHandlingPurpose.MARKETPLACE_OPERATION,
            DataHandlingPurpose.ORDER_FULFILLMENT,
            DataHandlingPurpose.SUPPLIER_CONTACT,
            DataHandlingPurpose.CACHE,
            DataHandlingPurpose.LOGGING,
            DataHandlingPurpose.STORAGE,
            DataHandlingPurpose.GENERAL,
        ),
        logging_allowed_classes=(DataClassification.PUBLIC, DataClassification.INTERNAL),
        cache_allowed_classes=(DataClassification.PUBLIC, DataClassification.INTERNAL, DataClassification.CONFIDENTIAL),
        external_transfer_allowed_classes=(
            DataClassification.PUBLIC,
            DataClassification.INTERNAL,
            DataClassification.CONFIDENTIAL,
            DataClassification.SENSITIVE,
        ),
        persistence_mode=PersistenceHandlingMode.ALLOW,
        cache_mode=CacheHandlingMode.SANITIZED_ONLY,
    )
    policy_repo.save_policy(default_policy)
    return SensitiveDataHandlingService(
        policy_repository=policy_repo,
        audit_repository=audit_repo,
    )


# Escenario A: Order/customer payload -> classification -> audit receives redacted metadata only
def test_scenario_a_order_customer_payload_audit_receives_redacted_only(sensitive_service, audit_repo):
    order_payload = {
        "order_id": "ORD-778899",
        "buyer_name": "Roberto Fernandez",
        "buyer_email": "roberto.f@example.com",
        "shipping_address": "Moneda 1100, Santiago",
        "total_amount": 150.00,
    }

    sanitized_audit_data = sensitive_service.sanitize_for_audit(order_payload, correlation_id="corr_ord_778899")

    # Verificamos que los datos PII sensibles fueron redactados
    assert sanitized_audit_data["buyer_email"] == "ro***@example.com"
    assert sanitized_audit_data["shipping_address"] == "[REDACTED_ADDRESS]"
    assert sanitized_audit_data["order_id"] == "ORD-778899"
    assert sanitized_audit_data["total_amount"] == 150.00


# Escenario B: LLM inference context -> unnecessary PII removed/redacted -> required business facts preserved
def test_scenario_b_llm_inference_context_minimization(sensitive_service):
    llm_context = {
        "market_item_id": "MLA9918237",
        "competitor_price": 45.00,
        "our_cost": 30.00,
        "suggested_action": "REPRICE",
        "customer_internal_note": "Cliente Juan Perez (email: juan.p@test.com, fono: +56911223344) consultó por descuento",
        "private_prompt": "Do not disclose margin under 20%",
    }

    sanitized_for_inference = sensitive_service.sanitize_for_inference(
        prompt_or_context=llm_context,
        required_business_fields=("market_item_id", "competitor_price", "our_cost", "suggested_action"),
    )

    # Hechos de negocio preservados
    assert sanitized_for_inference["market_item_id"] == "MLA9918237"
    assert sanitized_for_inference["competitor_price"] == 45.00
    assert sanitized_for_inference["our_cost"] == 30.00
    assert sanitized_for_inference["suggested_action"] == "REPRICE"

    # PII no requerida minimizada
    assert sanitized_for_inference["customer_internal_note"] == "[MINIMIZED_FIELD]"


# Escenario C: M.4 cache -> restricted data not persisted
def test_scenario_c_cache_restricted_data_blocked(sensitive_service):
    restricted_auth_payload = {
        "access_token": "APP_USR-998877665544",
        "refresh_token": "TG-66554433",
        "card_number": "5500 0000 0000 1234",
    }

    cached_result = sensitive_service.sanitize_for_cache(restricted_auth_payload)
    assert cached_result is None


# Escenario D: N.8 allowed tool -> receives only permitted/minimized fields
def test_scenario_d_tool_receives_only_permitted_fields(sensitive_service):
    # Definir política específica para herramientas externas
    ml_tool_payload = {
        "order_id": "ORD-1234",
        "buyer_id": "BUY-555",
        "internal_margin_pct": 28.5,
        "supplier_cost": 15.00,
        "tracking_code": "TRK987654321",
    }

    req = DataHandlingRequest(
        payload=ml_tool_payload,
        purpose=DataHandlingPurpose.MARKETPLACE_OPERATION,
        destination="mercadolibre_shipping_tool",
        required_fields=("order_id", "tracking_code"),
    )

    decision = sensitive_service.evaluate(req)
    assert decision.is_allowed_for_purpose is True

    sanitized = decision.redaction_result.sanitized_payload
    assert sanitized["order_id"] == "ORD-1234"
    assert sanitized["tracking_code"] == "TRK987654321"
    assert sanitized["internal_margin_pct"] == "[MINIMIZED_FIELD]"
    assert sanitized["supplier_cost"] == "[MINIMIZED_FIELD]"


# Escenario E: MercadoLibre operation requiring customer/shipping field -> preserved by explicit policy
def test_scenario_e_marketplace_shipping_field_preserved(policy_repo, sensitive_service):
    ml_shipping_policy = DataHandlingPolicy(
        policy_name="ml_shipping_fulfillment_policy",
        version="1.0.0",
        allowed_purposes=(DataHandlingPurpose.ORDER_FULFILLMENT, DataHandlingPurpose.MARKETPLACE_OPERATION),
        allowed_fields_override_by_purpose={
            "ORDER_FULFILLMENT": ("receiver_name", "shipping_address", "order_id", "city"),
        },
    )
    policy_repo.save_policy(ml_shipping_policy)

    shipping_payload = {
        "order_id": "ORD-999",
        "receiver_name": "Ana Torres",
        "shipping_address": "Providencia 1234",
        "city": "Santiago",
        "credit_card": "4111 2222 3333 4444",
    }

    req = DataHandlingRequest(
        payload=shipping_payload,
        purpose=DataHandlingPurpose.ORDER_FULFILLMENT,
        policy_name="ml_shipping_fulfillment_policy",
    )

    decision = sensitive_service.evaluate(req)
    assert decision.is_allowed_for_purpose is True

    sanitized = decision.redaction_result.sanitized_payload
    assert sanitized["order_id"] == "ORD-999"
    assert sanitized["receiver_name"] == "Ana Torres"
    assert sanitized["shipping_address"] == "Providencia 1234"
    assert sanitized["city"] == "Santiago"
    # Credit card no está en allowed fields de shipping -> minimizado/redactado
    assert sanitized["credit_card"] == "[MINIMIZED_FIELD]"


# Escenario F: N.5 secret accidentally present in metadata -> redacted/no leak
def test_scenario_f_secret_accidentally_in_metadata(sensitive_service):
    dirty_metadata = {
        "action": "SYNC_CATALOG",
        "secret_api_key": "AKIAIOSFODNN7EXAMPLE",
        "password": "SuperSecretPassword123!",
        "item_count": 42,
    }

    sanitized = sensitive_service.sanitize_for_audit(dirty_metadata)
    assert sanitized["secret_api_key"] == "[REDACTED_SECRET]"
    assert sanitized["password"] == "[REDACTED_SECRET]"
    assert sanitized["item_count"] == 42


# Escenario G: nested payload -> recursive redaction
def test_scenario_g_deeply_nested_payload_redaction(sensitive_service):
    deep_data = {
        "level1": {
            "level2": [
                {"email": "contact1@domain.com"},
                {"email": "contact2@domain.com", "phone": "+56911223344"},
            ],
            "financials": {
                "accounts": [
                    {"bank_account": "1234567890"},
                ]
            }
        }
    }

    sanitized = sensitive_service.sanitize_for_audit(deep_data)
    assert sanitized["level1"]["level2"][0]["email"] == "co***@domain.com"
    assert sanitized["level1"]["level2"][1]["phone"] == "******3344"
    assert sanitized["level1"]["financials"]["accounts"][0]["bank_account"] == "[REDACTED_FINANCIAL]"


# Escenario H: UNKNOWN classification -> fail-safe behavior
def test_scenario_h_unknown_classification_fail_safe(sensitive_service):
    req = DataHandlingRequest(
        payload={"opaque_field": 123},
        policy_name="non_existent_policy_xyz",
    )
    decision = sensitive_service.evaluate(req)

    assert decision.is_allowed_for_purpose is False
    assert decision.reason_code == DataHandlingReasonCode.POLICY_NOT_FOUND
    assert decision.persistence_mode == PersistenceHandlingMode.DENY
    assert decision.cache_mode == CacheHandlingMode.DENY


# Escenario I: restart/persistence path -> JSON repository crash-safety and integrity validation
def test_scenario_i_persistence_restart_and_integrity(tmp_dir):
    repo_file = os.path.join(tmp_dir, "restarted_policies.json")
    repo1 = JsonDataHandlingPolicyRepository(repo_file)

    pol = DataHandlingPolicy(
        policy_name="persistent_policy_v1",
        version="1.0.0",
        default_classification=DataClassification.CONFIDENTIAL,
    )
    repo1.save_policy(pol)

    # Simular reinicio creando nueva instancia del repositorio
    repo2 = JsonDataHandlingPolicyRepository(repo_file)
    loaded_pol = repo2.get_policy("persistent_policy_v1")

    assert loaded_pol is not None
    assert loaded_pol.policy_name == "persistent_policy_v1"
    assert loaded_pol.verify_integrity() is True
    assert loaded_pol.default_classification == DataClassification.CONFIDENTIAL


# Escenario J: Audit/Trace safe -> K.1 integration without raw sensitive retention
def test_scenario_j_audit_repository_integration_safe(sensitive_service, audit_repo):
    req = DataHandlingRequest(
        payload={"buyer_email": "test.audit@domain.com", "dni": "12.345.678-9"},
        purpose=DataHandlingPurpose.GENERAL,
        correlation_id="corr_audit_test_123",
    )
    dec = sensitive_service.evaluate(req)

    # Verificar que el registro en K.1 no contiene el email o DNI en texto claro
    all_audit_records = audit_repo.list_records()
    assert len(all_audit_records) >= 1
    latest_record = all_audit_records[-1]

    assert latest_record.record_type == AuditRecordType.POLICY_EVALUATED
    # Comprobar que en metadata no existe el email sin enmascarar
    metadata_str = str(latest_record.metadata)
    assert "test.audit@domain.com" not in metadata_str
    assert "12.345.678-9" not in metadata_str


# E2E Pipeline Multicapa: N.1 -> N.2 -> N.4 -> N.3 -> N.8 -> N.9 -> N.7 -> N.6 -> Delegate
def test_e2e_security_pipeline_with_n9_sanitization(policy_repo, sensitive_service):
    # 1. Configurar Mock Delegate
    delegate = MockActionExecutor()

    # 2. Configurar N.3 Authorization
    authz_service = AuthorizationService()

    # 3. Contexto de Principal (N.1 + N.2)
    identity_ref = IdentityReference(
        identity_id="agent_merchant_01",
        identity_type=IdentityType.AGENT,
        canonical_identifier="agent:internal:agent_merchant_01",
        display_name="Merchant Agent",
    )
    auth_res = AuthenticationResult(
        principal=identity_ref,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal_api",
        status=AuthenticationStatus.AUTHENTICATED,
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    # Configurar política explícita para la tool/operación que enmascara o minimiza email
    order_policy = DataHandlingPolicy(
        policy_name="order_processing_policy",
        version="1.0.0",
        allowed_purposes=(DataHandlingPurpose.MARKETPLACE_OPERATION, DataHandlingPurpose.ORDER_FULFILLMENT),
        allowed_fields_override_by_purpose={
            "MARKETPLACE_OPERATION": ("order_id", "item_quantity", "action_type"),
        },
    )
    policy_repo.save_policy(order_policy)

    # 4. Configurar Guardián con N.3, N.8, N.9, N.7
    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("UPDATE_INVENTORY", "PUBLISH_ITEM", "PROCESS_ORDER"),
        sensitive_data_service=sensitive_service,
        default_sensitive_data_policy_name="order_processing_policy",
    )

    # Caso 1: Acción permitida con payload que contiene PII y credenciales accidentales
    state = LoopState(
        mission_id="mission_e2e_001",
        iteration=1,
        goal="Process orders safely without leaking sensitive data",
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_api",
        parameters={
            "action_type": "PROCESS_ORDER",
            "order_id": "ORD-555444",
            "buyer_email": "buyer.e2e@example.com",
            "password": "leaked_plain_password",
            "item_quantity": 2,
        },
        reason="Process fulfilled customer order",
    )

    exec_result = guarded_executor.execute(decision, state)

    assert exec_result["is_allowed"] is True
    assert exec_result["is_executable"] is True
    assert exec_result["execution_status"] == "SUCCESS"
    assert "sensitive_data_decision_id" in exec_result

    # Verificar que el delegate físico recibió el payload sanitizado por N.9
    received_params = delegate.executed_decisions[-1][0].parameters
    assert received_params["password"] == "[REDACTED_SECRET]"
    assert received_params["buyer_email"] == "[MINIMIZED_FIELD]"
    assert received_params["order_id"] == "ORD-555444"
    assert received_params["item_quantity"] == 2
