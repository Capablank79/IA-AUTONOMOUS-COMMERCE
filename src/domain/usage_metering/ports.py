"""
Puertos de dominio para Usage Metering SaaS (Hito O.6 — Usage Metering).

Define:
- UsageEventRepositoryPort: Puerto de persistencia aislada por tenant para UsageEvents append-only.
- UsageMeteringServicePort: Puerto del servicio de medición y agregación multidimensional de consumo.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence, Dict

from src.domain.tenant.models import TenantContext
from .models import UsageEvent, UsageQuery, UsageAggregate


class UsageEventRepositoryPort(ABC):
    """
    Puerto de repositorio para eventos de uso acotados estrictamente por Tenant.

    Garantías:
    - Append-only persistente.
    - Detección de colisiones e idempotencia por usage_event_id / source_reference.
    - Aislamiento físico o lógico absoluto por tenant_id (CrossTenantGuard).
    """

    @abstractmethod
    def append_event(self, context: TenantContext, event: UsageEvent) -> UsageEvent:
        """
        Registra un nuevo evento de uso de forma atómica e inmutable.
        Si ya existe un evento idéntico (mismo usage_event_id o source_reference con mismo checksum):
        retorna el evento existente de forma idempotente.
        Si ya existe con datos contradictorios: lanza UsageEventConflictError.
        """
        pass

    @abstractmethod
    def get_event_by_id(self, context: TenantContext, usage_event_id: str) -> Optional[UsageEvent]:
        """Obtiene un evento de uso por su ID para el tenant verificado."""
        pass

    @abstractmethod
    def find_by_query(self, context: TenantContext, query: UsageQuery) -> List[UsageEvent]:
        """Lista todos los eventos de uso que coinciden con la consulta para el tenant verificado."""
        pass

    @abstractmethod
    def count_by_query(self, context: TenantContext, query: UsageQuery) -> int:
        """Retorna el conteo de eventos coincidentes para el tenant verificado."""
        pass


class UsageMeteringServicePort(ABC):
    """
    Puerto de servicio para la medición y agregación multidimensional de consumo de IA.
    """

    @abstractmethod
    def record_usage_event(self, context: TenantContext, event: UsageEvent) -> UsageEvent:
        """Registra un evento de consumo para el tenant del contexto."""
        pass

    @abstractmethod
    def get_event(self, context: TenantContext, usage_event_id: str) -> Optional[UsageEvent]:
        """Obtiene un evento de uso de forma segura y aislada."""
        pass

    @abstractmethod
    def aggregate_usage(self, context: TenantContext, query: UsageQuery) -> UsageAggregate:
        """
        Calcula y retorna la agregación multidimensional determinista de consumo para el tenant.
        """
        pass
