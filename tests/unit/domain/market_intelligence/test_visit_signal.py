import pytest
from datetime import datetime
from src.domain.market_intelligence.models import VisitSignal, Confidence

def test_visit_signal_valid_with_visits():
    now = datetime.utcnow()
    signal = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=140,
        observed_days=14,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=now,
        confidence=Confidence.HIGH
    )
    
    assert signal.item_id == "MLC123"
    assert signal.total_visits == 140
    assert signal.daily_average == 10.0
    assert signal.confidence == Confidence.HIGH

def test_visit_signal_with_total_visits_none():
    now = datetime.utcnow()
    signal = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=None,
        observed_days=14,
        coverage_ratio=0.5,
        source="mercadolibre_visits",
        observed_at=now
    )
    
    assert signal.total_visits is None
    assert signal.daily_average is None
    assert signal.confidence == Confidence.UNKNOWN

def test_visit_signal_with_total_visits_zero():
    now = datetime.utcnow()
    signal = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=0,
        observed_days=14,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=now
    )
    
    assert signal.total_visits == 0
    assert signal.daily_average == 0.0

def test_visit_signal_rejects_negative_visits():
    now = datetime.utcnow()
    with pytest.raises(ValueError, match="total_visits cannot be negative"):
        VisitSignal(
            item_id="MLC123",
            window="14d",
            total_visits=-1,
            observed_days=14,
            coverage_ratio=1.0,
            source="mercadolibre_visits",
            observed_at=now
        )

def test_visit_signal_rejects_negative_observed_days():
    now = datetime.utcnow()
    with pytest.raises(ValueError, match="observed_days cannot be negative"):
        VisitSignal(
            item_id="MLC123",
            window="14d",
            total_visits=100,
            observed_days=-1,
            coverage_ratio=1.0,
            source="mercadolibre_visits",
            observed_at=now
        )

def test_visit_signal_rejects_invalid_coverage_ratio():
    now = datetime.utcnow()
    with pytest.raises(ValueError, match="coverage_ratio must be between 0 and 1"):
        VisitSignal(
            item_id="MLC123",
            window="14d",
            total_visits=100,
            observed_days=14,
            coverage_ratio=1.5,
            source="mercadolibre_visits",
            observed_at=now
        )

def test_visit_signal_daily_average_only_when_data_present():
    now = datetime.utcnow()
    
    # Caso 1: Sin visitas
    signal_no_visits = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=None,
        observed_days=14,
        coverage_ratio=0.5,
        source="mercadolibre_visits",
        observed_at=now
    )
    assert signal_no_visits.daily_average is None
    
    # Caso 2: Sin días observados
    signal_no_days = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=100,
        observed_days=0,
        coverage_ratio=0.0,
        source="mercadolibre_visits",
        observed_at=now
    )
    assert signal_no_days.daily_average is None
    
    # Caso 3: Datos completos
    signal_ok = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=100,
        observed_days=10,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=now
    )
    assert signal_ok.daily_average == 10.0

def test_visit_signal_none_is_not_zero():
    now = datetime.utcnow()
    signal_none = VisitSignal(
        item_id="MLC123",
        window="14d",
        total_visits=None,
        observed_days=14,
        coverage_ratio=0.5,
        source="mercadolibre_visits",
        observed_at=now
    )
    assert signal_none.total_visits is not 0
    assert signal_none.total_visits is None
