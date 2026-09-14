"""
Definición de Puertos e Interfaces para Q.6 — Business KPIs (Hito Q — Business Intelligence).
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from datetime import datetime

from src.domain.tenant.models import TenantContext
from .models import (
    BusinessKPISummary,
    BusinessKPIValue,
    BusinessKPIQuery,
    BusinessKPIComparison,
    BusinessKPICatalogItem,
)


class BusinessKPIServicePort(ABC):
    """
    Puerto de servicio para la orquestación y cálculo seguro de KPIs de negocio.
    """

    @abstractmethod
    def get_summary(
        self,
        tenant_id: str,
        query: Optional[BusinessKPIQuery] = None,
        session_id: Optional[str] = None,
    ) -> BusinessKPISummary:
        """
        Calcula y proyecta el resumen ejecutivo cross-domain de KPIs para el tenant.
        """
        raise NotImplementedError

    @abstractmethod
    def get_kpi_by_id(
        self,
        tenant_id: str,
        kpi_id: str,
        query: Optional[BusinessKPIQuery] = None,
        session_id: Optional[str] = None,
    ) -> BusinessKPIValue:
        """
        Obtiene un KPI específico con su desglose y explicabilidad.
        """
        raise NotImplementedError

    @abstractmethod
    def get_comparison(
        self,
        tenant_id: str,
        current_query: BusinessKPIQuery,
        previous_query: BusinessKPIQuery,
        session_id: Optional[str] = None,
    ) -> List[BusinessKPIComparison]:
        """
        Compara los KPIs entre dos ventanas temporales deterministas.
        """
        raise NotImplementedError

    @abstractmethod
    def get_catalog(self) -> List[BusinessKPICatalogItem]:
        """
        Retorna el catálogo canónico de KPIs soportados.
        """
        raise NotImplementedError
