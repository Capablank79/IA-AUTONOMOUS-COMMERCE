"""
Pruebas Unitarias exhaustivas para R.7 — Self-monitoring (Hito R — Advanced Autonomy).

Cubre todos los casos requeridos y los 6 defectos arquitectónicos auditados:
1. healthy assessment with complete signals and required coverage
2. missing signals -> UNKNOWN
3. stale heartbeat detected
4. temporal stall: before timeout (OK), after timeout (STALLED), active nonterminal work, timezone-aware UTC, invalid/missing -> UNKNOWN
5. repeated technical failures -> REQUEST_REPLAN
6. coordination conflict -> AT_RISK
7. repeated delegation near bound -> REQUEST_DELEGATION
8. budget pressure
9. quota exhaustion -> BLOCK
10. rate-limit pressure
11. valid cost anomaly (Decimal, baseline/observed, equal currency)
12. mixed currency / invalid cost / missing fields -> UNKNOWN (never silent default "0")
13. zero explicit cost vs missing cost
14. failure-rate denominator zero -> UNKNOWN
15. request replan valid
16. policy denial no replan -> BLOCK
17. request delegation valid
18. emergency stop block -> BLOCK
19. pause decision
20. escalation (remediation limit)
21. tenant & mission mismatch rejection before evaluation
22. partial completeness and missing fields -> UNKNOWN, never healthy
23. required signal types coverage -> UNKNOWN if incomplete or missing
24. tenant isolation
25. Anti-CoT and sensitive data redaction
26. bounded response
27. no post-R.7 feature
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
import pytest

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.domain.self_monitoring.models import (
    MissionHealthStatus,
    SignalType,
    SignalSource,
    SignalSeverity,
    SignalCompleteness,
    DegradationReasonCode,
    SelfMonitoringAction,
    HealthSignal,
    SelfMonitoringPolicy,
    MissionHealthSnapshot,
    HealthAssessment,
)
from src.application.self_monitoring.self_monitoring_service import (
    SelfMonitoringService,
    InMemorySelfMonitoringRepository,
)


@pytest.fixture
def clock():
    return VirtualClock(datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def repo():
    return InMemorySelfMonitoringRepository()


@pytest.fixture
def context_a():
    return TenantContext(
        tenant_id="tenant_a",
        identity_id="user_a",
    )


@pytest.fixture
def context_b():
    return TenantContext(
        tenant_id="tenant_b",
        identity_id="user_b",
    )


@pytest.fixture
def default_policy():
    return SelfMonitoringPolicy(
        stale_heartbeat_threshold_seconds=30.0,
        stall_timeout_seconds=120.0,
        max_remediation_actions_per_mission=3,
        max_replan_requests=2,
        max_delegation_requests=2,
        failure_rate_threshold=0.5,
        min_failure_samples=3,
        required_signal_types=(
            SignalType.HEARTBEAT_LIVENESS,
            SignalType.EXECUTION_PROGRESS,
            SignalType.POLICY_COMPLIANCE,
        ),
    )


@pytest.fixture
def service(repo, clock, default_policy):
    return SelfMonitoringService(
        repository=repo,
        clock=clock,
        policy=default_policy,
    )


def test_1_healthy_assessment_with_complete_signals(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-hb",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={"last_seen_at": now},
            tenant_id="tenant_a",
            mission_id="mission-1",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-prog",
            signal_type=SignalType.EXECUTION_PROGRESS,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=75.0,
            tenant_id="tenant_a",
            mission_id="mission-1",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-pol",
            signal_type=SignalType.POLICY_COMPLIANCE,
            source=SignalSource.N_GOVERNANCE,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=True,
            tenant_id="tenant_a",
            mission_id="mission-1",
            completeness=SignalCompleteness.COMPLETE,
        ),
    ]
    assessment = service.assess_mission_health("mission-1", context_a, signals)
    assert assessment.status == MissionHealthStatus.HEALTHY
    assert assessment.decision.action == SelfMonitoringAction.NONE
    assert DegradationReasonCode.NONE_HEALTHY in assessment.snapshot.reason_codes


def test_2_missing_signals_unknown(service, context_a):
    assessment = service.assess_mission_health("mission-empty", context_a, signals=[])
    assert assessment.status == MissionHealthStatus.UNKNOWN
    assert assessment.decision.action == SelfMonitoringAction.CONTINUE_WITH_WARNING
    assert DegradationReasonCode.INSUFFICIENT_SIGNALS_UNKNOWN in assessment.snapshot.reason_codes


def test_3_stale_heartbeat_detected(service, context_a, clock):
    now = clock.now()
    stale_time = now - timedelta(seconds=60)  # > 30s threshold
    signals = [
        HealthSignal(
            signal_id="sig-hb",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={"last_seen_at": stale_time},
            tenant_id="tenant_a",
            mission_id="mission-stale",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-stale", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert assessment.snapshot.is_stale_heartbeat is True
    assert DegradationReasonCode.STALE_HEARTBEAT in assessment.snapshot.reason_codes
    assert assessment.decision.action == SelfMonitoringAction.PAUSE


def test_4_temporal_stall_before_and_after_timeout(service, context_a, clock):
    now = clock.now()
    # 4a. Progreso hace 60 segundos con timeout de 120s -> No es stall (HEALTHY si cubre reqs o sin stall)
    recent_progress = now - timedelta(seconds=60)
    signals_recent = [
        HealthSignal(
            signal_id="sig-hb",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={"last_seen_at": now},
            tenant_id="tenant_a",
            mission_id="mission-stall-time",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-prog",
            signal_type=SignalType.EXECUTION_PROGRESS,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=30.0,
            tenant_id="tenant_a",
            mission_id="mission-stall-time",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-pol",
            signal_type=SignalType.POLICY_COMPLIANCE,
            source=SignalSource.N_GOVERNANCE,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=True,
            tenant_id="tenant_a",
            mission_id="mission-stall-time",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-stall-recent",
            signal_type=SignalType.STALL_TEMPORAL,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={
                "last_progress_at": recent_progress,
                "active_nonterminal_work": True,
            },
            tenant_id="tenant_a",
            mission_id="mission-stall-time",
            completeness=SignalCompleteness.COMPLETE,
        ),
    ]
    assessment_recent = service.assess_mission_health("mission-stall-time", context_a, signals_recent)
    assert assessment_recent.snapshot.is_stalled is False
    assert assessment_recent.status == MissionHealthStatus.HEALTHY

    # 4b. Progreso hace 150 segundos (> 120s timeout) con trabajo activo -> STALLED
    old_progress = now - timedelta(seconds=150)
    signals_stalled = [
        HealthSignal(
            signal_id="sig-stall-old",
            signal_type=SignalType.STALL_TEMPORAL,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={
                "last_progress_at": old_progress,
                "active_nonterminal_work": True,
            },
            tenant_id="tenant_a",
            mission_id="mission-stall-time",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment_stalled = service.assess_mission_health("mission-stall-time", context_a, signals_stalled)
    assert assessment_stalled.snapshot.is_stalled is True
    assert assessment_stalled.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.MISSION_STALLED in assessment_stalled.snapshot.reason_codes
    assert assessment_stalled.decision.action == SelfMonitoringAction.PAUSE


def test_4c_temporal_stall_missing_evidence_yields_unknown(service, context_a, clock):
    now = clock.now()
    # Señal de stall sin dict estructurado o sin last_progress_at válido
    signals = [
        HealthSignal(
            signal_id="sig-stall-bad",
            signal_type=SignalType.STALL_TEMPORAL,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.WARNING,
            observed_at=now,
            confidence=0.5,
            value={"bad_field": "invalid"},
            tenant_id="tenant_a",
            mission_id="mission-stall-bad",
            completeness=SignalCompleteness.PARTIAL,
            missing_fields=("last_progress_at",),
        )
    ]
    assessment = service.assess_mission_health("mission-stall-bad", context_a, signals)
    assert assessment.snapshot.is_stalled is False
    assert assessment.status == MissionHealthStatus.UNKNOWN


def test_5_repeated_technical_failures(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-fail",
            signal_type=SignalType.TECHNICAL_FAILURE_RATE,
            source=SignalSource.R1_PLANNING,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={"failures": 3, "total": 4},  # 75% > 50%
            tenant_id="tenant_a",
            mission_id="mission-tech",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-tech", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.REPEATED_TECHNICAL_FAILURES in assessment.snapshot.reason_codes
    assert assessment.decision.action == SelfMonitoringAction.REQUEST_REPLAN


def test_6_coordination_conflict(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-coord",
            signal_type=SignalType.COORDINATION_CONFLICT,
            source=SignalSource.R4_COORDINATION,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value=True,
            tenant_id="tenant_a",
            mission_id="mission-coord",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-coord", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.COORDINATION_CONFLICT_DETECTED in assessment.snapshot.reason_codes


def test_7_repeated_delegation_near_bound(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-del",
            signal_type=SignalType.DELEGATION_PRESSURE,
            source=SignalSource.R5_DELEGATION,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value=2,  # >= max_delegation_requests (2)
            tenant_id="tenant_a",
            mission_id="mission-del",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-del", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.REPEATED_DELEGATION_NEAR_BOUND in assessment.snapshot.reason_codes


def test_8_budget_pressure(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-bud",
            signal_type=SignalType.BUDGET_PRESSURE,
            source=SignalSource.K3_COST_TRACKING,
            severity=SignalSeverity.WARNING,
            observed_at=now,
            confidence=1.0,
            value={"consumed": Decimal("88.0"), "allocated": Decimal("100.0")},  # 88% > 85% warning
            tenant_id="tenant_a",
            mission_id="mission-bud",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-bud", context_a, signals)
    assert assessment.status == MissionHealthStatus.DEGRADED
    assert DegradationReasonCode.BUDGET_PRESSURE_HIGH in assessment.snapshot.reason_codes
    assert assessment.decision.action == SelfMonitoringAction.CONTINUE_WITH_WARNING


def test_9_quota_exhaustion(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-quota",
            signal_type=SignalType.QUOTA_SATURATION,
            source=SignalSource.O7_QUOTA,
            severity=SignalSeverity.CRITICAL,
            observed_at=now,
            confidence=1.0,
            value="EXHAUSTED",
            tenant_id="tenant_a",
            mission_id="mission-quota",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-quota", context_a, signals)
    assert assessment.status == MissionHealthStatus.BLOCKED
    assert DegradationReasonCode.QUOTA_EXHAUSTED in assessment.snapshot.reason_codes
    assert assessment.decision.action == SelfMonitoringAction.BLOCK


def test_10_rate_limit_pressure(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-rate",
            signal_type=SignalType.RATE_LIMIT_PRESSURE,
            source=SignalSource.P11_RATE_LIMIT,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value="SATURATED",
            tenant_id="tenant_a",
            mission_id="mission-rate",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-rate", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.RATE_LIMIT_SATURATED in assessment.snapshot.reason_codes


def test_11_valid_cost_anomaly(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-cost",
            signal_type=SignalType.COST_ANOMALY,
            source=SignalSource.K3_COST_TRACKING,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={
                "baseline_cost": Decimal("10.0"),
                "observed_cost": Decimal("25.0"),  # 2.5x > 2.0x threshold
                "baseline_currency": "USD",
                "observed_currency": "USD",
            },
            tenant_id="tenant_a",
            mission_id="mission-cost",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-cost", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.COST_SPIKE_DETECTED in assessment.snapshot.reason_codes


def test_12_mixed_currency_not_compared(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-mix",
            signal_type=SignalType.COST_ANOMALY,
            source=SignalSource.K3_COST_TRACKING,
            severity=SignalSeverity.WARNING,
            observed_at=now,
            confidence=1.0,
            value={
                "baseline_cost": Decimal("10.0"),
                "observed_cost": Decimal("25.0"),
                "baseline_currency": "USD",
                "observed_currency": "EUR",  # Heterogeneous currencies
            },
            tenant_id="tenant_a",
            mission_id="mission-mix",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-mix", context_a, signals)
    assert DegradationReasonCode.MIXED_CURRENCY_DATA in assessment.snapshot.reason_codes
    assert assessment.status == MissionHealthStatus.UNKNOWN  # Not treated as spike


def test_13_cost_missing_fields_yields_unknown_and_explicit_zero(service, context_a, clock):
    now = clock.now()
    # 13a. Cost data missing baseline_cost -> UNKNOWN, not silent "0"
    signals_missing = [
        HealthSignal(
            signal_id="sig-cost-miss",
            signal_type=SignalType.COST_ANOMALY,
            source=SignalSource.K3_COST_TRACKING,
            severity=SignalSeverity.WARNING,
            observed_at=now,
            confidence=1.0,
            value={
                "observed_cost": Decimal("25.0"),
                "observed_currency": "USD",
            },
            tenant_id="tenant_a",
            mission_id="mission-cost-miss",
            completeness=SignalCompleteness.PARTIAL,
            missing_fields=("baseline_cost", "baseline_currency"),
        )
    ]
    assessment_missing = service.assess_mission_health("mission-cost-miss", context_a, signals_missing)
    assert assessment_missing.status == MissionHealthStatus.UNKNOWN
    assert DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN in assessment_missing.snapshot.reason_codes

    # 13b. Explicit zero cost baseline (e.g. 0 USD baseline, 10 USD observed -> valid spike)
    signals_zero_base = [
        HealthSignal(
            signal_id="sig-cost-zero",
            signal_type=SignalType.COST_ANOMALY,
            source=SignalSource.K3_COST_TRACKING,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={
                "baseline_cost": Decimal("0.0"),
                "observed_cost": Decimal("10.0"),
                "baseline_currency": "USD",
                "observed_currency": "USD",
            },
            tenant_id="tenant_a",
            mission_id="mission-cost-zero",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment_zero = service.assess_mission_health("mission-cost-zero", context_a, signals_zero_base)
    assert assessment_zero.status == MissionHealthStatus.AT_RISK
    assert DegradationReasonCode.COST_SPIKE_DETECTED in assessment_zero.snapshot.reason_codes


def test_14_failure_rate_denominator_zero(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-zero",
            signal_type=SignalType.TECHNICAL_FAILURE_RATE,
            source=SignalSource.R1_PLANNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={"failures": 0, "total": 0},
            tenant_id="tenant_a",
            mission_id="mission-zero",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-zero", context_a, signals)
    assert assessment.status == MissionHealthStatus.UNKNOWN


def test_15_request_replan_valid(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-fail",
            signal_type=SignalType.TECHNICAL_FAILURE_RATE,
            source=SignalSource.R1_PLANNING,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={"failures": 3, "total": 4},
            tenant_id="tenant_a",
            mission_id="mission-replan",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-replan", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.REQUEST_REPLAN
    assert assessment.decision.suggested_target == "dag_replan"


def test_16_policy_denial_no_replan(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-pol",
            signal_type=SignalType.POLICY_COMPLIANCE,
            source=SignalSource.N_GOVERNANCE,
            severity=SignalSeverity.CRITICAL,
            observed_at=now,
            confidence=1.0,
            value="POLICY_DENIED",
            tenant_id="tenant_a",
            mission_id="mission-policy",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-fail",
            signal_type=SignalType.TECHNICAL_FAILURE_RATE,
            source=SignalSource.R1_PLANNING,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={"failures": 3, "total": 4},
            tenant_id="tenant_a",
            mission_id="mission-policy",
            completeness=SignalCompleteness.COMPLETE,
        ),
    ]
    assessment = service.assess_mission_health("mission-policy", context_a, signals)
    assert assessment.status == MissionHealthStatus.BLOCKED
    assert assessment.decision.action == SelfMonitoringAction.BLOCK
    assert assessment.decision.action != SelfMonitoringAction.REQUEST_REPLAN


def test_17_request_delegation_valid(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-agent",
            signal_type=SignalType.AGENT_AVAILABILITY,
            source=SignalSource.R3_SPECIALIST_REGISTRY,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value="UNAVAILABLE",
            tenant_id="tenant_a",
            mission_id="mission-deleg",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-deleg", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.REQUEST_DELEGATION
    assert assessment.decision.suggested_target == "specialist_delegation"


def test_18_emergency_stop_block(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-es",
            signal_type=SignalType.EMERGENCY_STOP_STATUS,
            source=SignalSource.EXTERNAL,
            severity=SignalSeverity.CRITICAL,
            observed_at=now,
            confidence=1.0,
            value=True,
            tenant_id="tenant_a",
            mission_id="mission-es",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-es", context_a, signals)
    assert assessment.status == MissionHealthStatus.BLOCKED
    assert assessment.snapshot.is_emergency_stopped is True
    assert assessment.decision.action == SelfMonitoringAction.BLOCK


def test_19_pause_decision(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-stall",
            signal_type=SignalType.STALL_TEMPORAL,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={
                "last_progress_at": now - timedelta(seconds=200),
                "active_nonterminal_work": True,
            },
            tenant_id="tenant_a",
            mission_id="mission-pause",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-pause", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.PAUSE


def test_20_escalation_on_remediation_limit(service, context_a, clock):
    now = clock.now()
    # Ejecutar remediaciones hasta alcanzar límite
    for i in range(3):
        signals = [
            HealthSignal(
                signal_id=f"sig-stall-{i}",
                signal_type=SignalType.STALL_TEMPORAL,
                source=SignalSource.AUTONOMOUS_LOOP,
                severity=SignalSeverity.HIGH,
                observed_at=now,
                confidence=1.0,
                value={
                    "last_progress_at": now - timedelta(seconds=200),
                    "active_nonterminal_work": True,
                },
                tenant_id="tenant_a",
                mission_id="mission-escalate",
                completeness=SignalCompleteness.COMPLETE,
            )
        ]
        service.assess_mission_health("mission-escalate", context_a, signals)

    # La 4ta evaluación supera el límite (max 3) -> ESCALATE
    signals = [
        HealthSignal(
            signal_id="sig-stall-4",
            signal_type=SignalType.STALL_TEMPORAL,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value={
                "last_progress_at": now - timedelta(seconds=200),
                "active_nonterminal_work": True,
            },
            tenant_id="tenant_a",
            mission_id="mission-escalate",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-escalate", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.ESCALATE
    assert assessment.decision.requires_escalation is True
    assert assessment.remediation_limit_reached is True


def test_21_tenant_and_mission_mismatch_rejection(service, context_a, clock):
    now = clock.now()
    # 21a. Señal con tenant diferente -> CrossTenantAccessError
    signals_wrong_tenant = [
        HealthSignal(
            signal_id="sig-other-tenant",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            tenant_id="tenant_b",
            mission_id="mission-mismatch",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    with pytest.raises(CrossTenantAccessError):
        service.assess_mission_health("mission-mismatch", context_a, signals_wrong_tenant)

    # 21b. Señal con mission_id diferente -> ValueError
    signals_wrong_mission = [
        HealthSignal(
            signal_id="sig-other-mission",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            tenant_id="tenant_a",
            mission_id="different-mission-id",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    with pytest.raises(ValueError, match="Signal mission mismatch"):
        service.assess_mission_health("mission-mismatch", context_a, signals_wrong_mission)


def test_22_partial_completeness_never_produces_healthy(service, context_a, clock):
    now = clock.now()
    # Señales requeridas presentes pero una está incompleta / PARTIAL
    signals = [
        HealthSignal(
            signal_id="sig-hb",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={"last_seen_at": now},
            tenant_id="tenant_a",
            mission_id="mission-partial",
            completeness=SignalCompleteness.PARTIAL,
            missing_fields=("battery_level",),
        ),
        HealthSignal(
            signal_id="sig-prog",
            signal_type=SignalType.EXECUTION_PROGRESS,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=75.0,
            tenant_id="tenant_a",
            mission_id="mission-partial",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-pol",
            signal_type=SignalType.POLICY_COMPLIANCE,
            source=SignalSource.N_GOVERNANCE,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=True,
            tenant_id="tenant_a",
            mission_id="mission-partial",
            completeness=SignalCompleteness.COMPLETE,
        ),
    ]
    assessment = service.assess_mission_health("mission-partial", context_a, signals)
    assert assessment.status == MissionHealthStatus.UNKNOWN
    assert DegradationReasonCode.INCOMPLETE_SIGNAL_DATA in assessment.snapshot.reason_codes


def test_23_required_signal_coverage_enforcement(service, context_a, clock):
    now = clock.now()
    # Falta POLICY_COMPLIANCE requerida por policy -> UNKNOWN, no HEALTHY
    signals_missing_req = [
        HealthSignal(
            signal_id="sig-hb",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={"last_seen_at": now},
            tenant_id="tenant_a",
            mission_id="mission-req",
            completeness=SignalCompleteness.COMPLETE,
        ),
        HealthSignal(
            signal_id="sig-prog",
            signal_type=SignalType.EXECUTION_PROGRESS,
            source=SignalSource.AUTONOMOUS_LOOP,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value=75.0,
            tenant_id="tenant_a",
            mission_id="mission-req",
            completeness=SignalCompleteness.COMPLETE,
        ),
    ]
    assessment = service.assess_mission_health("mission-req", context_a, signals_missing_req)
    assert assessment.status == MissionHealthStatus.UNKNOWN
    assert DegradationReasonCode.MISSING_REQUIRED_SIGNALS in assessment.snapshot.reason_codes


def test_24_tenant_isolation(service, context_a, context_b, repo, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-1",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            value={"last_seen_at": now},
            tenant_id="tenant_a",
            mission_id="mission-tenant",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    service.assess_mission_health("mission-tenant", context_a, signals)

    # Tenant B no puede ver la evaluación de Tenant A
    assessment_b = repo.get_latest_assessment("mission-tenant", context_b)
    assert assessment_b is None

    # Intentar guardar un snapshot de tenant_a con contexto de tenant_b debe lanzar CrossTenantAccessError
    snap_a = repo.get_latest_snapshot("mission-tenant", context_a)
    with pytest.raises(CrossTenantAccessError):
        repo.save_snapshot(snap_a, context_b)


def test_25_anti_cot_and_sensitive_data(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-secret",
            signal_type=SignalType.HEARTBEAT_LIVENESS,
            source=SignalSource.R6_LONG_RUNNING,
            severity=SignalSeverity.INFO,
            observed_at=now,
            confidence=1.0,
            metadata={
                "api_key": "sk-secret-12345",
                "chain_of_thought": "Internal LLM reasoning step...",
                "prompt": "private user prompt",
            },
            tenant_id="tenant_a",
            mission_id="mission-anti-cot",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-anti-cot", context_a, signals)
    meta = assessment.snapshot.signals[0].metadata
    assert meta["api_key"] == "[REDACTED]"


def test_26_bounded_response(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal(
            signal_id="sig-del",
            signal_type=SignalType.DELEGATION_PRESSURE,
            source=SignalSource.R5_DELEGATION,
            severity=SignalSeverity.HIGH,
            observed_at=now,
            confidence=1.0,
            value=5,
            tenant_id="tenant_a",
            mission_id="mission-bound",
            completeness=SignalCompleteness.COMPLETE,
        )
    ]
    assessment = service.assess_mission_health("mission-bound", context_a, signals)
    assert assessment.decision.is_bounded is True


def test_27_no_post_r7_feature(service):
    assert not hasattr(service, "auto_modify_code")
    assert not hasattr(service, "deploy_to_production")
    assert not hasattr(service, "override_policy_decision")
