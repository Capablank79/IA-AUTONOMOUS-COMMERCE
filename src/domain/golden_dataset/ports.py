"""
Puertos de dominio para Golden Datasets (Hito K.5).

Define:
- GoldenDatasetRepositoryPort: Interfaz para almacenamiento, consulta, versionado y verificación de integridad de GoldenDataset y GoldenDatasetManifest.
- GoldenDatasetValidatorPort: Interfaz para validación estructural y determinista de datasets y sus membresías de casos.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence, Dict, Any

from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetManifest,
    GoldenDatasetStatus,
)
from src.domain.evaluation.models import EvaluationCase


class GoldenDatasetValidatorPort(ABC):
    """
    Puerto para validación determinista de Golden Datasets.
    """

    @abstractmethod
    def validate(
        self,
        dataset: GoldenDataset,
        resolved_cases: Optional[Sequence[EvaluationCase]] = None,
    ) -> Dict[str, Any]:
        """
        Valida que el dataset, manifest, casos, version y checksum sean consistentes.
        Retorna dict con {"is_valid": bool, "errors": List[str], "warnings": List[str]}.
        """
        pass


class GoldenDatasetRepositoryPort(ABC):
    """
    Puerto para la persistencia, consulta, inmutabilidad y resolución de Golden Datasets.
    """

    @abstractmethod
    def save_dataset(self, dataset: GoldenDataset) -> GoldenDataset:
        """
        Guarda un GoldenDataset de forma atómica e inmutable.
        Lanza excepción si se intenta sobrescribir una versión existente con checksum diferente (conflicto).
        Retorna la instancia guardada (idempotente si el contenido es idéntico).
        """
        pass

    @abstractmethod
    def get_dataset(self, dataset_id: str, version: Optional[str] = None) -> Optional[GoldenDataset]:
        """
        Obtiene un dataset por su ID y versión específica.
        Si version es None, retorna la última versión registrada (o la versión VALIDATED más reciente).
        """
        pass

    @abstractmethod
    def list_datasets(
        self,
        domain_scope: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[GoldenDatasetStatus] = None,
        limit: int = 100,
    ) -> List[GoldenDataset]:
        """
        Lista datasets aplicando filtros opcionales.
        """
        pass

    @abstractmethod
    def list_versions(self, dataset_id: str) -> List[str]:
        """
        Lista todas las versiones disponibles para un dataset_id dado.
        """
        pass
