from datetime import datetime, timezone
from typing import Dict, Any, Optional, Mapping
from types import MappingProxyType

from src.domain.tool.models import (
    ToolDescriptor,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolLifecycleStatus,
    ToolEvidenceProvenance,
)
from src.domain.tool.ports import ToolInvokerPort
from src.domain.policy.models import (
    PolicyEvaluationContext,
    PolicyDecisionType,
    PolicyEvaluation,
)
from src.domain.policy.ports import PolicyEnginePort, PolicyAuditRepository
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel


class ToolInvocationService:
    """
    Servicio de aplicación para la invocación tipada y segura de herramientas registradas.
    
    Flujo de Gobernanza y Separación Estricta:
    1. Obtención y validación de contrato de entrada (Input Schema Validation)
    2. Verificación de estado de ciclo de vida (Bloqueo de DISABLED, DEPRECATED, UNKNOWN)
    3. Evaluación obligatoria de Política de Gobernanza (PolicyEngine / PolicyEnforcementService)
    4. Si Policy == ALLOW -> Invocación delegada a ToolInvokerPort
    5. Validación de contrato de salida (Output Schema Validation)
    6. Retorno de ToolInvocationResult inmutable con trazabilidad y procedencia
    """

    def __init__(
        self,
        invoker: ToolInvokerPort,
        policy_service: Optional[PolicyEnforcementService] = None,
        policy_engine: Optional[PolicyEnginePort] = None,
        audit_repository: Optional[PolicyAuditRepository] = None,
    ):
        if invoker is None:
            raise ValueError("invoker cannot be None")
        self.invoker = invoker
        self.policy_service = policy_service or PolicyEnforcementService(
            policy_engine=policy_engine,
            audit_repository=audit_repository,
        )

    def invoke_tool(
        self,
        request: ToolInvocationRequest,
        descriptor: ToolDescriptor,
        human_approved: bool = False,
    ) -> ToolInvocationResult:
        """
        Ejecuta la secuencia segura de invocación de herramienta.
        """
        if descriptor is None:
            return ToolInvocationResult(
                tool_id=request.tool_id,
                version=request.version or "UNKNOWN",
                success=False,
                error_message="ToolDescriptor cannot be None",
                error_code="DESCRIPTOR_NOT_FOUND",
                correlation_id=request.correlation_id,
                provenance=ToolEvidenceProvenance.UNKNOWN,
            )

        # 1. Verificar ciclo de vida / ejecutable
        if descriptor.status == ToolLifecycleStatus.UNKNOWN:
            return ToolInvocationResult(
                tool_id=descriptor.tool_id,
                version=descriptor.version.version_str,
                success=False,
                error_message=f"Tool '{descriptor.tool_id}' has UNKNOWN lifecycle status and cannot be executed",
                error_code="UNKNOWN_TOOL_STATUS",
                correlation_id=request.correlation_id,
                provenance=ToolEvidenceProvenance.UNKNOWN,
            )

        if not descriptor.is_executable:
            return ToolInvocationResult(
                tool_id=descriptor.tool_id,
                version=descriptor.version.version_str,
                success=False,
                error_message=f"Tool '{descriptor.tool_id}' is {descriptor.status.value} and cannot be executed",
                error_code="TOOL_NOT_AVAILABLE",
                correlation_id=request.correlation_id,
                provenance=descriptor.provenance,
            )

        # 2. Validación de Contrato de Entrada (Input Contract)
        is_valid_input, input_errors = descriptor.input_contract.validate(request.input_payload)
        if not is_valid_input:
            return ToolInvocationResult(
                tool_id=descriptor.tool_id,
                version=descriptor.version.version_str,
                success=False,
                error_message=f"Input contract validation failed: {'; '.join(input_errors)}",
                error_code="INVALID_INPUT_CONTRACT",
                correlation_id=request.correlation_id,
                provenance=descriptor.provenance,
            )

        # 3. Construir contexto y Evaluar Política de Gobernanza
        is_external = descriptor.side_effect_level.value in ("EXTERNAL_SIDE_EFFECT", "IRREVERSIBLE")
        is_irreversible = descriptor.side_effect_level.value == "IRREVERSIBLE"

        # Mapear a LoopDecision para compatibilidad de contexto de política
        loop_decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason=f"Invoking tool {descriptor.qualified_id}",
            target=descriptor.tool_id,
            parameters=request.input_payload,
        )

        policy_context = PolicyEvaluationContext(
            action_type=descriptor.capability,
            actor_id=request.actor_id,
            mission_id=request.mission_id or "NO_MISSION",
            correlation_id=request.correlation_id or "NO_CORRELATION",
            loop_decision=loop_decision,
            idempotency_key=request.idempotency_key,
            request_id=request.idempotency_key or request.correlation_id,
            target_resource=descriptor.tool_id,
            channel=request.requested_channel.value if request.requested_channel else None,
            risk_level=RiskLevel.MEDIUM if is_external else RiskLevel.LOW,
            is_external_impact=is_external,
            is_irreversible=is_irreversible,
            human_approved=human_approved,
            actions_requiring_approval=(descriptor.capability,) if (descriptor.requires_approval or is_external) else (),
            provenance=EvidenceProvenanceType.LIVE if descriptor.provenance.value == "LIVE" else EvidenceProvenanceType.DERIVED,
        )

        policy_eval = self.policy_service.evaluate_decision(policy_context)

        # 4. Bloquear si la política no es ALLOW
        if policy_eval.decision == PolicyDecisionType.DENY:
            return ToolInvocationResult(
                tool_id=descriptor.tool_id,
                version=descriptor.version.version_str,
                success=False,
                error_message=f"Execution blocked by policy DENY: {', '.join(policy_eval.reasons)}",
                error_code="POLICY_DENIED",
                correlation_id=request.correlation_id,
                provenance=descriptor.provenance,
            )

        if policy_eval.decision == PolicyDecisionType.REQUIRE_APPROVAL:
            return ToolInvocationResult(
                tool_id=descriptor.tool_id,
                version=descriptor.version.version_str,
                success=False,
                error_message=f"Execution requires human approval: {', '.join(policy_eval.reasons)}",
                error_code="REQUIRE_APPROVAL",
                correlation_id=request.correlation_id,
                provenance=descriptor.provenance,
            )

        if policy_eval.decision in (PolicyDecisionType.DEFER, PolicyDecisionType.UNKNOWN):
            return ToolInvocationResult(
                tool_id=descriptor.tool_id,
                version=descriptor.version.version_str,
                success=False,
                error_message=f"Execution deferred or uncertain by policy: {', '.join(policy_eval.reasons)}",
                error_code=f"POLICY_{policy_eval.decision.value}",
                correlation_id=request.correlation_id,
                provenance=descriptor.provenance,
            )

        # 5. Invocación segura mediante ToolInvokerPort
        raw_result = self.invoker.invoke(request=request, descriptor=descriptor)

        # 6. Validación de Contrato de Salida (Output Contract)
        if raw_result.success:
            is_valid_output, output_errors = descriptor.output_contract.validate(raw_result.output_payload)
            if not is_valid_output:
                return ToolInvocationResult(
                    tool_id=descriptor.tool_id,
                    version=descriptor.version.version_str,
                    success=False,
                    error_message=f"Output contract validation failed: {'; '.join(output_errors)}",
                    error_code="INVALID_OUTPUT_CONTRACT",
                    correlation_id=request.correlation_id,
                    provenance=raw_result.provenance,
                )

        return raw_result
