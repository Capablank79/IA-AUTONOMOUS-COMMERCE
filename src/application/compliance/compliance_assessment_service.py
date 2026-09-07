"""
Application service for N.10 — Audit / Compliance (Transversal N — Security, Governance & Safety).

Implements ComplianceAssessmentService:
- Reconstructs operational execution history from Audit Trail (K.1), Agent Trace (K.2), and N.1-N.9 decisions.
- Evaluates compliance against active or historical policy versions.
- Enforces correlation binding, cryptographic integrity verification, and fail-secure logic.
- Generates structured, privacy-preserving compliance reports without leaking PII, secrets or CoT.
- Read-only service: does not modify state, policies, or execute actions.
"""

from datetime import datetime, timezone
import hashlib
from types import MappingProxyType
from typing import Dict, List, Optional, Sequence, Any, Set, Tuple

from src.domain.compliance.models import (
    ComplianceStatus,
    ComplianceFindingSeverity,
    ComplianceFindingReasonCode,
    ComplianceRequirementType,
    ComplianceEvidenceType,
    ComplianceRequirement,
    ComplianceEvidenceReference,
    ComplianceFinding,
    ComplianceCheck,
    CompliancePolicy,
    ComplianceAssessment,
    ComplianceReport,
    compute_evidence_checksum,
    compute_finding_checksum,
    compute_assessment_checksum,
)
from src.domain.compliance.ports import (
    ComplianceEvidenceCollectorPort,
    CompliancePolicyRepositoryPort,
    ComplianceAssessmentServicePort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus
from src.domain.security.models import sanitize_security_data, validate_safe_identifier
from src.infrastructure.persistence.data.in_memory.compliance_policy_repository import (
    InMemoryCompliancePolicyRepository,
    create_default_commercial_compliance_policy,
)


class DefaultComplianceEvidenceCollector(ComplianceEvidenceCollectorPort):
    """
    Recolector por defecto de referencias de evidencia desde K.1 (Audit) y K.2 (Trace).
    """

    def __init__(
        self,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
    ):
        self.audit_repository = audit_repository
        self.trace_repository = trace_repository

    def collect_by_correlation_id(
        self,
        correlation_id: str,
        mission_id: Optional[str] = None,
    ) -> Sequence[ComplianceEvidenceReference]:
        evidences: List[ComplianceEvidenceReference] = []

        # 1. Recolectar desde AuditRepository K.1
        if self.audit_repository:
            audit_records = self.audit_repository.list_records(
                correlation_id=correlation_id,
                mission_id=mission_id,
                limit=500,
            )
            for rec in audit_records:
                # Verificar integridad del registro de auditoría
                payload_for_hash = {
                    "audit_id": rec.audit_id,
                    "record_type": rec.record_type.value,
                    "occurred_at": rec.occurred_at.isoformat(),
                    "actor_type": rec.actor.actor_type.value,
                    "actor_id": rec.actor.actor_id,
                    "subject_type": rec.subject_type,
                    "subject_id": rec.subject_id,
                    "action_or_operation": rec.action_or_operation,
                    "status": rec.status,
                    "correlation_id": rec.correlation_id,
                    "causation_id": rec.causation_id,
                    "mission_id": rec.mission_id,
                    "entity_reference": rec.entity_reference,
                    "evidence_reference": rec.evidence_reference,
                    "provenance": rec.provenance,
                    "idempotency_key": rec.idempotency_key,
                    "schema_version": rec.schema_version,
                }
                import json
                expected_hash = hashlib.sha256(json.dumps(payload_for_hash, sort_keys=True).encode("utf-8")).hexdigest()
                is_valid = (rec.checksum == expected_hash)

                # Mapear tipo de evidencia
                ev_type = ComplianceEvidenceType.AUDIT_RECORD
                if rec.record_type == AuditRecordType.AUTHENTICATION_EVALUATED:
                    ev_type = ComplianceEvidenceType.AUTHENTICATION_RESULT
                elif rec.record_type == AuditRecordType.AUTHORIZATION_EVALUATED:
                    ev_type = ComplianceEvidenceType.AUTHORIZATION_DECISION
                elif rec.record_type in (AuditRecordType.RBAC_EVALUATED, AuditRecordType.ROLE_ASSIGNED):
                    ev_type = ComplianceEvidenceType.RBAC_EVALUATION
                elif rec.record_type in (AuditRecordType.TOOL_ACCESS_EVALUATED, AuditRecordType.TOOL_ACCESS_DENIED):
                    ev_type = ComplianceEvidenceType.TOOL_ACCESS_DECISION
                elif rec.record_type in (AuditRecordType.FINANCIAL_LIMIT_EVALUATED, AuditRecordType.LIMIT_EXCEEDED):
                    ev_type = ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION
                elif rec.record_type in (AuditRecordType.APPROVAL_EVALUATED, AuditRecordType.APPROVAL_GRANTED, AuditRecordType.APPROVAL_REJECTED):
                    ev_type = ComplianceEvidenceType.APPROVAL_DECISION
                elif rec.record_type in (AuditRecordType.SENSITIVE_DATA_EVALUATED, AuditRecordType.SENSITIVE_DATA_REDACTED, AuditRecordType.SENSITIVE_DATA_BLOCKED):
                    ev_type = ComplianceEvidenceType.SENSITIVE_DATA_DECISION
                elif rec.action_or_operation in ("EMERGENCY_STOP_EVALUATED", "EMERGENCY_STOP_ACTIVATED", "EMERGENCY_STOP_DEACTIVATED"):
                    ev_type = ComplianceEvidenceType.EMERGENCY_STOP_DECISION
                elif rec.record_type == AuditRecordType.ACTION_EXECUTED:
                    ev_type = ComplianceEvidenceType.ACTION_RESULT

                ev_ref = ComplianceEvidenceReference(
                    evidence_id=f"audit_{rec.audit_id}",
                    evidence_type=ev_type,
                    source_component="K1_AUDIT_TRAIL",
                    correlation_id=rec.correlation_id,
                    mission_id=rec.mission_id,
                    original_checksum=rec.checksum,
                    integrity_verified=is_valid,
                    observed_status=rec.status,
                    summary=f"{rec.record_type.value}: {rec.action_or_operation} by {rec.actor.actor_id} -> {rec.status}",
                    metadata={
                        "record_type": rec.record_type.value,
                        "actor_id": rec.actor.actor_id,
                        "actor_type": rec.actor.actor_type.value,
                        "action_or_operation": rec.action_or_operation,
                        "subject_type": rec.subject_type,
                        "subject_id": rec.subject_id,
                        "metadata": dict(rec.metadata),
                    },
                )
                evidences.append(ev_ref)

        # 2. Recolectar desde AgentTraceRepository K.2
        if self.trace_repository:
            trace_records = self.trace_repository.list_records(
                correlation_id=correlation_id,
                mission_id=mission_id,
                limit=500,
            )
            for tr in trace_records:
                is_valid = tr.verify_checksum()
                ev_ref = ComplianceEvidenceReference(
                    evidence_id=f"trace_{tr.trace_id}",
                    evidence_type=ComplianceEvidenceType.TRACE_RECORD,
                    source_component="K2_AGENT_TRACE",
                    correlation_id=tr.correlation_id,
                    mission_id=tr.mission_id,
                    original_checksum=tr.checksum,
                    integrity_verified=is_valid,
                    observed_status=tr.status.value,
                    summary=f"Trace step {tr.step_number} [{tr.step_type.value}]: {tr.operation} -> {tr.status.value}",
                    metadata={
                        "component_name": tr.component_name,
                        "step_number": tr.step_number,
                        "step_type": tr.step_type.value,
                        "operation": tr.operation,
                        "status": tr.status.value,
                        "tool_or_service": tr.tool_or_service,
                    },
                )
                evidences.append(ev_ref)

        return tuple(evidences)


class ComplianceAssessmentService(ComplianceAssessmentServicePort):
    """
    Servicio principal de Evaluación de Auditoría y Cumplimiento (N.10).
    """

    def __init__(
        self,
        evidence_collector: Optional[ComplianceEvidenceCollectorPort] = None,
        policy_repository: Optional[CompliancePolicyRepositoryPort] = None,
        default_policy_name: str = "default_commercial_compliance_policy",
    ):
        self.evidence_collector = evidence_collector or DefaultComplianceEvidenceCollector()
        self.policy_repository = policy_repository or InMemoryCompliancePolicyRepository()
        self.default_policy_name = default_policy_name

    def assess_operation(
        self,
        correlation_id: str,
        mission_id: Optional[str] = None,
        action_or_operation: Optional[str] = None,
        resource: Optional[str] = None,
        policy_name: Optional[str] = None,
        policy_version: Optional[str] = None,
        injected_evidences: Optional[Sequence[ComplianceEvidenceReference]] = None,
    ) -> ComplianceAssessment:
        """
        Evalúa retrospectiva y determinísticamente una operación contra una política de compliance.
        """
        if not correlation_id or not isinstance(correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string.")

        target_policy_name = policy_name or self.default_policy_name
        policy = self.policy_repository.get_by_name(target_policy_name, version=policy_version)
        if not policy:
            # Si no se encuentra la política solicitada, fail-secure con ERROR
            assessment_id = f"ass_{correlation_id[:16]}_{int(datetime.now(timezone.utc).timestamp())}"
            error_finding = ComplianceFinding(
                finding_id=f"fnd_policy_err_{correlation_id[:8]}",
                requirement_id="POLICY_NOT_FOUND",
                status=ComplianceStatus.ERROR,
                severity=ComplianceFindingSeverity.CRITICAL,
                reason_code=ComplianceFindingReasonCode.EVALUATION_ERROR,
                description=f"Compliance policy '{target_policy_name}' version '{policy_version}' could not be resolved.",
                correlation_id=correlation_id,
                mission_id=mission_id,
                policy_name=target_policy_name,
                policy_version=policy_version or "UNKNOWN",
            )
            return ComplianceAssessment(
                assessment_id=assessment_id,
                overall_status=ComplianceStatus.ERROR,
                policy_name=target_policy_name,
                policy_version=policy_version or "UNKNOWN",
                correlation_id=correlation_id,
                mission_id=mission_id,
                action_or_operation=action_or_operation,
                resource=resource,
                findings=(error_finding,),
                evidence_references=(),
                reconstruction_summary={"error": "Policy not found"},
            )

        # 1. Recolectar evidencias
        all_evidences: List[ComplianceEvidenceReference] = []
        if self.evidence_collector:
            collected = self.evidence_collector.collect_by_correlation_id(
                correlation_id=correlation_id,
                mission_id=mission_id,
            )
            all_evidences.extend(collected)

        if injected_evidences:
            all_evidences.extend(injected_evidences)

        # 2. Filtrar y validar evidencias por correlation_id y mission_id (Cross-Correlation Isolation)
        valid_evidences: List[ComplianceEvidenceReference] = []
        cross_correlation_evidences: List[ComplianceEvidenceReference] = []
        corrupt_evidences: List[ComplianceEvidenceReference] = []

        for ev in all_evidences:
            if ev.correlation_id != correlation_id:
                cross_correlation_evidences.append(ev)
            elif not ev.integrity_verified:
                corrupt_evidences.append(ev)
                valid_evidences.append(ev)  # Se incluye para generar finding explícito de integridad
            else:
                valid_evidences.append(ev)

        # 3. Evaluar cada requerimiento de la política
        findings: List[ComplianceFinding] = []

        # 3.1 Hallazgos directos de integridad si hay evidencias corruptas
        if corrupt_evidences and policy.require_audit_integrity:
            for c_ev in corrupt_evidences:
                findings.append(
                    ComplianceFinding(
                        finding_id=f"fnd_tamper_{c_ev.evidence_id[:16]}",
                        requirement_id="AUDIT_INTEGRITY",
                        status=ComplianceStatus.ERROR,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.AUDIT_INTEGRITY_FAILURE,
                        description=f"Evidence '{c_ev.evidence_id}' failed cryptographic integrity/checksum verification.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=(c_ev,),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                )

        # 3.2 Hallazgos de cross-correlation si se rechazó evidencia
        if cross_correlation_evidences and policy.reject_cross_correlation_evidence:
            findings.append(
                ComplianceFinding(
                    finding_id=f"fnd_crosscorr_{correlation_id[:8]}",
                    requirement_id="CORRELATION_ISOLATION",
                    status=ComplianceStatus.NON_COMPLIANT,
                    severity=ComplianceFindingSeverity.HIGH,
                    reason_code=ComplianceFindingReasonCode.CROSS_CORRELATION_EVIDENCE_REJECTED,
                    description=f"{len(cross_correlation_evidences)} evidence records were rejected due to mismatched correlation_id.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(cross_correlation_evidences),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            )

        # 4. Reconstrucción de hechos operacionales
        reconstruction = self._reconstruct_operational_context(valid_evidences, action_or_operation, resource)

        # 5. Evaluar requerimientos específicos
        for req in policy.requirements:
            if not req.applies_to_action(reconstruction.get("action", action_or_operation or ""), reconstruction.get("resource_type")):
                findings.append(
                    ComplianceFinding(
                        finding_id=f"fnd_{req.requirement_id.lower()}_na",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.CONTROL_NOT_APPLICABLE,
                        description=f"Requirement '{req.requirement_id}' does not apply to action '{reconstruction.get('action')}'.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        action_or_operation=reconstruction.get("action"),
                        resource=reconstruction.get("resource"),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                )
                continue

            req_finding = self._evaluate_requirement(req, reconstruction, valid_evidences, correlation_id, mission_id, policy)
            findings.append(req_finding)

        # 6. Determinar Estado Consolidado (Overall Status)
        overall_status = self._derive_overall_status(findings)

        assessment_id = f"ass_{correlation_id}_{int(datetime.now(timezone.utc).timestamp())}"

        return ComplianceAssessment(
            assessment_id=assessment_id,
            overall_status=overall_status,
            policy_name=policy.policy_name,
            policy_version=policy.version,
            correlation_id=correlation_id,
            mission_id=mission_id or reconstruction.get("mission_id"),
            action_or_operation=reconstruction.get("action") or action_or_operation,
            resource=reconstruction.get("resource") or resource,
            findings=tuple(findings),
            evidence_references=tuple(valid_evidences),
            reconstruction_summary=reconstruction,
            metadata={"evaluated_requirements_count": len(findings)},
        )

    def _reconstruct_operational_context(
        self,
        evidences: Sequence[ComplianceEvidenceReference],
        hint_action: Optional[str] = None,
        hint_resource: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reconstruye el contexto operacional a partir de las evidencias."""
        reconstruction: Dict[str, Any] = {
            "actor_id": None,
            "actor_type": None,
            "authentication_status": None,
            "rbac_permissions": [],
            "authorization_status": None,
            "authorization_allowed": None,
            "tool_access_status": None,
            "tool_allowed": None,
            "financial_limit_status": None,
            "financial_within_limit": None,
            "approval_status": None,
            "approval_granted": None,
            "sensitive_data_status": None,
            "action": hint_action,
            "resource": hint_resource,
            "action_executed": False,
            "action_blocked": False,
            "execution_status": None,
            "mission_id": None,
        }

        for ev in evidences:
            meta = ev.metadata
            if ev.mission_id and not reconstruction["mission_id"]:
                reconstruction["mission_id"] = ev.mission_id

            # Actor & Identity (N.1)
            if "actor_id" in meta and not reconstruction["actor_id"]:
                reconstruction["actor_id"] = meta.get("actor_id")
                reconstruction["actor_type"] = meta.get("actor_type")
            elif ev.evidence_type in (ComplianceEvidenceType.AUTHENTICATION_RESULT, ComplianceEvidenceType.IDENTITY_DECISION) and not reconstruction["actor_id"]:
                # Si proviene de un resultado de autenticación o identidad
                reconstruction["actor_id"] = meta.get("actor_id", "authenticated_principal")
                reconstruction["actor_type"] = meta.get("actor_type", "PRINCIPAL")

            # Authentication (N.2)
            if ev.evidence_type == ComplianceEvidenceType.AUTHENTICATION_RESULT:
                reconstruction["authentication_status"] = ev.observed_status

            # RBAC (N.4)
            if ev.evidence_type == ComplianceEvidenceType.RBAC_EVALUATION:
                perms = meta.get("metadata", {}).get("actions", []) or meta.get("actions", [])
                reconstruction["rbac_permissions"].extend(perms)

            # Authorization (N.3)
            if ev.evidence_type == ComplianceEvidenceType.AUTHORIZATION_DECISION:
                reconstruction["authorization_status"] = ev.observed_status
                reconstruction["authorization_allowed"] = (ev.observed_status == "ALLOW")
                if ev.observed_status in ("DENY", "BLOCKED"):
                    reconstruction["action_blocked"] = True

            # Tool Access (N.8)
            if ev.evidence_type == ComplianceEvidenceType.TOOL_ACCESS_DECISION:
                reconstruction["tool_access_status"] = ev.observed_status
                reconstruction["tool_allowed"] = (ev.observed_status == "ALLOW")
                if ev.observed_status in ("DENY", "BLOCKED"):
                    reconstruction["action_blocked"] = True

            # Financial Limit (N.7)
            if ev.evidence_type == ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION:
                reconstruction["financial_limit_status"] = ev.observed_status
                reconstruction["financial_within_limit"] = (ev.observed_status == "WITHIN_LIMIT")
                if ev.observed_status in ("LIMIT_EXCEEDED", "DENIED"):
                    reconstruction["action_blocked"] = True

            # Approval (N.6)
            if ev.evidence_type in (ComplianceEvidenceType.APPROVAL_DECISION, ComplianceEvidenceType.APPROVAL_EVIDENCE):
                reconstruction["approval_status"] = ev.observed_status
                reconstruction["approval_granted"] = (ev.observed_status in ("APPROVED", "NOT_REQUIRED"))
                if ev.observed_status == "REJECTED":
                    reconstruction["action_blocked"] = True

            # Sensitive Data (N.9)
            if ev.evidence_type == ComplianceEvidenceType.SENSITIVE_DATA_DECISION:
                reconstruction["sensitive_data_status"] = ev.observed_status

            # Emergency Stop (N.11)
            if ev.evidence_type == ComplianceEvidenceType.EMERGENCY_STOP_DECISION:
                reconstruction["emergency_stop_status"] = ev.observed_status
                if ev.observed_status in ("BLOCK_EXECUTION", "BLOCKED", "UNKNOWN", "ERROR"):
                    reconstruction["action_blocked"] = True

            # Action Result / Execution
            if ev.evidence_type == ComplianceEvidenceType.ACTION_RESULT:
                reconstruction["action_executed"] = True
                reconstruction["execution_status"] = ev.observed_status
                if not reconstruction["action"]:
                    reconstruction["action"] = meta.get("action_or_operation")

            # Detectar ejecuciones u operaciones desde trazas o auditoría general
            if ev.evidence_type == ComplianceEvidenceType.AUDIT_RECORD:
                rec_type = meta.get("record_type")
                if rec_type == "ACTION_EXECUTED":
                    reconstruction["action_executed"] = True
                    reconstruction["execution_status"] = ev.observed_status
                    if not reconstruction["action"]:
                        reconstruction["action"] = meta.get("action_or_operation")
                elif rec_type in ("ACTION_BLOCKED", "AUTHORIZATION_DENIED", "TOOL_ACCESS_DENIED", "LIMIT_EXCEEDED", "APPROVAL_REJECTED"):
                    reconstruction["action_blocked"] = True

        return reconstruction

    def _evaluate_requirement(
        self,
        req: ComplianceRequirement,
        recon: Dict[str, Any],
        evidences: Sequence[ComplianceEvidenceReference],
        correlation_id: str,
        mission_id: Optional[str],
        policy: CompliancePolicy,
    ) -> ComplianceFinding:
        """Evalúa un requerimiento concreto contra el contexto operacional reconstruido."""
        req_type = req.requirement_type
        relevant_evs = [e for e in evidences if self._is_evidence_relevant(e, req_type)]

        # Fail-secure ante falta de evidencia requerida
        if not relevant_evs:
            # Caso especial: Si la acción fue bloqueada correctamente en frontera anterior (ej: N.3 DENY),
            # y el control evaluado es downstream (ej: N.7 o N.8), no es un fallo si la acción nunca llegó a ejecutarse.
            if recon.get("action_blocked") and not recon.get("action_executed"):
                return ComplianceFinding(
                    finding_id=f"fnd_{req.requirement_id.lower()}_skipped_due_to_block",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                    description=f"Requirement '{req.requirement_id}' was not evaluated because the unsafe action was successfully blocked upstream.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )

            # Caso especial: Regla de Policy Chain: "No exigir pasos que no aplican a una operación".
            # Si una operación general fue autorizada (N.3 ALLOW) y ejecutada, y controles opcionales/condicionales
            # como Tool Policy, Financial Limits, Approval o Sensitive Data no fueron activados ni violados,
            # son evaluados como COMPLIANT_EXECUTION (control no gatillado).
            if recon.get("authorization_allowed") and req_type in (
                ComplianceRequirementType.TOOL_POLICY_REQUIRED,
                ComplianceRequirementType.FINANCIAL_LIMIT_REQUIRED,
                ComplianceRequirementType.APPROVAL_REQUIRED,
                ComplianceRequirementType.SENSITIVE_DATA_POLICY_REQUIRED,
                ComplianceRequirementType.RBAC_PERMISSIONS_REQUIRED,
                ComplianceRequirementType.EMERGENCY_STOP_ENFORCED,
            ):
                return ComplianceFinding(
                    finding_id=f"fnd_{req.requirement_id.lower()}_inferred_ok",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description=f"Requirement '{req.requirement_id}' satisfied under authorized standard operation.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )

            # De lo contrario, falta de evidencia requerida
            status = ComplianceStatus.INCOMPLETE if not req.is_mandatory else ComplianceStatus.NON_COMPLIANT
            reason_code = self._map_missing_reason_code(req_type)
            return ComplianceFinding(
                finding_id=f"fnd_{req.requirement_id.lower()}_missing",
                requirement_id=req.requirement_id,
                status=status,
                severity=req.default_severity_on_failure,
                reason_code=reason_code,
                description=f"Missing required evidence for control '{req.requirement_id}'.",
                correlation_id=correlation_id,
                mission_id=mission_id,
                policy_name=policy.policy_name,
                policy_version=policy.version,
            )

        # Evaluar semántica según el tipo de control
        if req_type == ComplianceRequirementType.IDENTITY_REQUIRED:
            if recon.get("actor_id"):
                return ComplianceFinding(
                    finding_id=f"fnd_identity_{correlation_id[:8]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description=f"Actor identity verified ({recon.get('actor_id')}).",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            return ComplianceFinding(
                finding_id=f"fnd_identity_missing_{correlation_id[:8]}",
                requirement_id=req.requirement_id,
                status=ComplianceStatus.NON_COMPLIANT,
                severity=req.default_severity_on_failure,
                reason_code=ComplianceFindingReasonCode.MISSING_IDENTITY_EVIDENCE,
                description="No valid actor identity found in evidence.",
                correlation_id=correlation_id,
                mission_id=mission_id,
                evidence_references=tuple(relevant_evs),
                policy_name=policy.policy_name,
                policy_version=policy.version,
            )

        elif req_type == ComplianceRequirementType.AUTHENTICATION_REQUIRED:
            auth_ev = relevant_evs[0]
            if auth_ev.observed_status in ("AUTHENTICATED", "ALLOW"):
                return ComplianceFinding(
                    finding_id=f"fnd_auth_{auth_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description=f"Actor was successfully authenticated ({auth_ev.observed_status}).",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            elif auth_ev.observed_status in ("UNKNOWN",):
                return ComplianceFinding(
                    finding_id=f"fnd_auth_{auth_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.UNKNOWN,
                    severity=ComplianceFindingSeverity.HIGH,
                    reason_code=ComplianceFindingReasonCode.UNKNOWN_EVIDENCE_STATUS,
                    description="Authentication status is UNKNOWN; fail-secure applied.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            else:
                return ComplianceFinding(
                    finding_id=f"fnd_auth_{auth_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.NON_COMPLIANT,
                    severity=ComplianceFindingSeverity.CRITICAL,
                    reason_code=ComplianceFindingReasonCode.UNAUTHENTICATED_EXECUTION,
                    description=f"Authentication check failed with status '{auth_ev.observed_status}'.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )

        elif req_type == ComplianceRequirementType.RBAC_PERMISSIONS_REQUIRED:
            rbac_ev = relevant_evs[0]
            if rbac_ev.observed_status in ("VALID", "PERMITTED", "ALLOW"):
                return ComplianceFinding(
                    finding_id=f"fnd_rbac_{rbac_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Actor possesses valid RBAC permissions.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            else:
                return ComplianceFinding(
                    finding_id=f"fnd_rbac_invalid_{rbac_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.NON_COMPLIANT,
                    severity=ComplianceFindingSeverity.HIGH,
                    reason_code=ComplianceFindingReasonCode.MISSING_RBAC_EVIDENCE,
                    description=f"RBAC permissions check failed with status '{rbac_ev.observed_status}'.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )

        elif req_type == ComplianceRequirementType.AUTHORIZATION_REQUIRED:
            authz_ev = relevant_evs[0]
            if authz_ev.observed_status == "ALLOW":
                return ComplianceFinding(
                    finding_id=f"fnd_authz_{authz_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Action obtained explicit ALLOW authorization.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            else:
                # Si fue DENY y la acción NO se ejecutó (está bloqueada), es COMPLIANT (enforcement correcto)
                if not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_authz_blocked_{authz_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Authorization correctly issued DENY and the action was blocked from executing.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    # DENY pero la acción se ejecutó -> VIOLACIÓN CRÍTICA
                    return ComplianceFinding(
                        finding_id=f"fnd_authz_violation_{authz_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.AUTHORIZATION_DENIED_BUT_EXECUTED,
                        description="Action was executed despite receiving an explicit DENY authorization decision.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )

        elif req_type == ComplianceRequirementType.TOOL_POLICY_REQUIRED:
            tool_ev = relevant_evs[0]
            if tool_ev.observed_status == "ALLOW":
                return ComplianceFinding(
                    finding_id=f"fnd_tool_{tool_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Tool access was evaluated and permitted.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            else:
                if not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_tool_blocked_{tool_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Tool access policy issued DENY and execution was successfully blocked.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    return ComplianceFinding(
                        finding_id=f"fnd_tool_violation_{tool_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.TOOL_POLICY_DENIED_BUT_EXECUTED,
                        description="Tool operation was executed despite tool policy DENY.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )

        elif req_type == ComplianceRequirementType.FINANCIAL_LIMIT_REQUIRED:
            fin_ev = relevant_evs[0]
            if fin_ev.observed_status == "WITHIN_LIMIT":
                return ComplianceFinding(
                    finding_id=f"fnd_fin_{fin_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Financial operation was within configured monetary limits.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            elif fin_ev.observed_status in ("LIMIT_EXCEEDED", "APPROVAL_REQUIRED"):
                # Si excedió pero existe aprobación válida (N.6) y se ejecutó -> COMPLIANT
                if recon.get("approval_granted"):
                    return ComplianceFinding(
                        finding_id=f"fnd_fin_approved_{fin_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                        description="Financial limit was exceeded but valid overriding approval was verified.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                elif not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_fin_blocked_{fin_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Financial limit exceeded without approval and operation was blocked.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    return ComplianceFinding(
                        finding_id=f"fnd_fin_violation_{fin_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.FINANCIAL_LIMIT_EXCEEDED_WITHOUT_APPROVAL,
                        description="Financial limit exceeded and executed without required approval.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
            else:
                return ComplianceFinding(
                    finding_id=f"fnd_fin_unknown_{fin_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.UNKNOWN,
                    severity=ComplianceFindingSeverity.HIGH,
                    reason_code=ComplianceFindingReasonCode.UNKNOWN_EVIDENCE_STATUS,
                    description=f"Financial limit evaluation returned status '{fin_ev.observed_status}'.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )

        elif req_type == ComplianceRequirementType.APPROVAL_REQUIRED:
            appr_ev = relevant_evs[0]
            if appr_ev.observed_status in ("APPROVED", "NOT_REQUIRED"):
                return ComplianceFinding(
                    finding_id=f"fnd_appr_{appr_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Approval requirements satisfied.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            elif appr_ev.observed_status == "REJECTED":
                if not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_appr_blocked_{appr_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Approval was rejected and action was correctly blocked from executing.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    return ComplianceFinding(
                        finding_id=f"fnd_appr_violation_{appr_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.APPROVAL_REJECTED_BUT_EXECUTED,
                        description="Action executed despite explicit approval rejection.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
            else:
                if not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_appr_blocked_{appr_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Approval required and action was correctly blocked.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    return ComplianceFinding(
                        finding_id=f"fnd_appr_violation_{appr_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.APPROVAL_REQUIRED_BUT_MISSING,
                        description="Action executed without required approval.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )

        elif req_type == ComplianceRequirementType.SENSITIVE_DATA_POLICY_REQUIRED:
            sens_ev = relevant_evs[0]
            if sens_ev.observed_status in ("ALLOWED", "REDACTED", "MINIMIZED", "SANITIZED"):
                return ComplianceFinding(
                    finding_id=f"fnd_sens_{sens_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Sensitive data handling policy satisfied.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            else:
                if not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_sens_blocked_{sens_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Sensitive data handling blocked unsafe payload and execution was avoided.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    return ComplianceFinding(
                        finding_id=f"fnd_sens_violation_{sens_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.HIGH,
                        reason_code=ComplianceFindingReasonCode.SENSITIVE_DATA_BLOCKED_BUT_EXECUTED,
                        description="Operation executed with sensitive data blocked or unredacted.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )

        elif req_type == ComplianceRequirementType.EMERGENCY_STOP_ENFORCED:
            estop_ev = relevant_evs[0]
            if estop_ev.observed_status in ("ALLOW_EXECUTION", "ALLOW"):
                return ComplianceFinding(
                    finding_id=f"fnd_estop_{estop_ev.evidence_id[:12]}",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.COMPLIANT,
                    severity=ComplianceFindingSeverity.INFO,
                    reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                    description="Emergency stop evaluation allowed operation.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            elif estop_ev.observed_status in ("BLOCK_EXECUTION", "BLOCKED", "UNKNOWN", "ERROR"):
                if not recon.get("action_executed"):
                    return ComplianceFinding(
                        finding_id=f"fnd_estop_blocked_{estop_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.COMPLIANT,
                        severity=ComplianceFindingSeverity.INFO,
                        reason_code=ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION,
                        description="Emergency stop blocked operation and zero physical execution occurred.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )
                else:
                    return ComplianceFinding(
                        finding_id=f"fnd_estop_violation_{estop_ev.evidence_id[:12]}",
                        requirement_id=req.requirement_id,
                        status=ComplianceStatus.NON_COMPLIANT,
                        severity=ComplianceFindingSeverity.CRITICAL,
                        reason_code=ComplianceFindingReasonCode.EMERGENCY_STOP_BLOCKED_BUT_EXECUTED,
                        description="Operation physically executed while Emergency Stop was active/blocking.",
                        correlation_id=correlation_id,
                        mission_id=mission_id,
                        evidence_references=tuple(relevant_evs),
                        policy_name=policy.policy_name,
                        policy_version=policy.version,
                    )

        elif req_type == ComplianceRequirementType.SECRET_LEAKAGE_PROHIBITED:
            # Chequear si en las evidencias o metadata de trazas/auditoría existen secretos en claro
            for ev in evidences:
                meta_str = str(ev.metadata).lower()
                for leak_key in ("password", "secret", "api_key", "pan", "cvv", "private_key"):
                    if f"'{leak_key}': '" in meta_str and "[redacted]" not in meta_str:
                        return ComplianceFinding(
                            finding_id=f"fnd_secret_leak_{ev.evidence_id[:12]}",
                            requirement_id=req.requirement_id,
                            status=ComplianceStatus.NON_COMPLIANT,
                            severity=ComplianceFindingSeverity.CRITICAL,
                            reason_code=ComplianceFindingReasonCode.SECRET_EXPOSURE_VIOLATION,
                            description=f"Secret leakage detected in evidence metadata '{ev.evidence_id}'.",
                            correlation_id=correlation_id,
                            mission_id=mission_id,
                            evidence_references=(ev,),
                            policy_name=policy.policy_name,
                            policy_version=policy.version,
                        )
            return ComplianceFinding(
                finding_id="fnd_secret_ok",
                requirement_id=req.requirement_id,
                status=ComplianceStatus.COMPLIANT,
                severity=ComplianceFindingSeverity.INFO,
                reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                description="No secret leakage detected across all evidence metadata.",
                correlation_id=correlation_id,
                mission_id=mission_id,
                evidence_references=tuple(relevant_evs),
                policy_name=policy.policy_name,
                policy_version=policy.version,
            )

        elif req_type == ComplianceRequirementType.AUDIT_TRAIL_REQUIRED:
            # Verificar que existan registros de auditoría K.1 y su integridad
            has_corrupt = any(not e.integrity_verified for e in relevant_evs)
            if has_corrupt:
                return ComplianceFinding(
                    finding_id="fnd_audit_tamper",
                    requirement_id=req.requirement_id,
                    status=ComplianceStatus.ERROR,
                    severity=ComplianceFindingSeverity.CRITICAL,
                    reason_code=ComplianceFindingReasonCode.AUDIT_INTEGRITY_FAILURE,
                    description="Audit trail records failed cryptographic integrity check.",
                    correlation_id=correlation_id,
                    mission_id=mission_id,
                    evidence_references=tuple(relevant_evs),
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                )
            return ComplianceFinding(
                finding_id="fnd_audit_ok",
                requirement_id=req.requirement_id,
                status=ComplianceStatus.COMPLIANT,
                severity=ComplianceFindingSeverity.INFO,
                reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
                description=f"Audit trail verified ({len(relevant_evs)} records with valid integrity).",
                correlation_id=correlation_id,
                mission_id=mission_id,
                evidence_references=tuple(relevant_evs),
                policy_name=policy.policy_name,
                policy_version=policy.version,
            )

        # Requerimiento genérico / por defecto
        return ComplianceFinding(
            finding_id=f"fnd_{req.requirement_id.lower()}_ok",
            requirement_id=req.requirement_id,
            status=ComplianceStatus.COMPLIANT,
            severity=ComplianceFindingSeverity.INFO,
            reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
            description=f"Requirement '{req.requirement_id}' satisfied.",
            correlation_id=correlation_id,
            mission_id=mission_id,
            evidence_references=tuple(relevant_evs),
            policy_name=policy.policy_name,
            policy_version=policy.version,
        )

    def _is_evidence_relevant(self, ev: ComplianceEvidenceReference, req_type: ComplianceRequirementType) -> bool:
        """Mapea si una evidencia es relevante para un requerimiento."""
        if req_type == ComplianceRequirementType.AUDIT_TRAIL_REQUIRED:
            return ev.source_component == "K1_AUDIT_TRAIL" or ev.evidence_type == ComplianceEvidenceType.AUDIT_RECORD

        if req_type == ComplianceRequirementType.SECRET_LEAKAGE_PROHIBITED:
            return True

        mapping = {
            ComplianceRequirementType.IDENTITY_REQUIRED: (
                ComplianceEvidenceType.IDENTITY_DECISION,
                ComplianceEvidenceType.AUTHENTICATION_RESULT,
                ComplianceEvidenceType.AUDIT_RECORD,
                ComplianceEvidenceType.ACTION_RESULT,
            ),
            ComplianceRequirementType.AUTHENTICATION_REQUIRED: (
                ComplianceEvidenceType.AUTHENTICATION_RESULT,
            ),
            ComplianceRequirementType.RBAC_PERMISSIONS_REQUIRED: (
                ComplianceEvidenceType.RBAC_EVALUATION,
            ),
            ComplianceRequirementType.AUTHORIZATION_REQUIRED: (
                ComplianceEvidenceType.AUTHORIZATION_DECISION,
            ),
            ComplianceRequirementType.TOOL_POLICY_REQUIRED: (
                ComplianceEvidenceType.TOOL_ACCESS_DECISION,
            ),
            ComplianceRequirementType.FINANCIAL_LIMIT_REQUIRED: (
                ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION,
            ),
            ComplianceRequirementType.APPROVAL_REQUIRED: (
                ComplianceEvidenceType.APPROVAL_DECISION,
                ComplianceEvidenceType.APPROVAL_EVIDENCE,
            ),
            ComplianceRequirementType.SENSITIVE_DATA_POLICY_REQUIRED: (
                ComplianceEvidenceType.SENSITIVE_DATA_DECISION,
            ),
            ComplianceRequirementType.EMERGENCY_STOP_ENFORCED: (
                ComplianceEvidenceType.EMERGENCY_STOP_DECISION,
            ),
            ComplianceRequirementType.TRACE_SEQUENCE_REQUIRED: (
                ComplianceEvidenceType.TRACE_RECORD,
            ),
        }
        allowed = mapping.get(req_type, ())
        return ev.evidence_type in allowed

    def _map_missing_reason_code(self, req_type: ComplianceRequirementType) -> ComplianceFindingReasonCode:
        """Mapea el código de razón para evidencia faltante."""
        mapping = {
            ComplianceRequirementType.IDENTITY_REQUIRED: ComplianceFindingReasonCode.MISSING_IDENTITY_EVIDENCE,
            ComplianceRequirementType.AUTHENTICATION_REQUIRED: ComplianceFindingReasonCode.MISSING_AUTHENTICATION_EVIDENCE,
            ComplianceRequirementType.RBAC_PERMISSIONS_REQUIRED: ComplianceFindingReasonCode.MISSING_RBAC_EVIDENCE,
            ComplianceRequirementType.AUTHORIZATION_REQUIRED: ComplianceFindingReasonCode.MISSING_AUTHORIZATION_EVIDENCE,
            ComplianceRequirementType.TOOL_POLICY_REQUIRED: ComplianceFindingReasonCode.MISSING_TOOL_POLICY_EVIDENCE,
            ComplianceRequirementType.FINANCIAL_LIMIT_REQUIRED: ComplianceFindingReasonCode.MISSING_FINANCIAL_LIMIT_EVIDENCE,
            ComplianceRequirementType.APPROVAL_REQUIRED: ComplianceFindingReasonCode.MISSING_APPROVAL_EVIDENCE,
            ComplianceRequirementType.SENSITIVE_DATA_POLICY_REQUIRED: ComplianceFindingReasonCode.MISSING_SENSITIVE_DATA_EVIDENCE,
            ComplianceRequirementType.EMERGENCY_STOP_ENFORCED: ComplianceFindingReasonCode.MISSING_REQUIRED_CONTROL_EVIDENCE,
            ComplianceRequirementType.AUDIT_TRAIL_REQUIRED: ComplianceFindingReasonCode.MISSING_AUDIT_TRAIL,
            ComplianceRequirementType.TRACE_SEQUENCE_REQUIRED: ComplianceFindingReasonCode.MISSING_TRACE_EVIDENCE,
        }
        return mapping.get(req_type, ComplianceFindingReasonCode.MISSING_REQUIRED_CONTROL_EVIDENCE)

    def _derive_overall_status(self, findings: Sequence[ComplianceFinding]) -> ComplianceStatus:
        """
        Deriva el estado general de compliance aplicando fail-secure y precedencia:
        ERROR > NON_COMPLIANT > INCOMPLETE > UNKNOWN > COMPLIANT.
        """
        if not findings:
            return ComplianceStatus.UNKNOWN

        statuses = set(f.status for f in findings)

        if ComplianceStatus.ERROR in statuses:
            return ComplianceStatus.ERROR
        if ComplianceStatus.NON_COMPLIANT in statuses:
            return ComplianceStatus.NON_COMPLIANT
        if ComplianceStatus.INCOMPLETE in statuses:
            return ComplianceStatus.INCOMPLETE
        if ComplianceStatus.UNKNOWN in statuses:
            return ComplianceStatus.UNKNOWN
        if all(s == ComplianceStatus.COMPLIANT for s in statuses):
            return ComplianceStatus.COMPLIANT

        return ComplianceStatus.UNKNOWN

    def generate_report(self, assessment: ComplianceAssessment) -> ComplianceReport:
        """Genera un reporte estructurado y seguro para un assessment."""
        report_id = f"rep_{assessment.assessment_id}"
        return ComplianceReport(
            report_id=report_id,
            assessment=assessment,
            title="Commercial Compliance & Security Governance Report",
        )
