"""
Módulo de dominio para Prompt Compression (Hito M.3).

Transversal M — Control de Coste e Inferencia.
"""

from src.domain.prompt_compression.models import (
    CompressionStatus,
    ContextComponentType,
    PriorityLevel,
    ContextItem,
    CompressionActionType,
    CompressionAction,
    CompressionPolicy,
    RawContextPayload,
    CompressedContextPayload,
    CompressionRequest,
    CompressionResult,
)
from src.domain.prompt_compression.ports import PromptCompressionPort

__all__ = [
    "CompressionStatus",
    "ContextComponentType",
    "PriorityLevel",
    "ContextItem",
    "CompressionActionType",
    "CompressionAction",
    "CompressionPolicy",
    "RawContextPayload",
    "CompressedContextPayload",
    "CompressionRequest",
    "CompressionResult",
    "PromptCompressionPort",
]
