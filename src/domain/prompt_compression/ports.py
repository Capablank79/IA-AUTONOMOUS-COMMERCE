"""
Puertos de dominio para Compresión Determinista de Prompt y Contexto (Prompt Compression - Hito M.3).

Transversal M — Control de Coste e Inferencia.
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.domain.prompt_compression.models import (
    CompressionRequest,
    CompressionResult,
    CompressionPolicy,
)


class PromptCompressionPort(ABC):
    """
    Puerto primario del servicio de compresión determinista de prompt / contexto.
    """

    @abstractmethod
    def compress_context(
        self,
        request: CompressionRequest,
        policy: Optional[CompressionPolicy] = None,
    ) -> CompressionResult:
        """
        Ejecuta la compresión determinista de contexto si excede el presupuesto,
        respetando prioridades y auditabilidad.
        """
        pass
