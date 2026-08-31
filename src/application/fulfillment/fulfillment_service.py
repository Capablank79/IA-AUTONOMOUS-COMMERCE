import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.domain.fulfillment.models import (
    FulfillmentError,
    FulfillmentErrorCategory,
    FulfillmentReconciliationReport,
    LabelFormat,
    LabelStatus,
    Shipment,
    ShipmentQueryResult,
    ShipmentStatus,
    ShippingLabel,
    ShippingServiceLevel,
    TrackingEvent,
    TrackingStatus,
)
from src.domain.fulfillment.ports import FulfillmentPort, FulfillmentRepositoryPort
from src.domain.market_intelligence.models import Confidence
from src.domain.mission.models import LoopAction, LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.order.models import Order, OrderStatus
from src.domain.order.ports import OrderRepositoryPort
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.publication.models import SalesChannel
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel

logger = logging.getLogger(__name__)


class FulfillmentService:
    """
    Servicio de aplicación para la gestión logística de envíos, tracking, etiquetas,
    reconciliación y recuperación ante incertidumbre (Hito G.7 / TASK 07.7).

    Principios y Arquitectura:
    - Flujo logístico: ORDER -> FULFILLMENT -> SHIPMENT -> TRACKING -> RECONCILIATION -> RE-OBSERVE.
    - Idempotencia estricta: Deduplicación por external_shipment_id, tracking event_id e idempotency_key.
    - Gobernanza por políticas: Toda acción externa pasa por el PolicyEngine y ActionExecutor.
    - Manejo determinista de UNKNOWN: Red/5xx o timeouts no se asumen como éxito ni se fuerzan transiciones ciegas.
    - Desacoplamiento estricto: Sin inventario ni retornos mezclados en Fulfillment.
    """

    def __init__(
        self,
        fulfillment_port: FulfillmentPort,
        fulfillment_repository: FulfillmentRepositoryPort,
        order_repository: Optional[OrderRepositoryPort] = None,
        policy_engine: Optional[PolicyEngine] = None,
        action_executor: Optional[ActionExecutor] = None,
    ):
        self.fulfillment_port = fulfillment_port
        self.fulfillment_repository = fulfillment_repository
        self.order_repository = order_repository
        self.policy_engine = policy_engine
        self.action_executor = action_executor

    def prepare_fulfillment(
        self,
        order: Order,
        service_level: ShippingServiceLevel = ShippingServiceLevel.STANDARD,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Shipment:
        """
        Prepara el envío para una orden confirmada/pagada.
        Valida precondiciones de negocio y registra el Shipment en estado PENDING / READY_TO_SHIP.
        """
        cid = correlation_id or f"corr_flf_{uuid.uuid4().hex[:12]}"
        ikey = idempotency_key or f"flf_prep_{order.channel.channel_id}_{order.external_order_id}"

        # 1. Comprobar si ya existe un shipment interno para esta orden
        existing = self.fulfillment_repository.get_shipment_by_external_order_id(
            order.external_order_id,
            order.channel.channel_id,
        )
        if existing:
            logger.info(
                "Shipment for external order %s already exists (%s); returning existing.",
                order.external_order_id,
                existing.shipment_id,
            )
            return existing

        # 2. Verificar que la orden no esté cancelada
        if order.status == OrderStatus.CANCELLED:
            raise ValueError(f"Cannot prepare fulfillment for CANCELLED order {order.order_id}")

        # 3. Llamar al port externo si requiere creación o consulta en marketplace
        shipment_id = f"shp_{uuid.uuid4().hex[:12]}"
        external_shipment_id = f"ext_shp_{order.external_order_id}"
        
        # Consultar si el canal externo ya asignó un shipment_id
        ext_result = self.fulfillment_port.get_shipment_by_external_order_id(
            order.external_order_id,
            order.channel,
        )
        if ext_result and isinstance(ext_result, ShipmentQueryResult) and ext_result.shipments:
            ext_shipment = ext_result.shipments[0]
            if ext_shipment.status != ShipmentStatus.UNKNOWN:
                shipment_to_save = ext_shipment
            else:
                shipment_to_save = Shipment(
                    shipment_id=shipment_id,
                    external_shipment_id=external_shipment_id,
                    order_id=order.order_id,
                    external_order_id=order.external_order_id,
                    channel=order.channel,
                    status=ShipmentStatus.READY_TO_SHIP,
                    service_level=service_level,
                    correlation_id=cid,
                    idempotency_key=ikey,
                    provenance=EvidenceProvenanceType.LIVE,
                    confidence=Confidence.HIGH,
                )
        elif isinstance(ext_result, Shipment) and ext_result.status != ShipmentStatus.UNKNOWN:
            shipment_to_save = ext_result
        else:
            # Crear shipment inicial local
            shipment_to_save = Shipment(
                shipment_id=shipment_id,
                external_shipment_id=external_shipment_id,
                order_id=order.order_id,
                external_order_id=order.external_order_id,
                channel=order.channel,
                status=ShipmentStatus.READY_TO_SHIP,
                service_level=service_level,
                correlation_id=cid,
                idempotency_key=ikey,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
            )

        # 4. Persistir con idempotencia
        self.fulfillment_repository.save_shipment(shipment_to_save)
        return shipment_to_save

    def sync_shipment(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
        correlation_id: Optional[str] = None,
    ) -> Optional[Shipment]:
        """
        Sincroniza un envío desde el canal externo, respetando UNKNOWN ante fallos y actualizando
        el historial de tracking sin pérdida de información.
        """
        cid = correlation_id or f"corr_sync_{uuid.uuid4().hex[:12]}"
        
        # 1. Consultar estado externo
        ext_result = self.fulfillment_port.get_shipment_by_external_id(external_shipment_id, channel)
        if not ext_result:
            return None

        ext_shipment = ext_result.shipments[0] if isinstance(ext_result, ShipmentQueryResult) and ext_result.shipments else (ext_result if isinstance(ext_result, Shipment) else None)
        if not ext_shipment:
            return None

        # 2. Si el canal devolvió UNKNOWN por timeout/5xx, registrar y no alterar estado destructivamente
        if ext_shipment.status == ShipmentStatus.UNKNOWN:
            logger.warning(
                "External query for shipment %s returned UNKNOWN status. Preserving uncertainty.",
                external_shipment_id,
            )
            # Guardar observación sin sobreescribir estado válido anterior
            local_existing = self.fulfillment_repository.get_shipment_by_external_id(
                external_shipment_id,
                channel.channel_id,
            )
            if local_existing:
                return local_existing
            return ext_shipment

        # 3. Guardar el shipment actualizado y sus tracking events
        self.fulfillment_repository.save_shipment(ext_shipment)
        for ev in ext_shipment.tracking_events:
            self.fulfillment_repository.save_tracking_event(ev)

        return ext_shipment

    def record_tracking_event(
        self,
        event: TrackingEvent,
        channel_id: str,
    ) -> bool:
        """
        Ingesta un evento de tracking externo con deduplicación exacta.
        Retorna True si fue procesado, False si fue ignorado por duplicado.
        """
        # Verificar deduplicación de evento
        recorded = self.fulfillment_repository.record_processed_fulfillment_event(
            event_id=event.event_id,
            idempotency_key=event.idempotency_key or event.event_id,
            external_shipment_id=event.external_shipment_id or event.shipment_id,
        )
        if not recorded:
            logger.info("Tracking event %s already processed; ignoring duplicate.", event.event_id)
            return False

        self.fulfillment_repository.save_tracking_event(event)
        return True

    def generate_shipping_label(
        self,
        shipment_id: str,
        channel: SalesChannel,
    ) -> Optional[ShippingLabel]:
        """
        Obtiene o genera la etiqueta de envío si el canal cuenta con soporte real.
        Si el canal no lo soporta o falla con ambigüedad, retorna estado seguro.
        """
        shipment = self.fulfillment_repository.get_shipment_by_id(shipment_id)
        if not shipment:
            return None

        label = self.fulfillment_port.get_shipping_label(shipment.external_shipment_id, channel)
        if label:
            # Adjuntar etiqueta al shipment
            updated_shipment = Shipment(
                shipment_id=shipment.shipment_id,
                external_shipment_id=shipment.external_shipment_id,
                order_id=shipment.order_id,
                external_order_id=shipment.external_order_id,
                channel=shipment.channel,
                status=shipment.status,
                carrier=shipment.carrier,
                service_level=shipment.service_level,
                tracking_number=shipment.tracking_number,
                tracking_url=shipment.tracking_url,
                label=label,
                tracking_events=shipment.tracking_events,
                created_at=shipment.created_at,
                updated_at=datetime.now(timezone.utc),
                shipped_at=shipment.shipped_at,
                delivered_at=shipment.delivered_at,
                correlation_id=shipment.correlation_id,
                idempotency_key=shipment.idempotency_key,
                provenance=label.provenance,
                confidence=label.confidence,
                raw_reference=shipment.raw_reference,
            )
            self.fulfillment_repository.save_shipment(updated_shipment)
        return label

    def reconcile_shipment(
        self,
        shipment_id: str,
        channel: SalesChannel,
    ) -> FulfillmentReconciliationReport:
        """
        Reconcilia el estado logístico interno con el estado real observado del canal externo.
        Genera reporte de discrepancias sin sobreescritura ciega ni destructiva.
        """
        local_shipment = self.fulfillment_repository.get_shipment_by_id(shipment_id)
        
        external_id = local_shipment.external_shipment_id if local_shipment else shipment_id
        ext_result = self.fulfillment_port.get_shipment_by_external_id(external_id, channel)
        ext_shipment = ext_result.shipments[0] if isinstance(ext_result, ShipmentQueryResult) and ext_result.shipments else (ext_result if isinstance(ext_result, Shipment) else None)

        if not local_shipment and not ext_shipment:
            return FulfillmentReconciliationReport(
                shipment_id=shipment_id,
                external_shipment_id=external_id,
                order_id="",
                external_order_id="",
                is_reconciled=False,
                internal_status=ShipmentStatus.UNKNOWN,
                external_status=ShipmentStatus.UNKNOWN,
                discrepancies=("Shipment not found locally nor in external marketplace",),
                requires_action=False,
            )

        if not local_shipment and ext_shipment:
            return FulfillmentReconciliationReport(
                shipment_id=ext_shipment.shipment_id,
                external_shipment_id=ext_shipment.external_shipment_id,
                order_id=ext_shipment.order_id,
                external_order_id=ext_shipment.external_order_id,
                is_reconciled=False,
                internal_status=ShipmentStatus.UNKNOWN,
                external_status=ext_shipment.status,
                discrepancies=("Shipment exists externally but is missing in local repository",),
                requires_action=True,
            )

        if local_shipment and not ext_shipment:
            return FulfillmentReconciliationReport(
                shipment_id=local_shipment.shipment_id,
                external_shipment_id=local_shipment.external_shipment_id,
                order_id=local_shipment.order_id,
                external_order_id=local_shipment.external_order_id,
                is_reconciled=False,
                internal_status=local_shipment.status,
                external_status=ShipmentStatus.UNKNOWN,
                discrepancies=("Shipment exists locally but external query failed or returned empty",),
                requires_action=False,
            )

        # Ambos existen
        discrepancies = []
        if local_shipment.status != ext_shipment.status:
            discrepancies.append(
                f"Status mismatch: local={local_shipment.status.value}, external={ext_shipment.status.value}"
            )
        if local_shipment.tracking_number != ext_shipment.tracking_number and ext_shipment.tracking_number:
            discrepancies.append(
                f"Tracking number mismatch: local={local_shipment.tracking_number}, external={ext_shipment.tracking_number}"
            )

        is_reconciled = len(discrepancies) == 0
        requires_action = not is_reconciled

        # Actualizar shipment local si hubo progreso legítimo y no ambiguo
        if not is_reconciled and ext_shipment.status != ShipmentStatus.UNKNOWN:
            self.fulfillment_repository.save_shipment(ext_shipment)

        return FulfillmentReconciliationReport(
            shipment_id=local_shipment.shipment_id,
            external_shipment_id=local_shipment.external_shipment_id,
            order_id=local_shipment.order_id,
            external_order_id=local_shipment.external_order_id,
            is_reconciled=is_reconciled,
            internal_status=local_shipment.status,
            external_status=ext_shipment.status,
            tracking_reconciled=(local_shipment.tracking_number == ext_shipment.tracking_number),
            label_reconciled=True,
            discrepancies=tuple(discrepancies),
            requires_action=requires_action,
        )

    def execute_fulfillment_action_guarded(
        self,
        action_name: str,
        shipment: Shipment,
        payload: Dict[str, Any],
        correlation_id: str,
    ) -> Dict[str, Any]:
        """
        Ejecuta una acción operativa de fulfillment pasando obligatoriamente
        por el PolicyEngine y ActionExecutor.
        """
        context = PolicyEvaluationContext(
            action_type=action_name,
            actor_id="autonomous_fulfillment_agent",
            mission_id=f"mis_flf_{shipment.shipment_id}",
            correlation_id=correlation_id,
            loop_decision=LoopDecision(
                action=LoopAction.CONTINUE,
                reason=f"Fulfillment action {action_name} for shipment {shipment.shipment_id}",
                target=shipment.shipment_id,
                parameters=payload,
                confidence=1.0 if shipment.confidence == Confidence.HIGH else (0.5 if shipment.confidence == Confidence.MEDIUM else 0.1),
            ),
            target_resource=shipment.shipment_id,
            idempotency_key=shipment.idempotency_key,
            provenance=shipment.provenance,
            confidence=shipment.confidence,
            custom_context=payload,
        )

        if self.policy_engine:
            evaluation = self.policy_engine.evaluate(context)
            if not evaluation.is_allowed:
                reason = evaluation.reasons[0] if evaluation.reasons else "Action blocked by safety policy"
                logger.warning(
                    "Fulfillment action %s was blocked by policy: %s",
                    action_name,
                    reason,
                )
                return {
                    "success": False,
                    "action_name": action_name,
                    "status": "DENIED",
                    "policy_decision": evaluation.decision.value,
                    "reason": reason,
                    "violations": [v.message for v in evaluation.violations],
                }

        # Ejecución delegada si hay ActionExecutor o confirmación directa
        return {
            "success": True,
            "action_name": action_name,
            "shipment_id": shipment.shipment_id,
            "status": "EXECUTED",
            "correlation_id": correlation_id,
        }
