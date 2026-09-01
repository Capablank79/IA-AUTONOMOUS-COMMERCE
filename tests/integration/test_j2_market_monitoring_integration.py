import os
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any, Tuple

from src.domain.market_monitoring.models import (
    MarketObservation,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
    ObservationSourceType,
    ObservationStatus,
)
from src.domain.market_monitoring.ports import MarketObservationSourcePort
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.application.market_monitoring.service import MarketMonitoringService
from src.infrastructure.persistence.data.json.market_observation_repository import JsonMarketObservationRepository

from src.domain.mission.models import MissionType, MissionPriority
from src.domain.scheduling.models import (
    Schedule,
    ScheduleConfig,
    ScheduleStatus,
    ScheduleType,
    ExecutionStatus,
    DeterministicClock,
)
from src.domain.scheduling.ports import MissionTriggerPort
from src.application.scheduling.service import SchedulerService
from src.infrastructure.persistence.data.json.schedule_repository import JsonScheduleRepository
from src.infrastructure.market_monitoring.mercadolibre_adapter import MercadoLibreObservationAdapter


class FakeApiClient:
    """Mock client de API de Mercado Libre para simulación determinista."""
    def __init__(self):
        self.routes = {}
        self.should_timeout = False

    def get(self, path: str):
        if self.should_timeout:
            raise TimeoutError("MercadoLibre API gateway timeout")
        if path in self.routes:
            return self.routes[path]
        if "/products/search" in path:
            return self.routes.get("/products/search", {"results": [], "paging": {"total": 0}})
        if "/items/" in path:
            item_id = path.split("/items/")[1]
            return self.routes.get(f"/items/{item_id}", {"id": item_id, "title": "Default Item", "price": 9990, "available_quantity": 10})
        raise ValueError(f"Route {path} not mocked")


class MarketMonitoringTrigger(MissionTriggerPort):
    """
    Trigger adaptador que conecta las ocurrencias del Scheduler J.1
    con la ejecución del servicio de Market Monitoring J.2.
    """
    def __init__(self, monitoring_service: MarketMonitoringService):
        self.monitoring_service = monitoring_service
        self.executions = []

    def trigger(
        self,
        schedule: Schedule,
        occurrence: Any,
    ) -> Tuple[str, ExecutionStatus, Optional[Dict[str, Any]], Optional[str]]:
        params = schedule.mission_parameters or {}
        source_name = params.get("source_name")
        query = params.get("query")
        entity_id = params.get("entity_id")
        category = params.get("category")
        limit = params.get("limit", 50)
        correlation_id = schedule.correlation_id

        obs_list = self.monitoring_service.monitor(
            source_name=source_name,
            query=query,
            entity_id=entity_id,
            category=category,
            limit=limit,
            correlation_id=correlation_id,
        )
        self.executions.append({
            "correlation_id": correlation_id,
            "observations_count": len(obs_list),
            "observations": obs_list,
        })
        return f"exec-mon-{correlation_id}", ExecutionStatus.SUCCESS, {"count": len(obs_list)}, None


def test_j2_integration_scheduler_to_market_monitor(tmp_path):
    """
    Demuestra el flujo completo integrado:
    SCHEDULER (J.1)
    → TICK
    → TRIGGER
    → MARKET MONITOR (J.2)
    → SOURCE ADAPTER (ML)
    → NORMALIZATION
    → OBSERVATION
    → ATOMIC PERSISTENCE
    → RELOAD REPOSITORIES.
    """
    clock = DeterministicClock(datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc))

    # Repositorios
    sched_repo = JsonScheduleRepository(tmp_path / "scheduling")
    mon_repo = JsonMarketObservationRepository(tmp_path / "monitoring")

    # API Client & Source Adapter
    api_client = FakeApiClient()
    api_client.routes["/products/search?limit=10&site_id=MLC&q=auriculares+bluetooth"] = {
        "results": [
            {
                "id": "MLC-PROD-001",
                "name": "Auriculares Bluetooth Pro X",
                "domain_id": "MLC-HEADPHONES",
                "sold_quantity": 120,
                "buy_box_winner": {
                    "price": 29990.00,
                    "currency_id": "CLP",
                    "available_quantity": 45,
                    "seller_id": "SELLER-99",
                }
            }
        ],
        "paging": {"total": 1}
    }
    ml_adapter = MercadoLibreObservationAdapter(api_client, source_name="MERCADOLIBRE_LIVE")

    # Market Monitoring Service
    mon_service = MarketMonitoringService(repository=mon_repo, sources=[ml_adapter], clock=clock)

    # Mission Trigger Port & Scheduler Service
    trigger = MarketMonitoringTrigger(mon_service)
    scheduler = SchedulerService(repository=sched_repo, trigger=trigger, clock=clock)

    # 1. Crear Schedule en J.1 para monitoreo cada 300 segundos (5 minutos)
    schedule = scheduler.create_schedule(
        schedule_id="sched-market-mon-1",
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={
            "source_name": "MERCADOLIBRE_LIVE",
            "query": "auriculares bluetooth",
            "limit": 10,
        },
        schedule_type=ScheduleType.INTERVAL,
        interval_seconds=300,
    )
    assert schedule.status == ScheduleStatus.ACTIVE
    assert schedule.next_run_at == clock.now()

    # 2. Tick inicial -> Ejecución de monitoreo
    executed = scheduler.tick()
    assert len(executed) == 1
    assert executed[0].status == ExecutionStatus.SUCCESS
    assert len(trigger.executions) == 1
    assert trigger.executions[0]["observations_count"] == 1

    # Verificar observación en persistencia de monitoreo
    all_obs = mon_repo.list_all()
    assert len(all_obs) == 1
    obs = all_obs[0]
    assert obs.entity_id == "MLC-PROD-001"
    assert obs.title == "Auriculares Bluetooth Pro X"
    assert obs.price.amount == Decimal("29990.00")
    assert obs.price.currency == "CLP"
    assert obs.stock == 45
    assert obs.sold_quantity == 120
    assert obs.availability == "IN_STOCK"
    assert obs.confidence == Confidence.HIGH
    assert obs.signal_type == SignalType.OBSERVED
    assert obs.seller_info.seller_id == "SELLER-99"

    # 3. Avanzar el reloj menos del intervalo -> No debe ejecutar
    clock.advance(100)
    executed_early = scheduler.tick()
    assert len(executed_early) == 0

    # 4. Avanzar el reloj hasta cumplir el intervalo (total 300s desde t0)
    clock.advance(200)
    executed_due = scheduler.tick()
    assert len(executed_due) == 1
    assert executed_due[0].status == ExecutionStatus.SUCCESS

    # Idempotencia: como es una nueva ocurrencia con nuevo correlation_id/timestamp, se procesa
    assert len(trigger.executions) == 2


# ============================================================
# ESCENARIOS E2E (A - G)
# ============================================================

def test_e2e_scenario_a_market_observation(tmp_path):
    """Escenario A: schedule due -> monitor executes -> source returns data -> observation persisted."""
    clock = DeterministicClock(datetime(2026, 9, 1, 14, 0, 0, tzinfo=timezone.utc))
    mon_repo = JsonMarketObservationRepository(tmp_path)
    api_client = FakeApiClient()
    api_client.routes["/items/MLC123456"] = {
        "id": "MLC123456",
        "title": "Smartwatch V8",
        "category_id": "MLC-WATCHES",
        "price": 45000,
        "currency_id": "CLP",
        "available_quantity": 8,
        "sold_quantity": 25,
        "seller_id": "SELLER-33",
    }
    adapter = MercadoLibreObservationAdapter(api_client)
    service = MarketMonitoringService(repository=mon_repo, sources=[adapter], clock=clock)

    results = service.monitor(entity_id="MLC123456")
    assert len(results) == 1
    assert results[0].status == ObservationStatus.SUCCESS
    assert results[0].price.amount == Decimal("45000")
    assert len(mon_repo.list_all()) == 1


def test_e2e_scenario_b_duplicate(tmp_path):
    """Escenario B: same occurrence -> source result processed twice -> ONE persisted observation."""
    clock = DeterministicClock(datetime(2026, 9, 1, 14, 0, 0, tzinfo=timezone.utc))
    mon_repo = JsonMarketObservationRepository(tmp_path)
    api_client = FakeApiClient()
    api_client.routes["/items/MLC-DUP"] = {
        "id": "MLC-DUP",
        "title": "Item Dup",
        "price": 10000,
        "available_quantity": 2,
    }
    adapter = MercadoLibreObservationAdapter(api_client)
    service = MarketMonitoringService(repository=mon_repo, sources=[adapter], clock=clock)

    # Primera llamada
    obs1 = service.monitor(entity_id="MLC-DUP", correlation_id="corr-same")
    assert len(obs1) == 1
    assert len(mon_repo.list_all()) == 1

    # Segunda llamada con idéntico payload / id
    mon_repo.save(obs1[0])
    assert len(mon_repo.list_all()) == 1


def test_e2e_scenario_c_restart(tmp_path):
    """Escenario C: observation persisted -> process destroyed -> reload -> observation retained."""
    storage_dir = tmp_path / "restart_repo"
    repo1 = JsonMarketObservationRepository(storage_dir)
    clock = DeterministicClock()
    service1 = MarketMonitoringService(repository=repo1, clock=clock)

    now = clock.now()
    obs = MarketObservation(
        observation_id="obs-restart-e2e",
        source="TEST_RESTART",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-RES",
        title="Persisted Item",
        price=NormalizedPrice(amount=Decimal("12500.00"), currency="CLP"),
    )
    repo1.save(obs)

    # Simular caída de proceso
    del service1
    del repo1

    # Recrear
    repo2 = JsonMarketObservationRepository(storage_dir)
    reloaded = repo2.get_by_id("obs-restart-e2e")
    assert reloaded is not None
    assert reloaded.title == "Persisted Item"
    assert reloaded.price.amount == Decimal("12500.00")


def test_e2e_scenario_d_missing_data(tmp_path):
    """Escenario D: source omits sold quantity / stock -> remains UNKNOWN/None."""
    clock = DeterministicClock()
    mon_repo = JsonMarketObservationRepository(tmp_path)
    api_client = FakeApiClient()
    api_client.routes["/items/MLC-OMIT"] = {
        "id": "MLC-OMIT",
        "title": "Item Without Sold Qty",
        "price": 20000,
        "available_quantity": None,
        "sold_quantity": None,
    }
    adapter = MercadoLibreObservationAdapter(api_client)
    service = MarketMonitoringService(repository=mon_repo, sources=[adapter], clock=clock)

    obs = service.monitor(entity_id="MLC-OMIT")
    assert len(obs) == 1
    assert obs[0].stock is None
    assert obs[0].sold_quantity is None
    assert obs[0].stock is not 0
    assert obs[0].sold_quantity is not 0


def test_e2e_scenario_e_source_failure(tmp_path):
    """Escenario E: source timeout -> no fabricated market data -> failure recorded."""
    clock = DeterministicClock()
    mon_repo = JsonMarketObservationRepository(tmp_path)
    api_client = FakeApiClient()
    api_client.should_timeout = True

    adapter = MercadoLibreObservationAdapter(api_client)
    service = MarketMonitoringService(repository=mon_repo, sources=[adapter], clock=clock)

    obs = service.monitor(entity_id="MLC-TIMEOUT")
    assert len(obs) == 1
    assert obs[0].status in (ObservationStatus.TIMEOUT, ObservationStatus.SOURCE_FAILURE)
    assert obs[0].price is None
    assert obs[0].stock is None
    assert obs[0].confidence == Confidence.UNKNOWN
    assert "timeout" in obs[0].error_message.lower()


def test_e2e_scenario_f_sensitive_data(tmp_path):
    """Escenario F: source metadata contains token/password/api_key -> persisted representation excludes sensitive values."""
    clock = DeterministicClock()
    mon_repo = JsonMarketObservationRepository(tmp_path)
    now = clock.now()

    obs = MarketObservation(
        observation_id="obs-sec-e2e",
        source="MERCADOLIBRE_LIVE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-SEC",
        raw_payload={
            "item_name": "Valid Name",
            "access_token": "secret_oauth_token",
            "refresh_token": "secret_refresh_token",
            "metadata": {
                "api_key": "secret_key",
                "authorization": "Bearer xyz",
                "safe_field": "valid_public_data"
            }
        }
    )
    mon_repo.save(obs)

    # Leer archivo crudo en disco y verificar que no existen las cadenas secretas
    disk_file = tmp_path / "obs-sec-e2e.json"
    content = disk_file.read_text(encoding="utf-8")
    assert "secret_oauth_token" not in content
    assert "secret_refresh_token" not in content
    assert "secret_key" not in content
    assert "Bearer xyz" not in content
    assert "valid_public_data" in content


class FakeCustomAdapter(MarketObservationSourcePort):
    def __init__(self, name: str, marketplace: Marketplace):
        self._name = name
        self._mp = marketplace

    @property
    def source_name(self) -> str:
        return self._name

    def observe(
        self,
        query: Optional[str] = None,
        entity_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 50,
        correlation_id: Optional[str] = None,
    ) -> List[MarketObservation]:
        now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        return [
            MarketObservation(
                observation_id=f"obs-{self._name}-1",
                source=self._name,
                source_type=ObservationSourceType.SIMULATED,
                observed_at=now,
                collected_at=now,
                marketplace=self._mp,
                entity_id=f"ITEM-{self._name}",
                title=f"Sample from {self._name}",
                price=NormalizedPrice(amount=Decimal("100.00"), currency="USD"),
            )
        ]


def test_e2e_scenario_g_multi_source_contract(tmp_path):
    """Escenario G: two deterministic fake adapters implement same port -> normalized observations generated through same application service."""
    clock = DeterministicClock()
    mon_repo = JsonMarketObservationRepository(tmp_path)
    service = MarketMonitoringService(repository=mon_repo, clock=clock)

    adapter1 = FakeCustomAdapter("SOURCE_ONE", Marketplace.MERCADO_LIBRE)
    adapter2 = FakeCustomAdapter("SOURCE_TWO", Marketplace.AMAZON)

    service.register_source(adapter1)
    service.register_source(adapter2)

    results = service.monitor()
    assert len(results) == 2
    assert {r.source for r in results} == {"SOURCE_ONE", "SOURCE_TWO"}
    assert {r.marketplace for r in results} == {Marketplace.MERCADO_LIBRE, Marketplace.AMAZON}
    assert all(r.price.amount == Decimal("100.00") for r in results)
