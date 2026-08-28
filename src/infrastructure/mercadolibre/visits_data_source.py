from datetime import datetime, timezone
from typing import Any, Dict

from src.domain.market_intelligence.models import Confidence, VisitSignal
from src.domain.market_intelligence.ports import VisitsDataSource
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MercadoLibreVisitsDataSource(VisitsDataSource):
    """
    Infrastructure adapter to fetch visits data from Mercado Libre.
    """

    def __init__(self, api_client: MercadoLibreApiClient):
        self.api_client = api_client

    def get_visits(self, item_id: str, window_days: int) -> VisitSignal:
        if window_days <= 0:
            raise ValueError("window_days must be strictly positive")

        path = f"/items/{item_id}/visits/time_window?last={window_days}&unit=day"
        data = self.api_client.get(path)
        now = datetime.now(timezone.utc)
        current_date_str = now.strftime("%Y-%m-%d")

        # Parse total visits
        total_visits_raw = data.get("total_visits")
        if total_visits_raw is None:
            total_visits = None
        else:
            total_visits = int(total_visits_raw)

        results = data.get("results", [])
        observed_days = len(results)

        valid_days_count = 0
        valid_visits_sum = 0

        for r in results:
            date_str = str(r.get("date", ""))[:10]
            visits = int(r.get("visits", 0))

            # Discard incomplete current day for average calculation
            if date_str == current_date_str:
                continue

            valid_days_count += 1
            valid_visits_sum += visits

        coverage_ratio = float(observed_days) / float(window_days)
        # Ensure coverage is capped at 1.0 just in case API returns more days
        coverage_ratio = min(1.0, max(0.0, coverage_ratio))

        average_daily_visits = None
        if valid_days_count > 0:
            average_daily_visits = float(valid_visits_sum) / float(valid_days_count)
        elif total_visits == 0 and observed_days > 0:
            average_daily_visits = 0.0

        return VisitSignal(
            item_id=item_id,
            window=f"{window_days}d",
            total_visits=total_visits,
            observed_days=observed_days,
            coverage_ratio=coverage_ratio,
            source="mercadolibre_visits",
            observed_at=now,
            confidence=Confidence.UNKNOWN,  # As requested, fallback to default/architecturally correct
            average_daily_visits=average_daily_visits,
            momentum=None,
            acceleration=None,
        )
