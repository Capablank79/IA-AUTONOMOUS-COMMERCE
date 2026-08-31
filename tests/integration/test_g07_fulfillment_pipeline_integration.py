from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.application.fulfillment.fulfillment_service import FulfillmentService
from src.application.order.order_processing_service import OrderProcessingService
from src.application.tool.catalog import register_standard_commerce_tools
from src.domain.fulfillment.models import (
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
from src.domain.inventory.models import InventoryRequest, InventoryResult, InventoryStatus, StockLevel
from src.domain.inventory.ports import InventoryPort
from src.domain.market_intelligence.models import Confidence
from src.domain.order.models import (
    BuyerReference,
    FulfillmentStatus,
    Order,
    OrderItem,
    OrderStatus,
    PaymentStatus,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyDecisionType
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.tool.registry import ToolRegistry
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.fulfillment_adapter import MercadoLibreFulfillmentAdapter
from src.infrastructure.mercadolibre.order_adapter import MercadoLibreOrderAdapter
from src.infrastructure.persistence.data.in_memory.fulfillment_repository import (
    InMemoryFulfillmentRepository,
)
from src.infrastructure.persistence.data.in_memory.order_repository import (
    InMemoryOrderRepository,
)


def test_e2e_fulfillment_full_pipeline_happy_path_and_reconciliation():
    """
    E2E SCENARIO A: HAPPY PATH
    CONFIRMED/PAID ORDER (G.6)
    -> FULFILLMENT PREPARATION (G.7)
    -> SHIPMENT CREATION (READY_TO_SHIP)
    -> TRACKING EVENT INGESTION (IN_TRANSIT -> DELIVERED)
    -> SHIPPING LABEL GENERATION
    -> LOGISTICS RECONCILIATION & AUDIT
    """
    # 1. Canales y repositorios
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        metadata={"user_id": "12345678"},
    )
    order_repo = InMemoryOrderRepository()
    fulfillment_repo = InMemoryFulfillmentRepository()

    # 2. Mock API Client de Mercado Libre
    api_client = MagicMock(spec=MercadoLibreApiClient)

    def mock_get(path, *args, **kwargs):
        if path == "/orders/987654321":
            return {
                "id": 987654321,
                "status": "paid",
                "shipping": {"id": 99887766},
            }
        elif path == "/shipments/99887766":
            return {
                "id": 99887766,
                "order_id": 987654321,
                "status": "shipped",
                "substatus": "in_transit",
                "logistic_type": "drop_off",
                "tracking_number": "TRK-CHL-9988",
                "tracking_method": "Chilexpress",
                "date_created": "2026-08-31T10:00:00.000Z",
                "last_updated": "2026-08-31T11:00:00.000Z",
                "substatus_history": [
                    {
                        "date": "2026-08-31T10:30:00.000Z",
                        "status": "ready_to_ship",
                        "description": "Ready to drop off",
                    },
                    {
                        "date": "2026-08-31T11:00:00.000Z",
                        "status": "in_transit",
                        "description": "Package in transit with Chilexpress",
                    },
                ],
            }
        return {}

    api_client.get.side_effect = mock_get

    fulfillment_adapter = MercadoLibreFulfillmentAdapter(api_client=api_client)
    fulfillment_service = FulfillmentService(
        fulfillment_port=fulfillment_adapter,
        fulfillment_repository=fulfillment_repo,
        order_repository=order_repo,
    )

    # 3. Orden confirmada y pagada (G.6)
    order = Order(
        order_id="ord_e2e_01",
        external_order_id="987654321",
        channel=channel,
        status=OrderStatus.CONFIRMED,
        payment_status=PaymentStatus.APPROVED,
        fulfillment_status=FulfillmentStatus.READY_TO_SHIP,
        items=(
            OrderItem(
                item_id="item_e2e_01",
                title="Logitech MX Master 3S Mouse",
                quantity=1,
                unit_price=Decimal("95000"),
                currency="CLP",
            ),
        ),
        total_amount=Decimal("95000"),
        currency="CLP",
        buyer=BuyerReference(buyer_id="buyer_e2e_01", nickname="buyer_one"),
    )
    order_repo.save_order(order)

    # 4. Preparación de Fulfillment (G.7)
    shipment = fulfillment_service.prepare_fulfillment(
        order=order,
        service_level=ShippingServiceLevel.ME2_DROP_OFF,
    )
    assert shipment is not None
    assert shipment.external_shipment_id == "99887766"
    assert shipment.status == ShipmentStatus.IN_TRANSIT
    assert shipment.tracking_number == "TRK-CHL-9988"

    # 5. Generación de Etiqueta (Label)
    label = fulfillment_service.generate_shipping_label(
        shipment_id=shipment.shipment_id,
        channel=channel,
    )
    assert label is not None
    assert label.status == LabelStatus.READY
    assert label.format == LabelFormat.PDF

    # 6. Ingesta de evento de tracking
    track_event = TrackingEvent(
        event_id="trk_ev_delivered_01",
        shipment_id=shipment.shipment_id,
        external_shipment_id="99887766",
        status=TrackingStatus.DELIVERED,
        normalized_status=ShipmentStatus.DELIVERED,
        description="Entregado al comprador en destino",
        idempotency_key="idemp_track_del_01",
    )
    ingested = fulfillment_service.record_tracking_event(track_event, channel_id=channel.channel_id)
    assert ingested is True

    # 7. Reconciliación logística
    api_client.get.side_effect = lambda path, *args, **kwargs: {
        "id": 99887766,
        "order_id": 987654321,
        "status": "delivered",
        "logistic_type": "drop_off",
        "tracking_number": "TRK-CHL-9988",
    } if path == "/shipments/99887766" else {}
    reconciliation = fulfillment_service.reconcile_shipment(
        shipment_id=shipment.shipment_id,
        channel=channel,
    )
    assert reconciliation.is_reconciled is True
    assert reconciliation.internal_status == ShipmentStatus.DELIVERED
    assert reconciliation.external_status == ShipmentStatus.DELIVERED
    assert len(reconciliation.discrepancies) == 0


def test_e2e_fulfillment_duplicate_event_and_idempotency_safety():
    """
    E2E SCENARIO B: DUPLICATE / IDEMPOTENCY SAFETY
    Demuestra que eventos repetidos de webhook o polling de tracking no duplican
    efectos ni registros en la base de datos de fulfillment.
    """
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="ML Chile")
    repo = InMemoryFulfillmentRepository()
    mock_port = MagicMock()
    service = FulfillmentService(fulfillment_port=mock_port, fulfillment_repository=repo)

    initial_shipment = Shipment(
        shipment_id="shp_idemp_100",
        external_shipment_id="ext_shp_100",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.READY_TO_SHIP,
    )
    repo.save_shipment(initial_shipment)

    event = TrackingEvent(
        event_id="ev_dup_999",
        shipment_id="shp_idemp_100",
        external_shipment_id="ext_shp_100",
        status=TrackingStatus.IN_TRANSIT,
        normalized_status=ShipmentStatus.IN_TRANSIT,
        idempotency_key="key_dup_999",
    )

    # Ingesta 1
    res1 = service.record_tracking_event(event, channel_id="ML-CL")
    # Ingesta 2 (duplicada)
    res2 = service.record_tracking_event(event, channel_id="ML-CL")

    assert res1 is True
    assert res2 is False
    assert len(repo.get_tracking_events("shp_idemp_100")) == 1


def test_e2e_fulfillment_unknown_preservation_on_external_failure():
    """
    E2E SCENARIO C: UNKNOWN & RECOVERY
    Demuestra que ante fallos 5xx, timeout o respuestas ambiguas del canal, el sistema
    preserva UNKNOWN con LOW Confidence sin sobreescribir destructivamente el estado local.
    """
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="ML Chile")
    repo = InMemoryFulfillmentRepository()
    api_client = MagicMock(spec=MercadoLibreApiClient)
    
    # Simular fallo de red 500
    api_client.get.side_effect = Exception("HTTP 500 Internal Server Error")

    adapter = MercadoLibreFulfillmentAdapter(api_client=api_client)
    service = FulfillmentService(fulfillment_port=adapter, fulfillment_repository=repo)

    # Estado local previamente confirmado
    local_shipment = Shipment(
        shipment_id="shp_unk_500",
        external_shipment_id="ext_500",
        order_id="ord_500",
        external_order_id="ext_ord_500",
        channel=channel,
        status=ShipmentStatus.IN_TRANSIT,
    )
    repo.save_shipment(local_shipment)

    synced = service.sync_shipment(external_shipment_id="ext_500", channel=channel)
    assert synced is not None
    # No sobrescribió con UNKNOWN ni destruyó el estado previo
    assert synced.status == ShipmentStatus.IN_TRANSIT
    assert repo.get_shipment_by_id("shp_unk_500").status == ShipmentStatus.IN_TRANSIT


def test_e2e_fulfillment_reconciliation_discrepancy_and_policy_governance():
    """
    E2E SCENARIO D: DISCREPANCY & POLICY GOVERNANCE
    Demuestra la detección de discrepancias entre estado local y remoto,
    y que cualquier acción operativa logística requiere validación de políticas.
    """
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="ML Chile")
    repo = InMemoryFulfillmentRepository()
    mock_port = MagicMock()
    policy_engine = PolicyEngine()
    service = FulfillmentService(
        fulfillment_port=mock_port,
        fulfillment_repository=repo,
        policy_engine=policy_engine,
    )

    local_shipment = Shipment(
        shipment_id="shp_disc_01",
        external_shipment_id="ext_disc_01",
        order_id="ord_disc_01",
        external_order_id="ext_ord_disc_01",
        channel=channel,
        status=ShipmentStatus.READY_TO_SHIP,
        tracking_number="TRK_OLD",
    )
    repo.save_shipment(local_shipment)

    # El canal remoto ya reporta entregado con nuevo tracking
    remote_shipment = Shipment(
        shipment_id="shp_disc_01",
        external_shipment_id="ext_disc_01",
        order_id="ord_disc_01",
        external_order_id="ext_ord_disc_01",
        channel=channel,
        status=ShipmentStatus.DELIVERED,
        tracking_number="TRK_NEW",
    )
    mock_port.get_shipment_by_external_id.return_value = ShipmentQueryResult(
        shipments=(remote_shipment,),
        total_count=1,
        channel=channel,
    )

    reconciliation = service.reconcile_shipment("shp_disc_01", channel)
    assert reconciliation.is_reconciled is False
    assert reconciliation.requires_action is True
    assert len(reconciliation.discrepancies) == 2

    # Ejecución de acción operativa pasando por gobernanza de políticas
    action_res = service.execute_fulfillment_action_guarded(
        action_name="NOTIFY_DELIVERY_CONFIRMED",
        shipment=remote_shipment,
        payload={"note": "Sync completed"},
        correlation_id="corr_disc_01",
    )
    assert action_res["success"] is True
    assert action_res["status"] == "EXECUTED"


def test_e2e_fulfillment_tool_registry_publication():
    """
    E2E SCENARIO E: TOOL REGISTRY DISCOVERY & VERIFICATION
    Verifica que las 6 herramientas logísticas estén publicadas en el ToolRegistry
    con sus contratos correctos.
    """
    registry = ToolRegistry()
    register_standard_commerce_tools(registry)

    expected_tools = [
        "get_shipments",
        "get_shipment",
        "get_tracking",
        "reconcile_shipment",
        "prepare_fulfillment",
        "create_shipping_label",
    ]

    for tool_name in expected_tools:
        descriptor = registry.get(tool_name)
        assert descriptor is not None
        assert descriptor.tool_id == tool_name
        assert "logistics" in descriptor.tags or "fulfillment" in descriptor.tags
        assert descriptor.status.value in ("REGISTERED", "AVAILABLE")
