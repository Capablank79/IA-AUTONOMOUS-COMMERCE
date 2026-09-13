"""
Módulo de Dominio para Capacity Planning (Hito P.10 — Production / Operations).
"""

from src.domain.capacity_planning.models import (
    CapacityConfidence,
    CapacityEvaluation,
    CapacityForecast,
    CapacityPlanningError,
    CapacityPlanningIntegrityError,
    CapacityPlanningConfigurationError,
    CapacityRecommendation,
    CapacityRecommendationType,
    CapacityResourceLimits,
    CapacityRisk,
    CapacityScope,
    CapacitySnapshot,
    CapacityTrend,
    ForecastHorizon,
    ResourceDimension,
    compute_capacity_checksum,
)
from src.domain.capacity_planning.ports import (
    CapacityConfigurationPort,
    CapacityDataProviderPort,
    CapacitySnapshotRepositoryPort,
)

__all__ = [
    "CapacityConfidence",
    "CapacityEvaluation",
    "CapacityForecast",
    "CapacityPlanningError",
    "CapacityPlanningIntegrityError",
    "CapacityPlanningConfigurationError",
    "CapacityRecommendation",
    "CapacityRecommendationType",
    "CapacityResourceLimits",
    "CapacityRisk",
    "CapacityScope",
    "CapacitySnapshot",
    "CapacityTrend",
    "ForecastHorizon",
    "ResourceDimension",
    "compute_capacity_checksum",
    "CapacityConfigurationPort",
    "CapacityDataProviderPort",
    "CapacitySnapshotRepositoryPort",
]
