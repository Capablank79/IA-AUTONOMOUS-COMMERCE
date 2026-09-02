"""
Puertos de dominio para Quality Gates (Hito K.6).

Define:
- QualityGateRepositoryPort: Interfaz de repositorio para almacenar y recuperar de forma inmutable y determinista QualityGateDefinition y QualityGateDecision.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence

from src.domain.quality_gate.models import (
    QualityGateDefinition,
    QualityGateDecision,
    GateDecisionStatus,
)


class QualityGateRepositoryPort(ABC):
    """
    Puerto de persistencia para compuertas de calidad (Quality Gates).
    Garantiza inmutabilidad, atomicidad, durabilidad y recarga determinista.
    """

    @abstractmethod
    def save_definition(self, definition: QualityGateDefinition) -> QualityGateDefinition:
        """
        Persiste una definición de compuerta de calidad.
        Lanza excepción si la versión ya existe con diferente checksum (inmutabilidad).
        """
        pass

    @abstractmethod
    def get_definition(self, gate_id: str, version: Optional[str] = None) -> Optional[QualityGateDefinition]:
        """
        Recupera una definición de compuerta por gate_id y versión opcional (retorna última si no se especifica).
        """
        pass

    @abstractmethod
    def list_definitions(self, limit: int = 100) -> List[QualityGateDefinition]:
        """
        Lista las definiciones de compuertas disponibles.
        """
        pass

    @abstractmethod
    def list_definition_versions(self, gate_id: str) -> List[str]:
        """
        Lista todas las versiones conocidas de una compuerta por gate_id.
        """
        pass

    @abstractmethod
    def save_decision(self, decision: QualityGateDecision) -> QualityGateDecision:
        """
        Persiste una decisión de compuerta de calidad de manera inmutable e idempotente.
        """
        pass

    @abstractmethod
    def get_decision(self, decision_id: str) -> Optional[QualityGateDecision]:
        """
        Recupera una decisión por su identificador único.
        """
        pass

    @abstractmethod
    def get_decision_by_idempotency_key(self, idempotency_key: str) -> Optional[QualityGateDecision]:
        """
        Recupera una decisión por su clave de idempotencia.
        """
        pass

    @abstractmethod
    def list_decisions(
        self,
        gate_id: Optional[str] = None,
        gate_version: Optional[str] = None,
        evaluation_run_id: Optional[str] = None,
        status: Optional[GateDecisionStatus] = None,
        limit: int = 100,
    ) -> List[QualityGateDecision]:
        """
        Lista decisiones registradas con filtros opcionales.
        """
        pass
