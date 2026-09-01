import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

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
from src.infrastructure.persistence.data.json.market_observation_repository import (
    JsonMarketObservationRepository,
    CorruptedMarketObservationDataError,
)
from src.domain.scheduling.models import DeterministicClock


class FakeObservationSource(MarketObservationSourcePort):
    def __init__(self, name: str, items: Optional[List[MarketObservation]] = None, fail_with_exception: bool = False):
        self._name = name
        self._items = items or []
        self._fail = fail_with_exception

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
        if self._fail:
            raise ConnectionError(f"Remote connection failure in {self._name}")
        return self._items


def test_a_observation_creation():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-001",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-123",
        title="Test Item",
        price=NormalizedPrice(amount=Decimal("15990.00"), currency="CLP"),
        stock=10,
        sold_quantity=5,
    )
    assert obs.observation_id == "obs-001"
    assert obs.price.amount == Decimal("15990.00")
    assert obs.stock == 10
    assert obs.sold_quantity == 5
    assert obs.signal_type == SignalType.OBSERVED
    assert obs.confidence == Confidence.HIGH
    assert obs.idempotency_key != ""


def test_b_source_identification():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-002",
        source="MERCADOLIBRE_LIVE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-999",
    )
    assert obs.source == "MERCADOLIBRE_LIVE"
    assert obs.source_type == ObservationSourceType.MARKETPLACE_API


def test_c_normalization():
    price = NormalizedPrice(amount=Decimal("19990.50"), currency="CLP")
    assert price.amount == Decimal("19990.50")
    assert price.currency == "CLP"


def test_d_missing_values():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-003",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-NO-STOCK",
        stock=None,
        sold_quantity=None,
    )
    assert obs.stock is None
    assert obs.sold_quantity is None


def test_e_unknown_preservation():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-004",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.GENERIC,
        entity_id="ITEM-UNKNOWN",
        stock=None,
        sold_quantity=None,
        confidence=Confidence.UNKNOWN,
    )
    assert obs.stock is not 0
    assert obs.stock is None
    assert obs.sold_quantity is not 0
    assert obs.sold_quantity is None
    assert obs.confidence == Confidence.UNKNOWN


def test_f_invalid_payload_rejection():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        NormalizedPrice(amount=Decimal("-10.00"), currency="CLP")

    with pytest.raises(ValueError):
        MarketObservation(
            observation_id="obs-005",
            source="TEST_SRC",
            source_type=ObservationSourceType.MARKETPLACE_API,
            observed_at=now,
            collected_at=now,
            marketplace=Marketplace.GENERIC,
            entity_id="ITEM-BAD-STOCK",
            stock=-5,
        )


def test_g_and_h_duplicate_and_idempotency(tmp_path):
    repo = JsonMarketObservationRepository(tmp_path)
    clock = DeterministicClock()
    now = clock.now()

    obs = MarketObservation(
        observation_id="obs-dup-1",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-1",
        correlation_id="corr-fixed",
    )

    # Primera inserción
    repo.save(obs)
    assert len(repo.list_all()) == 1

    # Segunda inserción con id idéntico
    repo.save(obs)
    assert len(repo.list_all()) == 1

    # Inserción con nuevo observation_id pero misma clave de idempotencia
    obs_duplicate = MarketObservation(
        observation_id="obs-dup-2",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-1",
        correlation_id="corr-fixed",
        idempotency_key=obs.idempotency_key,
    )
    repo.save(obs_duplicate)
    assert len(repo.list_all()) == 1


def test_i_provenance_and_confidence():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-prov",
        source="MERCADOLIBRE_LIVE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-PROV",
        provenance="LIVE",
        confidence=Confidence.HIGH,
    )
    assert obs.provenance == "LIVE"
    assert obs.confidence == Confidence.HIGH


def test_j_and_k_timestamp_and_correlation():
    now = datetime(2026, 9, 1, 12, 30, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-ts",
        source="SRC_A",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.GENERIC,
        entity_id="ITEM-TS",
        correlation_id="corr-custom-xyz",
    )
    assert obs.observed_at.tzinfo == timezone.utc
    assert obs.collected_at.tzinfo == timezone.utc
    assert obs.correlation_id == "corr-custom-xyz"


def test_l_sensitive_data_exclusion(tmp_path):
    repo = JsonMarketObservationRepository(tmp_path)
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

    obs = MarketObservation(
        observation_id="obs-sec-1",
        source="SRC_SEC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.GENERIC,
        entity_id="ITEM-SEC",
        raw_payload={
            "product_name": "Safe item",
            "access_token": "secret_token_12345",
            "api_key": "private_api_key_abc",
            "metadata": {
                "password": "super_secret_pwd",
                "nested_token": "bearer xyz",
                "safe_info": "visible",
            },
        },
    )

    repo.save(obs)

    # Recargar desde disco y verificar sanitización
    loaded = repo.get_by_id("obs-sec-1")
    assert loaded is not None
    assert "product_name" in loaded.raw_payload
    assert "access_token" not in loaded.raw_payload
    assert "api_key" not in loaded.raw_payload
    assert "password" not in loaded.raw_payload["metadata"]
    assert "nested_token" not in loaded.raw_payload["metadata"]
    assert loaded.raw_payload["metadata"]["safe_info"] == "visible"


def test_m_source_failure_handling(tmp_path):
    repo = JsonMarketObservationRepository(tmp_path)
    clock = DeterministicClock()
    service = MarketMonitoringService(repository=repo, clock=clock)

    failing_source = FakeObservationSource("FAILING_SOURCE", fail_with_exception=True)
    service.register_source(failing_source)

    results = service.monitor(source_name="FAILING_SOURCE", entity_id="ITEM-ERR")
    assert len(results) == 1
    assert results[0].status == ObservationStatus.SOURCE_FAILURE
    assert "Unhandled exception in source FAILING_SOURCE" in results[0].error_message
    assert results[0].confidence == Confidence.UNKNOWN

    # Verificar que quedó registrado en persistencia
    assert len(repo.list_all()) == 1


def test_n_multiple_sources_through_same_port(tmp_path):
    repo = JsonMarketObservationRepository(tmp_path)
    clock = DeterministicClock()
    service = MarketMonitoringService(repository=repo, clock=clock)
    now = clock.now()

    src_a = FakeObservationSource("SOURCE_A", [
        MarketObservation(
            observation_id="obs-a1",
            source="SOURCE_A",
            source_type=ObservationSourceType.MARKETPLACE_API,
            observed_at=now,
            collected_at=now,
            marketplace=Marketplace.MERCADO_LIBRE,
            entity_id="ITEM-A",
            title="Item From A",
        )
    ])

    src_b = FakeObservationSource("SOURCE_B", [
        MarketObservation(
            observation_id="obs-b1",
            source="SOURCE_B",
            source_type=ObservationSourceType.CATALOG_API,
            observed_at=now,
            collected_at=now,
            marketplace=Marketplace.AMAZON,
            entity_id="ITEM-B",
            title="Item From B",
        )
    ])

    service.register_source(src_a)
    service.register_source(src_b)

    all_obs = service.monitor()
    assert len(all_obs) == 2
    sources = {o.source for o in all_obs}
    assert sources == {"SOURCE_A", "SOURCE_B"}
    assert len(repo.list_all()) == 2


def test_o_observed_vs_derived_separation():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-observed",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-OBS",
        signal_type=SignalType.OBSERVED,
    )
    assert obs.signal_type == SignalType.OBSERVED
    assert not hasattr(obs, "opportunity_score")


def test_p_restart_safe_persistence(tmp_path):
    # Instancia 1 de repositorio
    repo1 = JsonMarketObservationRepository(tmp_path)
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-restart-1",
        source="TEST_SRC",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-RESTART",
        title="Restart Test Item",
        price=NormalizedPrice(amount=Decimal("4990.00"), currency="CLP"),
    )
    repo1.save(obs)

    # Destruir / Simular reinicio creando repo2 sobre la misma ruta
    del repo1
    repo2 = JsonMarketObservationRepository(tmp_path)
    reloaded = repo2.get_by_id("obs-restart-1")

    assert reloaded is not None
    assert reloaded.observation_id == "obs-restart-1"
    assert reloaded.title == "Restart Test Item"
    assert reloaded.price.amount == Decimal("4990.00")
    assert len(repo2.list_all()) == 1
