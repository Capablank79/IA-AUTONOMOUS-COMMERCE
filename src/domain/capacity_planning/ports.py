"""
Puertos de dominio para Capacity Planning (Hito P.10 — Production / Operations).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Mapping, Optional, Sequence, Tuple, Union

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.capacity_planning.models import (
    CapacityResourceLimits,
    CapacityScope,
    CapacitySnapshot,
    ResourceDimension,
)
from src.domain.monitoring.models import MetricSample, MetricSeries, MetricType


class CapacityConfigurationPort(ABC):
    """Puerto para consultar los límites de capacidad conocidos y políticas de headroom."""

    @abstractmethod
    def get_resource_limits(
        self,
        environment: ApplicationEnvironment,
        dimension: ResourceDimension,
    ) -> CapacityResourceLimits:
        """Obtiene la configuración de límites de capacidad para una dimensión y entorno."""
        pass

    @abstractmethod
    def get_all_resource_limits(
        self,
        environment: ApplicationEnvironment,
    ) -> Mapping[ResourceDimension, CapacityResourceLimits]:
        """Obtiene todas las especificaciones de capacidad configuradas para el entorno."""
        pass


class CapacityDataProviderPort(ABC):
    """
    Puerto para abstraer la extracción de métricas históricas de telemetría técnica (P.7, O.6, DB, Storage).
    """

    @abstractmethod
    def get_dimension_samples(
        self,
        environment: ApplicationEnvironment,
        dimension: ResourceDimension,
        start_time: datetime,
        end_time: datetime,
        scope: CapacityScope = CapacityScope.PLATFORM,
        tenant_id: Optional[str] = None,
    ) -> Tuple[MetricSample, ...]:
        """Obtiene muestras históricas ordenadas temporalmente para una dimensión de recurso."""
        pass


class CapacitySnapshotRepositoryPort(ABC):
    """Puerto para persistencia y consulta opcional de snapshots de capacidad."""

    @abstractmethod
    def save_snapshot(self, snapshot: CapacitySnapshot) -> None:
        """Guarda un snapshot de capacidad validado."""
        pass

    @abstractmethod
    def get_latest_snapshot(
        self,
        environment: ApplicationEnvironment,
        scope: CapacityScope = CapacityScope.PLATFORM,
        tenant_id: Optional[str] = None,
    ) -> Optional[CapacitySnapshot]:
        """Obtiene el último snapshot de capacidad evaluado."""
        pass
