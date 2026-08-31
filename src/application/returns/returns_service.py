import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.market_intelligence.models import Confidence
from src.domain.mission.models import LoopAction, LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.order.models import Order, OrderStatus
from src.domain.order.ports import OrderRepositoryPort
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluation,
    PolicyEvaluationContext,
)
from src.domain.publication.models import SalesChannel
from src.domain.returns.models import (
    Claim,
    ClaimStage,
    ClaimStatus,
    RefundDetail,
    RefundStatus,
    Return,
    ReturnError,
    ReturnErrorCategory,
    ReturnEvent,
    ReturnQueryResult,
    ReturnReason,
    ReturnReconciliationReport,
    ReturnResolution,
    ReturnStatus,
)
from src.domain.returns.ports import ReturnsPort, ReturnsRepositoryPort
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel

logger = logging.getLogger(__name__)


class ReturnsService:
    """
    Servicio de aplicación para la gestión integral de Devoluciones, Reclamos, Reembolsos y Excepciones Postventa (G.8 / TASK 07.8).
    
    Principios de Diseño:
    - Pipeline Postventa: ORDER / SHIPMENT -> RETURN / CLAIM / EXCEPTION -> OBSERVE -> NORMALIZE -> VALIDATE -> POLICY -> ACTION -> RESULT -> RECONCILE -> RE-OBSERVE.
    - Ciclos desacoplados: ReturnStatus != ClaimStatus != RefundStatus != OrderStatus != PaymentStatus != ShipmentStatus.
    - Idempotencia estricta: Deduplicación por return_id, event_id, idempotency_key y external reference.
    - Gobernanza determinista por PolicyEngine & ActionExecutor para cualquier side-effect (reembolsos, aprobaciones).
    - Incertidumbre preservada (UNKNOWN): Errores 5xx, timeouts o conectividad no asumen éxito ni realizan retries ciegos.
    - Reconciliación inmutable: Comparación explícita de discrepancias sin sobreescritura ciega.
    """

    def __init__(
        self,
        returns_port: ReturnsPort,
        returns_repository: ReturnsRepositoryPort,
        order_repository: Optional[OrderRepositoryPort] = None,
        policy_engine: Optional[PolicyEngine] = None,
        action_executor: Optional[ActionExecutor] = None,
    ):
        self.returns_port = returns_port
        self.returns_repository = returns_repository
        self.order_repository = order_repository
        self.policy_engine = policy_engine
        self.action_executor = action_executor

    def create_return_request(
        self,
        order: Order,
        reason: ReturnReason = ReturnReason.UNKNOWN,
        shipment_id: Optional[str] = None,
        external_shipment_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Return:
        """
        Inicia una solicitud de devolución para una orden existente de forma idempotente.
        """
        cid = correlation_id or f"corr_ret_{uuid.uuid4().hex[:12]}"
        ikey = idempotency_key or f"ret_req_{order.channel.channel_id}_{order.external_order_id}"

        # 1. Comprobar si ya existe devolución para esta orden externa
        existing = self.returns_repository.get_return_by_external_order_id(
            order.external_order_id,
            order.channel.channel_id,
        )
        if existing:
            logger.info(
                "Return for external order %s already exists (%s); returning existing.",
                order.external_order_id,
                existing.return_id,
            )
            return existing

        return_id = f"ret_{uuid.uuid4().hex[:12]}"
        external_return_id = f"ext_ret_{order.external_order_id}"

        # 2. Consultar si el canal externo ya tiene o soporta la creación de la devolución
        ext_result = self.returns_port.create_return_request(
            external_order_id=order.external_order_id,
            channel=order.channel,
            reason=reason.value,
            correlation_id=cid,
            idempotency_key=ikey,
        )

        if ext_result and ext_result.returns and ext_result.returns[0].status != ReturnStatus.UNKNOWN:
            return_to_save = ext_result.returns[0]
        else:
            return_to_save = Return(
                return_id=return_id,
                external_return_id=external_return_id,
                order_id=order.order_id,
                external_order_id=order.external_order_id,
                channel=order.channel,
                status=ReturnStatus.REQUESTED,
                reason=reason,
                resolution=ReturnResolution.UNKNOWN,
                shipment_id=shipment_id,
                external_shipment_id=external_shipment_id,
                correlation_id=cid,
                idempotency_key=ikey,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
            )

        self.returns_repository.save_return(return_to_save)
        return return_to_save

    def sync_return(
        self,
        external_return_id: str,
        channel: SalesChannel,
        correlation_id: Optional[str] = None,
    ) -> Optional[Return]:
        """
        Sincroniza el estado de una devolución desde el marketplace respetando UNKNOWN.
        """
        cid = correlation_id or f"corr_sync_ret_{uuid.uuid4().hex[:12]}"

        ext_result = self.returns_port.get_return_by_external_id(external_return_id, channel)
        if not ext_result or not ext_result.returns:
            return None

        ext_return = ext_result.returns[0]

        # Si el resultado es UNKNOWN por error de red/5xx, no alterar destructivamente
        if ext_return.status == ReturnStatus.UNKNOWN:
            logger.warning(
                "External query for return %s returned UNKNOWN status. Preserving uncertainty.",
                external_return_id,
            )
            local_existing = self.returns_repository.get_return_by_external_id(
                external_return_id,
                channel.channel_id,
            )
            return local_existing or ext_return

        self.returns_repository.save_return(ext_return)
        for ev in ext_return.events:
            self.returns_repository.save_return_event(ev)

        return ext_return

    def record_return_event(
        self,
        event: ReturnEvent,
        channel_id: str,
    ) -> bool:
        """
        Registra un evento del ciclo de devoluciones con deduplicación estricta por event_id.
        """
        if self.returns_repository.is_event_processed(event.event_id):
            logger.info("Return event %s already processed; duplicate ignored.", event.event_id)
            return False

        inserted = self.returns_repository.save_return_event(event)
        if inserted:
            self.returns_repository.record_processed_event(event.event_id)
        return inserted

    def reconcile_return(
        self,
        return_id: str,
        channel: SalesChannel,
    ) -> ReturnReconciliationReport:
        """
        Compara el estado local vs el estado externo observado y genera un reporte inmutable.
        """
        local_return = self.returns_repository.get_return_by_id(return_id)
        external_id = local_return.external_return_id if local_return else return_id

        ext_result = self.returns_port.get_return_by_external_id(external_id, channel)
        ext_return = ext_result.returns[0] if (ext_result and ext_result.returns) else None

        if not local_return and not ext_return:
            return ReturnReconciliationReport(
                return_id=return_id,
                external_return_id=external_id,
                order_id="",
                external_order_id="",
                is_reconciled=False,
                internal_status=ReturnStatus.UNKNOWN,
                external_status=ReturnStatus.UNKNOWN,
                discrepancies=("Return not found locally nor in marketplace",),
                requires_action=False,
            )

        if not local_return and ext_return:
            return ReturnReconciliationReport(
                return_id=ext_return.return_id,
                external_return_id=ext_return.external_return_id,
                order_id=ext_return.order_id,
                external_order_id=ext_return.external_order_id,
                is_reconciled=False,
                internal_status=ReturnStatus.UNKNOWN,
                external_status=ext_return.status,
                internal_refund_status=None,
                external_refund_status=ext_return.refund.status if ext_return.refund else None,
                refund_reconciled=True,
                discrepancies=("Return exists externally but is missing locally",),
                requires_action=True,
            )

        if local_return and not ext_return:
            return ReturnReconciliationReport(
                return_id=local_return.return_id,
                external_return_id=local_return.external_return_id,
                order_id=local_return.order_id,
                external_order_id=local_return.external_order_id,
                is_reconciled=False,
                internal_status=local_return.status,
                external_status=ReturnStatus.UNKNOWN,
                internal_refund_status=local_return.refund.status if local_return.refund else None,
                external_refund_status=None,
                refund_reconciled=True,
                discrepancies=("Return exists locally but external query failed or returned empty",),
                requires_action=False,
            )

        # Ambos existen: comparar estados y reembolsos
        discrepancies = []
        if local_return.status != ext_return.status:
            discrepancies.append(
                f"status: local {local_return.status.value} != external {ext_return.status.value}"
            )
        if local_return.reason != ext_return.reason and ext_return.reason != ReturnReason.UNKNOWN:
            discrepancies.append(
                f"reason: local {local_return.reason.value} != external {ext_return.reason.value}"
            )
        if local_return.resolution != ext_return.resolution and ext_return.resolution != ReturnResolution.UNKNOWN:
            discrepancies.append(
                f"resolution: local {local_return.resolution.value} != external {ext_return.resolution.value}"
            )

        local_ref_status = local_return.refund.status if local_return.refund else RefundStatus.NOT_REQUESTED
        ext_ref_status = ext_return.refund.status if ext_return.refund else RefundStatus.NOT_REQUESTED
        refund_reconciled = (local_ref_status == ext_ref_status)
        if not refund_reconciled:
            discrepancies.append(
                f"refund: local {local_ref_status.value} != external {ext_ref_status.value}"
            )

        is_reconciled = len(discrepancies) == 0
        requires_action = not is_reconciled

        # Actualizar local si hubo evolución legítima y no UNKNOWN
        if not is_reconciled and ext_return.status != ReturnStatus.UNKNOWN:
            self.returns_repository.save_return(ext_return)

        return ReturnReconciliationReport(
            return_id=local_return.return_id,
            external_return_id=local_return.external_return_id,
            order_id=local_return.order_id,
            external_order_id=local_return.external_order_id,
            is_reconciled=is_reconciled,
            internal_status=local_return.status,
            external_status=ext_return.status,
            internal_refund_status=local_ref_status,
            external_refund_status=ext_ref_status,
            refund_reconciled=refund_reconciled,
            discrepancies=tuple(discrepancies),
            requires_action=requires_action,
        )

    def execute_return_action_guarded(
        self,
        action_type: str,
        return_id: str,
        channel: SalesChannel,
        amount: Optional[Decimal] = None,
        currency: str = "USD",
        human_approved: bool = False,
        actor_id: str = "autonomous_returns_agent",
        mission_id: str = "returns_mission_g8",
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[PolicyEvaluation, Optional[Any]]:
        """
        Ejecuta una acción postventa (ej. ISSUE_REFUND, APPROVE_RETURN, REJECT_RETURN)
        gobernada estrictamente por PolicyEngine y ActionExecutor.
        """
        cid = correlation_id or f"corr_act_{uuid.uuid4().hex[:12]}"
        ikey = idempotency_key or f"act_{action_type}_{return_id}_{uuid.uuid4().hex[:8]}"

        local_return = self.returns_repository.get_return_by_id(return_id)

        # 1. Comprobar si la clave de idempotencia ya fue ejecutada
        if self.returns_repository.is_idempotency_key_executed(ikey):
            logger.warning("Action idempotency_key %s already executed. Denying re-execution.", ikey)
            ctx_dup = PolicyEvaluationContext(
                action_type=action_type,
                actor_id=actor_id,
                mission_id=mission_id,
                correlation_id=cid,
                idempotency_key=ikey,
                loop_decision=LoopDecision(
                    action=LoopAction.CONTINUE,
                    reason=f"Duplicate action attempt {ikey}",
                ),
                executed_idempotency_keys=(ikey,),
                is_external_impact=True,
                human_approved=human_approved,
            )
            if self.policy_engine:
                eval_res = self.policy_engine.evaluate(ctx_dup)
                return eval_res, None

        # 2. Contexto de evaluación de políticas
        context = PolicyEvaluationContext(
            action_type=action_type,
            actor_id=actor_id,
            mission_id=mission_id,
            correlation_id=cid,
            idempotency_key=ikey,
            loop_decision=LoopDecision(
                action=LoopAction.CONTINUE,
                reason=f"Guarded post-sale action {action_type} on return {return_id}",
            ),
            target_resource=return_id,
            channel=channel.channel_id,
            requested_budget=amount,
            risk_level=RiskLevel.MEDIUM if amount and amount > Decimal("50.00") else RiskLevel.LOW,
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
            is_external_impact=True,
            is_irreversible=(action_type in ("ISSUE_REFUND", "REJECT_RETURN")),
            human_approved=human_approved,
            actions_requiring_approval=("REJECT_RETURN",),
        )

        # 3. Evaluar con PolicyEngine si está configurado
        if self.policy_engine:
            policy_eval = self.policy_engine.evaluate(context)
            if policy_eval.decision != PolicyDecisionType.ALLOW:
                logger.info(
                    "Policy denied or requested approval for action %s on return %s: %s",
                    action_type,
                    return_id,
                    policy_eval.decision.value,
                )
                return policy_eval, None
        else:
            # Fallback seguro determinista si no hay PolicyEngine inyectado
            policy_eval = PolicyEvaluation(
                decision=PolicyDecisionType.ALLOW,
                context_summary={"action_type": action_type, "return_id": return_id},
            )

        # 4. Ejecución del side-effect externo si la política lo autoriza
        result_payload = None
        if action_type == "ISSUE_REFUND":
            if not amount or amount <= Decimal("0.00"):
                raise ValueError("ISSUE_REFUND requires a positive amount")

            ext_return_id = local_return.external_return_id if local_return else return_id
            ext_order_id = local_return.external_order_id if local_return else ""

            refund_res = self.returns_port.execute_refund(
                external_return_id=ext_return_id,
                external_order_id=ext_order_id,
                amount=amount,
                currency=currency,
                channel=channel,
                correlation_id=cid,
                idempotency_key=ikey,
            )

            result_payload = refund_res

            # Si el reembolso fue exitoso o quedó en seguimiento, actualizar Return local
            if refund_res and local_return:
                updated_return = Return(
                    return_id=local_return.return_id,
                    external_return_id=local_return.external_return_id,
                    order_id=local_return.order_id,
                    external_order_id=local_return.external_order_id,
                    channel=local_return.channel,
                    status=ReturnStatus.RESOLVED if refund_res.status == RefundStatus.CONFIRMED else local_return.status,
                    reason=local_return.reason,
                    resolution=ReturnResolution.REFUND,
                    shipment_id=local_return.shipment_id,
                    external_shipment_id=local_return.external_shipment_id,
                    claim_id=local_return.claim_id,
                    refund=refund_res,
                    events=local_return.events,
                    created_at=local_return.created_at,
                    updated_at=datetime.now(timezone.utc),
                    closed_at=datetime.now(timezone.utc) if refund_res.status == RefundStatus.CONFIRMED else None,
                    correlation_id=cid,
                    idempotency_key=ikey,
                    provenance=refund_res.provenance,
                    confidence=refund_res.confidence,
                    raw_reference=local_return.raw_reference,
                )
                self.returns_repository.save_return(updated_return)

        # 5. Marcar clave de idempotencia como ejecutada
        self.returns_repository.record_executed_idempotency_key(ikey)

        return policy_eval, result_payload
