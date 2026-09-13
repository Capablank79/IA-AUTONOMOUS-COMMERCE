"""
Puertos e Interfaces para el Ciclo de Vida y Retención de Logs (P.9).

Define contratos para:
- Stores de datos operacionales (archivos .log/.jsonl, PostgreSQL, Repositorios JSON).
- Repositorio/Registro de Políticas de Retención.
- Emisión de Auditoría de Operaciones de Retención.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.log_retention.models import (
    RetentionClass,
    RetentionDecision,
    RetentionPolicy,
    RetentionResult,
    RetentionStatusSummary,
)


class LogRetentionStorePort(ABC):
    """
    Puerto para interactuar con un almacén físico o lógico de logs/evidencias.
    Implementaciones: FileLogRetentionStore, MetricLogRetentionStore, AlertLogRetentionStore, TraceLogRetentionStore, PostgresLogRetentionStore.
    """

    @property
    @abstractmethod
    def supported_classes(self) -> Sequence[RetentionClass]:
        """Clases de logs soportadas por este store."""
        pass

    @abstractmethod
    def get_status_summary(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> RetentionStatusSummary:
        """Obtiene el resumen de estado y conteo de elementos elegibles para una política."""
        pass

    @abstractmethod
    def evaluate_retention(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> Sequence[RetentionDecision]:
        """Evalúa las reglas de retención y genera decisiones atómicas para cada elemento."""
        pass

    @abstractmethod
    def execute_purge(
        self,
        policy: RetentionPolicy,
        decisions: Sequence[RetentionDecision],
    ) -> RetentionResult:
        """Ejecuta de manera segura, atómica o por lotes la purga/rotación según las decisiones."""
        pass


class LogRetentionPolicyRegistryPort(ABC):
    """
    Puerto para la consulta y registro de políticas de retención centralizadas.
    """

    @abstractmethod
    def get_policy(
        self,
        environment: ApplicationEnvironment,
        data_class: RetentionClass,
        tenant_id: Optional[str] = None,
    ) -> RetentionPolicy:
        """Obtiene la política configurada o el default estricto para un entorno y clase."""
        pass

    @abstractmethod
    def list_policies(
        self,
        environment: Optional[ApplicationEnvironment] = None,
        tenant_id: Optional[str] = None,
    ) -> Sequence[RetentionPolicy]:
        """Lista todas las políticas de retención activas."""
        pass


class LogRetentionAuditEmitterPort(ABC):
    """
    Puerto para emitir eventos de auditoría inmutables (K.1) tras operaciones de purga/rotación.
    """

    @abstractmethod
    def emit_retention_event(
        self,
        event_type: str,
        environment: ApplicationEnvironment,
        data_class: RetentionClass,
        result: RetentionResult,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Registra el inicio, éxito o fallo de una operación de retención."""
        pass
