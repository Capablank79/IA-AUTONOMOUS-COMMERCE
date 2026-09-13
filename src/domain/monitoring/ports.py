"""
Puertos de repositorio y servicio para Monitoreo de Producción (Hito P.7).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Mapping, Optional, Sequence, Tuple, Union

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.monitoring.models import (
    MetricSample,
    MetricSeries,
    MetricType,
    MetricWindow,
    MonitoringMetric,
    MonitoringScope,
    ProductionMonitoringSnapshot,
)


class MetricRepositoryPort(ABC):
    """Puerto de persistencia/almacenamiento de muestras y series temporales de telemetría."""

    @abstractmethod
    def record_sample(self, sample: MetricSample) -> None:
        """Registra una muestra individual de telemetría técnica."""
        pass

    @abstractmethod
    def record_samples(self, samples: Sequence[MetricSample]) -> None:
        """Registra un lote de muestras de telemetría técnica."""
        pass

    @abstractmethod
    def get_samples(
        self,
        environment: ApplicationEnvironment,
        metric_type: Optional[MetricType] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        scope: Optional[MonitoringScope] = None,
        tenant_id: Optional[str] = None,
    ) -> Tuple[MetricSample, ...]:
        """Obtiene muestras filtradas por entorno y ventana temporal."""
        pass

    @abstractmethod
    def get_latest_sample_timestamp(
        self,
        environment: ApplicationEnvironment,
    ) -> Optional[datetime]:
        """Obtiene el timestamp de la muestra más reciente registrada para el entorno."""
        pass

    @abstractmethod
    def clear(self, environment: Optional[ApplicationEnvironment] = None) -> None:
        """Limpia las muestras registradas (útil para pruebas o reinicialización controlada)."""
        pass
