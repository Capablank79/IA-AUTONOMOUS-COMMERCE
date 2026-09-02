"""
Catálogo de Precios y Tarifas en Memoria / Configuración (Hito K.3).

Implementa PricingCatalogPort de forma determinista y configurable.
Permite registrar tarifas versionadas con fechas de vigencia (effective_from, effective_to)
y escalas de tasa (por token, por 1K tokens, por 1M tokens, por request).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from src.domain.cost.models import PricingRate, CostType
from src.domain.cost.ports import PricingCatalogPort


class InMemoryPricingCatalog(PricingCatalogPort):
    """
    Implementación en memoria de PricingCatalogPort.
    Permite almacenar y buscar tarifas deterministas por provider y service_or_model.
    """

    def __init__(self, initial_rates: Optional[List[PricingRate]] = None):
        self._rates: List[PricingRate] = list(initial_rates or [])

    def register_rate(self, rate: PricingRate) -> None:
        """Registra o añade una nueva tarifa al catálogo."""
        self._rates.append(rate)

    def get_rate(
        self,
        provider: str,
        service_or_model: str,
        at_time: Optional[datetime] = None,
        cost_type: Optional[CostType] = None,
    ) -> Optional[PricingRate]:
        """
        Busca la tarifa aplicable más reciente y vigente para el proveedor y modelo solicitados.
        """
        target_time = at_time or datetime.now(timezone.utc)
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=timezone.utc)

        p_norm = provider.strip().lower()
        s_norm = service_or_model.strip().lower()

        matching: List[PricingRate] = []
        for r in self._rates:
            if r.provider.strip().lower() == p_norm and r.service_or_model.strip().lower() == s_norm:
                # Comprobar vigencia temporal
                if r.effective_from is not None:
                    ef_from = r.effective_from if r.effective_from.tzinfo else r.effective_from.replace(tzinfo=timezone.utc)
                    if target_time < ef_from:
                        continue
                if r.effective_to is not None:
                    ef_to = r.effective_to if r.effective_to.tzinfo else r.effective_to.replace(tzinfo=timezone.utc)
                    if target_time > ef_to:
                        continue
                matching.append(r)

        if not matching:
            return None

        # Si hay varias, tomar la última registrada o la más específica
        return matching[-1]


def get_default_pricing_catalog() -> InMemoryPricingCatalog:
    """
    Crea el catálogo inicial de tarifas conocidas del sistema.
    Tarifas estándar referenciales deterministas (escala por 1M tokens o por llamada).
    """
    catalog = InMemoryPricingCatalog()

    # Modelos LLM estándar
    catalog.register_rate(
        PricingRate(
            provider="omniroute",
            service_or_model="gpt-4o-mini",
            currency="USD",
            input_rate=Decimal("0.150"),     # $0.15 por 1M input tokens
            output_rate=Decimal("0.600"),    # $0.60 por 1M output tokens
            rate_scale=Decimal("1000000"),
            version="2024-07",
        )
    )
    catalog.register_rate(
        PricingRate(
            provider="omniroute",
            service_or_model="auto/best-coding",
            currency="USD",
            input_rate=Decimal("0.150"),
            output_rate=Decimal("0.600"),
            rate_scale=Decimal("1000000"),
            version="2024-07",
        )
    )
    catalog.register_rate(
        PricingRate(
            provider="openai",
            service_or_model="gpt-4o",
            currency="USD",
            input_rate=Decimal("2.500"),
            output_rate=Decimal("10.000"),
            rate_scale=Decimal("1000000"),
            version="2024-08",
        )
    )

    # Herramientas con coste 0 confirmado (ZERO_COST)
    catalog.register_rate(
        PricingRate(
            provider="mercadolibre",
            service_or_model="market_search",
            currency="USD",
            flat_rate=Decimal("0.00"),
            rate_scale=Decimal("1"),
            version="1.0.0",
        )
    )
    catalog.register_rate(
        PricingRate(
            provider="mercadolibre",
            service_or_model="trend_search",
            currency="USD",
            flat_rate=Decimal("0.00"),
            rate_scale=Decimal("1"),
            version="1.0.0",
        )
    )
    catalog.register_rate(
        PricingRate(
            provider="internal",
            service_or_model="supplier_search",
            currency="USD",
            flat_rate=Decimal("0.00"),
            rate_scale=Decimal("1"),
            version="1.0.0",
        )
    )

    # API de pago externa referencial (ejemplo $0.01 por request)
    catalog.register_rate(
        PricingRate(
            provider="premium_enricher",
            service_or_model="serp_enrichment",
            currency="USD",
            flat_rate=Decimal("0.010"),
            rate_scale=Decimal("1"),
            version="1.0.0",
        )
    )

    return catalog
