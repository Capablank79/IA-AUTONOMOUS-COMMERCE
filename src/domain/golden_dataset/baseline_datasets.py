"""
Datasets Golden canónicos y de referencia para evaluación del sistema (Hito K.5).

Define fábricas de datasets y casos canónicos para:
1. GOVERNANCE & POLICY: Casos de evaluación deterministas de PolicyEngine (ALLOW, DENY, APPROVAL_REQUIRED).
2. UNKNOWN SAFETY: Casos donde UNKNOWN es el comportamiento correcto y seguro ante incertidumbre o fallos de fuente.
3. IDEMPOTENCY & REPLAY: Casos de deduplicación y preservación de resultado único ante replays concurrentes o repetidos.
4. CONTINUOUS AUTONOMY & RESTART: Casos de ciclo continuo J.7 / Gate I con verificación de invariantes y recuperación durable.
5. SECURITY & SANITIZATION: Casos de exclusión y redacción recursiva de secretos y credenciales.

Principios:
- Basados EXCLUSIVAMENTE en contratos y comportamientos ya validados en el sistema.
- Cero fabricación de respuestas comerciales ("producto ganador", "profit arbitrario").
- Reutilizan EvaluationCase de K.4.
- Totalmente reproducibles y versionados.
"""

from typing import List, Tuple
from src.domain.evaluation.models import EvaluationCase, EvaluationType
from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetStatus,
    GoldenDatasetProvenance,
    GoldenDatasetCurator,
    GoldenDatasetCuratorType,
)


def get_governance_policy_baseline_cases() -> List[EvaluationCase]:
    """
    Retorna casos canónicos para evaluar el PolicyEngine del sistema.
    """
    return [
        EvaluationCase(
            case_id="case_gov_allow_small_order",
            name="Governance Policy - Small Safe Order ALLOW",
            description="Evaluates that an operational order within daily budget and valid supplier is allowed.",
            evaluation_type=EvaluationType.POLICY,
            input_reference={
                "action_type": "PLACE_SUPPLIER_ORDER",
                "estimated_amount": 50.0,
                "daily_budget_limit": 500.0,
                "supplier_verified": True,
            },
            expected_criteria={
                "expected_decision": "ALLOW",
                "required_violations": [],
            },
            tags=("governance", "policy", "allow", "p0"),
            version="1.0.0",
            provenance="VALIDATED_GATE_H_POLICY_SPEC",
            metadata={"domain": "governance"},
        ),
        EvaluationCase(
            case_id="case_gov_deny_budget_exceeded",
            name="Governance Policy - Budget Exceeded DENY",
            description="Evaluates that an order exceeding max budget threshold is strictly denied.",
            evaluation_type=EvaluationType.POLICY,
            input_reference={
                "action_type": "PLACE_SUPPLIER_ORDER",
                "estimated_amount": 50000.0,
                "daily_budget_limit": 500.0,
                "supplier_verified": True,
            },
            expected_criteria={
                "expected_decision": "DENY",
                "required_violations": ["BUDGET_EXCEEDED"],
            },
            tags=("governance", "policy", "deny", "p0"),
            version="1.0.0",
            provenance="VALIDATED_GATE_H_POLICY_SPEC",
            metadata={"domain": "governance"},
        ),
        EvaluationCase(
            case_id="case_gov_deny_unverified_supplier",
            name="Governance Policy - Unverified Supplier DENY",
            description="Evaluates that orders to unverified suppliers are blocked by policy.",
            evaluation_type=EvaluationType.POLICY,
            input_reference={
                "action_type": "PLACE_SUPPLIER_ORDER",
                "estimated_amount": 100.0,
                "daily_budget_limit": 500.0,
                "supplier_verified": False,
            },
            expected_criteria={
                "expected_decision": "DENY",
                "required_violations": ["UNVERIFIED_SUPPLIER"],
            },
            tags=("governance", "policy", "supplier_safety"),
            version="1.0.0",
            provenance="VALIDATED_GATE_H_POLICY_SPEC",
            metadata={"domain": "governance"},
        ),
    ]


def get_unknown_safety_baseline_cases() -> List[EvaluationCase]:
    """
    Retorna casos canónicos donde la respuesta esperada es explícitamente UNKNOWN para preservar seguridad epistémica.
    """
    return [
        EvaluationCase(
            case_id="case_unk_missing_market_price",
            name="Market Safety - Missing Price Produces UNKNOWN",
            description="When market snapshot lacks price data, system must emit UNKNOWN status instead of guessing.",
            evaluation_type=EvaluationType.SAFETY,
            input_reference={
                "sku": "SKU-UNKNOWN-TEST",
                "source": "EXTERNAL_API",
                "price": None,
                "stock": 10,
            },
            expected_criteria={
                "expected_safety_status": "PASS",
                "prohibited_values": ["0.00", "SUCCESS", "DEFAULT_PRICE"],
                "required_status": "UNKNOWN",
            },
            tags=("unknown", "safety", "market_monitoring", "p0"),
            version="1.0.0",
            provenance="VALIDATED_GATE_I_UNKNOWN_SPEC",
            metadata={"domain": "safety"},
        ),
        EvaluationCase(
            case_id="case_unk_corrupted_source_stream",
            name="Source Safety - Corrupted Stream Emits UNKNOWN",
            description="When data source fails or delivers unparseable content, evaluation and pipeline must register UNKNOWN.",
            evaluation_type=EvaluationType.STATUS,
            input_reference={
                "stream_status": "CORRUPTED",
                "data_payload": None,
            },
            expected_criteria={
                "expected_status": "UNKNOWN",
            },
            tags=("unknown", "safety", "source_handling"),
            version="1.0.0",
            provenance="VALIDATED_GATE_I_UNKNOWN_SPEC",
            metadata={"domain": "safety"},
        ),
    ]


def get_idempotency_baseline_cases() -> List[EvaluationCase]:
    """
    Retorna casos canónicos para verificar deduplicación e idempotencia estricta.
    """
    return [
        EvaluationCase(
            case_id="case_idemp_duplicate_event_execution",
            name="Idempotency - Duplicate Event Ingestion Produces Single Effect",
            description="Verifies that reprocessing the exact same event payload results in duplicate detection and identical logical effect.",
            evaluation_type=EvaluationType.IDEMPOTENCY,
            input_reference={
                "event_id": "evt-idemp-golden-001",
                "event_type": "MARKET_OBSERVATION_CREATED",
                "payload_hash": "sha256_mock_hash_123",
                "invocations_count": 2,
            },
            expected_criteria={
                "match": True,
            },
            tags=("idempotency", "event_bus", "replay", "p0"),
            version="1.0.0",
            provenance="VALIDATED_GATE_I_IDEMPOTENCY_SPEC",
            metadata={"domain": "reliability"},
        )
    ]


def get_continuous_autonomy_baseline_cases() -> List[EvaluationCase]:
    """
    Retorna casos canónicos para verificar ciclos continuos y recuperación tras reinicio.
    """
    return [
        EvaluationCase(
            case_id="case_autonomy_cycle_max_limit",
            name="Continuous Autonomy - Max Cycles Enforcement",
            description="Verifies continuous mission halts cleanly when reaching max_cycles parameter.",
            evaluation_type=EvaluationType.END_TO_END,
            input_reference={
                "mission_id": "continuous_mission_golden_001",
                "max_cycles": 2,
                "target_cycles_to_run": 5,
            },
            expected_criteria={
                "expected_final_status": "COMPLETED",
                "expected_executed_cycles": 2,
            },
            tags=("continuous_mission", "autonomy", "lifecycle", "p0"),
            version="1.0.0",
            provenance="VALIDATED_GATE_I_CONTINUOUS_MISSION_SPEC",
            metadata={"domain": "continuous_autonomy"},
        )
    ]


def get_security_sanitization_baseline_cases() -> List[EvaluationCase]:
    """
    Retorna casos canónicos para verificar exclusión y sanitización estricta de secretos.
    """
    return [
        EvaluationCase(
            case_id="case_sec_secret_redaction",
            name="Security - Secret Keys Sanitization",
            description="Verifies sensitive fields (api_key, password, token) are redacted before storage or evaluation.",
            evaluation_type=EvaluationType.SAFETY,
            input_reference={
                "api_key": "sk-live-super-secret-12345",
                "password": "my_password_xyz",
                "token": "bearer-token-abc",
                "public_sku": "SKU-999",
            },
            expected_criteria={
                "prohibited_values": ["sk-live-super-secret-12345", "my_password_xyz", "bearer-token-abc"],
                "expected_safety_status": "PASS",
            },
            tags=("security", "sanitization", "redaction", "p0"),
            version="1.0.0",
            provenance="VALIDATED_SECURITY_SPEC",
            metadata={"domain": "security"},
        )
    ]
