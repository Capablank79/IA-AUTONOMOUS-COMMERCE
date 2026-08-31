import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# H.1 & H.2 imports
from src.domain.mission.models import Mission, MissionStatus, MissionType
from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
from src.domain.decision.models import (
    DecisionRecord,
    DecisionType,
    DecisionStatus,
    DecisionOutcome,
    DecisionEvidenceReference,
)
from src.infrastructure.persistence.data.json.decision_repository import JsonDecisionRepository
from src.application.decision.decision_service import DecisionMemoryService

# H.3 Action imports
from src.domain.action.models import ActionRecord, ActionStatus
from src.infrastructure.persistence.data.json.action_repository import JsonActionRepository
from src.application.action.action_service import ActionMemoryService

# H.4 Result imports
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.infrastructure.persistence.data.json.result_repository import JsonResultRepository
from src.application.result.result_service import ResultMemoryService

# H.5 Product Memory imports
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.product_memory.models import ProductMemoryRecord
from src.infrastructure.persistence.data.json.product_memory_repository import JsonProductMemoryRepository
from src.application.product_memory.product_memory_service import ProductMemoryService

# H.6 Supplier Memory imports
from src.domain.supplier_intelligence.models import SupplierStatus, EvidenceProvenanceType, SupplierReadiness
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.infrastructure.persistence.data.json.supplier_memory_repository import JsonSupplierMemoryRepository
from src.application.supplier_memory.supplier_memory_service import SupplierMemoryService

# H.7 Temporal State imports
from src.domain.temporal_state.models import TemporalSnapshot
from src.infrastructure.persistence.data.json.temporal_state_repository import JsonTemporalStateRepository
from src.application.temporal_state.temporal_state_service import TemporalStateService


def test_hito_h_full_business_memory_e2e_and_restart():
    """
    Prueba de integración E2E completa de Hito H:
    MISSION -> DECISION -> ACTION -> RESULT -> PRODUCT CONTEXT -> SUPPLIER CONTEXT -> TEMPORAL STATE
    Demuestra persistencia durable, trazabilidad de referencias, no mutación y reconstrucción tras reinicio.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = Path(tmpdir)

        mission_repo = JsonMissionRepository(base_path / "missions.json")
        decision_repo = JsonDecisionRepository(base_path / "decisions.json")
        decision_service = DecisionMemoryService(decision_repo)

        action_repo = JsonActionRepository(base_path / "actions.json")
        action_service = ActionMemoryService(action_repo)

        result_repo = JsonResultRepository(base_path / "results.json")
        result_service = ResultMemoryService(result_repo)

        product_repo = JsonProductMemoryRepository(base_path / "product_memory.json")
        product_service = ProductMemoryService(product_repo)

        supplier_repo = JsonSupplierMemoryRepository(base_path / "supplier_memory.json")
        supplier_service = SupplierMemoryService(supplier_repo)

        temporal_repo = JsonTemporalStateRepository(base_path / "temporal_state.json")
        temporal_service = TemporalStateService(temporal_repo)

        # 1. Create Mission (H.1)
        mission_id = "miss-hito-h-001"
        mission = Mission(
            mission_id=mission_id,
            type=MissionType.MARKET_DISCOVERY,
            parameters={"target_niche": "Smart Home"},
        )
        mission_repo.save(mission)

        # 2. Create Decision linked to Mission (H.2)
        decision = decision_service.record_decision(
            mission_id=mission_id,
            decision_type=DecisionType.PUBLICATION_STRATEGY,
            reason="Market demand high, supplier profit margin > 35%",
            target_resource="SKU-SMART-PLUG-01",
            idempotency_key="idemp-dec-001",
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
        )
        decision_id = decision.decision_id

        # 3. Create Action linked to Decision and Mission (H.3)
        action_id = "act-hito-h-001"
        action = action_service.record_action(
            action_id=action_id,
            decision_id=decision_id,
            mission_id=mission_id,
            action_type="CREATE_MERCADO_LIBRE_LISTING",
            target_resource="MLC-SMART-PLUG",
            parameters={"title": "Enchufe Inteligente WiFi", "price": 14990, "api_key": "HIDE_ME"},
            idempotency_key="idemp-act-001",
            policy_reference="pol-publishing-v1",
            approval_reference="appr-auto-pass",
        )
        action_service.update_action_status(action_id, ActionStatus.COMPLETED)

        # 4. Create Result linked to Action, Decision and Mission (H.4)
        result_id = "res-hito-h-001"
        result = result_service.record_result(
            result_id=result_id,
            action_id=action_id,
            decision_id=decision_id,
            mission_id=mission_id,
            outcome=ResultOutcome.SUCCESS,
            response_summary={"listing_id": "MLC-88776655", "status": "active"},
            evidence_reference="evid-mlc-published",
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
            idempotency_key="idemp-res-001",
        )

        # 5. Record Product Memory Context (H.5)
        prod_mem = product_service.record_product_memory(
            product_memory_id="pm-smart-plug-01",
            sku="SKU-SMART-PLUG-01",
            external_id="MLC-88776655",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Enchufe Inteligente WiFi",
            category="Hogar Inteligente",
            price_amount=Decimal("14990"),
            sold_quantity=0,
            available_quantity=20,
            seller_id="SELLER-ME",
            evidence_reference="evid-mlc-published",
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
        )

        # 6. Record Supplier Memory Context (H.6)
        supp_mem = supplier_service.record_supplier_memory(
            supplier_memory_id="sm-supplier-plug-01",
            supplier_id="SUP-SHENZHEN-PLUG",
            name="Shenzhen Smart Tech",
            status=SupplierStatus.VERIFIED,
            sku="SKU-SMART-PLUG-01",
            cost_amount=Decimal("5500"),
            moq=50,
            lead_time_days=12,
            source="DIRECT_FACTORY_DIRECTORY",
            evidence_reference="evid-supplier-quote-pdf",
            verification_status=SupplierReadiness.READY_FOR_ECONOMICS,
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
        )

        # 7. Record Temporal State Snapshots (H.7)
        t0 = datetime(2026, 8, 31, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

        snap0 = temporal_service.record_snapshot(
            snapshot_id="snap-plug-t0",
            entity_type="PRODUCT_LISTING_STATE",
            entity_id="SKU-SMART-PLUG-01",
            state_payload={"price": 16990, "stock": 20},
            timestamp=t0,
        )

        snap1 = temporal_service.record_snapshot(
            snapshot_id="snap-plug-t1",
            entity_type="PRODUCT_LISTING_STATE",
            entity_id="SKU-SMART-PLUG-01",
            state_payload={"price": 14990, "stock": 20},
            timestamp=t1,
        )

        # =========================================================================
        # RESTART / RECONSTRUCTION SIMULATION
        # Re-instantiate all repositories from saved files without in-memory state.
        # =========================================================================
        fresh_mission_repo = JsonMissionRepository(base_path / "missions.json")
        fresh_decision_repo = JsonDecisionRepository(base_path / "decisions.json")
        fresh_action_repo = JsonActionRepository(base_path / "actions.json")
        fresh_result_repo = JsonResultRepository(base_path / "results.json")
        fresh_product_repo = JsonProductMemoryRepository(base_path / "product_memory.json")
        fresh_supplier_repo = JsonSupplierMemoryRepository(base_path / "supplier_memory.json")
        fresh_temporal_repo = JsonTemporalStateRepository(base_path / "temporal_state.json")

        # 1. Verify Mission
        re_mission = fresh_mission_repo.get_by_id(mission_id)
        assert re_mission is not None
        assert re_mission.mission_id == mission_id

        # 2. Verify Decision & linkage
        re_decisions = fresh_decision_repo.get_by_mission_id(mission_id)
        assert len(re_decisions) == 1
        re_decision = re_decisions[0]
        assert re_decision.decision_id == decision_id
        assert re_decision.decision_type == DecisionType.PUBLICATION_STRATEGY

        # 3. Verify Action & linkages
        re_actions_dec = fresh_action_repo.get_by_decision_id(decision_id)
        re_actions_miss = fresh_action_repo.get_by_mission_id(mission_id)
        assert len(re_actions_dec) == 1
        assert len(re_actions_miss) == 1
        re_action = re_actions_dec[0]
        assert re_action.action_id == action_id
        assert re_action.status == ActionStatus.COMPLETED
        assert re_action.policy_reference == "pol-publishing-v1"
        assert "api_key" not in re_action.parameters  # Sanitization check

        # 4. Verify Result & linkages
        re_result_act = fresh_result_repo.get_by_action_id(action_id)
        assert re_result_act is not None
        assert re_result_act.result_id == result_id
        assert re_result_act.outcome == ResultOutcome.SUCCESS
        assert re_result_act.evidence_reference == "evid-mlc-published"

        # 5. Verify Product Memory
        re_prod_mem = fresh_product_repo.get_by_sku("SKU-SMART-PLUG-01")
        assert re_prod_mem is not None
        assert re_prod_mem.external_id == "MLC-88776655"
        assert re_prod_mem.price_amount == Decimal("14990")

        # 6. Verify Supplier Memory
        re_supp_mems = fresh_supplier_repo.get_by_sku("SKU-SMART-PLUG-01")
        assert len(re_supp_mems) == 1
        assert re_supp_mems[0].supplier_id == "SUP-SHENZHEN-PLUG"
        assert re_supp_mems[0].cost_amount == Decimal("5500")

        # 7. Verify Temporal State Reconstruction
        # At T0 -> price 16990
        state_t0 = fresh_temporal_repo.get_state_at("PRODUCT_LISTING_STATE", "SKU-SMART-PLUG-01", t0)
        assert state_t0 is not None
        assert state_t0.state_payload["price"] == 16990

        # At T1 -> price 14990
        state_t1 = fresh_temporal_repo.get_state_at("PRODUCT_LISTING_STATE", "SKU-SMART-PLUG-01", t1)
        assert state_t1 is not None
        assert state_t1.state_payload["price"] == 14990
