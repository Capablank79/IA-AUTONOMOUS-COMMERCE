"""
Puertos de dominio para Confiabilidad y Resiliencia (Reliability - Hito K.7).

Define:
- ClockPort: Abstracción de tiempo para sleeps virtuales y control temporal determinista sin sleep real.
- CircuitBreakerPort: Puerto para circuit breakers de protección contra fallos en cascada.
- IdempotencyStorePort: Almacén de claves de idempotencia y payloads asociados para evitar ejecuciones duplicadas y detectar conflictos.
- ReliabilityEnginePort: Motor orquestador de ejecución confiable (retry, backoff, circuit breaker, reconciliation).
- ReliabilityAuditPort: Emisión no acoplada hacia Audit Trail K.1 y Agent Trace K.2.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable, Tuple

from .models import (
    FailureCategory,
    FailureRecoverability,
    CircuitState,
    CircuitBreakerConfig,
    RetryPolicy,
    RecoveryDecision,
    ReliabilityResult,
    SystemHealthState,
)


class ClockPort(ABC):
    """Puerto de abstracción temporal para pruebas deterministas sin time.sleep."""

    @abstractmethod
    def now(self) -> datetime:
        """Retorna la fecha/hora UTC actual."""
        pass

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        """Avanza el tiempo o duerme según la implementación."""
        pass


class CircuitBreakerPort(ABC):
    """Puerto de Circuit Breaker para aislar dependencias degradadas."""

    @abstractmethod
    def get_state(self, service_name: str) -> CircuitState:
        """Retorna el estado actual del circuito para el servicio."""
        pass

    @abstractmethod
    def allow_request(self, service_name: str) -> bool:
        """Indica si se permite enviar una solicitud al servicio."""
        pass

    @abstractmethod
    def record_success(self, service_name: str) -> None:
        """Registra un resultado exitoso."""
        pass

    @abstractmethod
    def record_failure(
        self,
        service_name: str,
        category: FailureCategory,
        error_message: Optional[str] = None,
    ) -> None:
        """Registra un fallo ocurrido en el servicio."""
        pass

    @abstractmethod
    def reset(self, service_name: str) -> None:
        """Reinicia el estado del circuito para el servicio a CLOSED."""
        pass


class IdempotencyStorePort(ABC):
    """Puerto de almacenamiento persistente / duradero para control estricto de idempotencia."""

    @abstractmethod
    def get(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Obtiene el registro almacenado para una clave de idempotencia."""
        pass

    @abstractmethod
    def save(
        self,
        idempotency_key: str,
        payload_hash: str,
        result: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        """Guarda o actualiza el registro de idempotencia de forma atómica."""
        pass

    @abstractmethod
    def compute_payload_hash(self, payload: Any) -> str:
        """Calcula el hash determinista SHA-256 de un payload."""
        pass


class ReliabilityEnginePort(ABC):
    """Puerto del motor de confiabilidad y ejecución resiliente."""

    @abstractmethod
    def execute_with_reliability(
        self,
        operation_id: str,
        operation_func: Callable[[], Any],
        is_side_effect: bool,
        retry_policy: Optional[RetryPolicy] = None,
        service_name: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        payload: Optional[Any] = None,
        reconcile_func: Optional[Callable[[], Optional[Any]]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReliabilityResult:
        """Ejecuta una operación garantizando taxonomía de fallos, reintentos seguros, reconciliación y control de idempotencia."""
        pass
