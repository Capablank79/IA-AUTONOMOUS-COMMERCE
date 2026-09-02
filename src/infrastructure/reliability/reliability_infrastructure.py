"""
Implementación de componentes de infraestructura y aplicación para Reliability (K.7).

Incluye:
- SystemClock / VirtualClock: Reloj real y reloj virtual con avance determinista de tiempo para tests sin sleep.
- InMemoryCircuitBreaker: Implementación thread-safe de Circuit Breaker.
- JsonIdempotencyStore: Almacén atómico con persistencia JSON y fsync para idempotencia crash-safe.
- InMemoryIdempotencyStore: Almacén en memoria thread-safe para operaciones rápidas.
"""

import os
import json
import time
import hashlib
import logging
import threading
from datetime import datetime, timezone, timedelta
from types import MappingProxyType
from typing import Optional, Dict, Any, Tuple

from src.domain.reliability.models import (
    CircuitState,
    CircuitBreakerConfig,
    FailureCategory,
)
from src.domain.reliability.ports import (
    ClockPort,
    CircuitBreakerPort,
    IdempotencyStorePort,
)

logger = logging.getLogger(__name__)


class SystemClock(ClockPort):
    """Implementación de reloj con tiempo real del sistema."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock(ClockPort):
    """
    Reloj virtual determinista para unit tests.
    Permite simular esperas, backoffs y expiración de ventanas sin pausar la CPU real.
    """

    def __init__(self, initial_time: Optional[datetime] = None):
        self._current_time = initial_time or datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        self.sleep_calls: list[float] = []
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._current_time

    def sleep(self, seconds: float) -> None:
        with self._lock:
            if seconds > 0:
                self.sleep_calls.append(seconds)
                self._current_time += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> datetime:
        with self._lock:
            self._current_time += timedelta(seconds=seconds)
            return self._current_time


class InMemoryCircuitBreaker(CircuitBreakerPort):
    """
    Circuit Breaker thread-safe in-memory con soporte de VirtualClock.
    Transiciones canónicas:
    - CLOSED -> OPEN: cuando fallos consecutivos >= failure_threshold dentro de la ventana de monitoreo.
    - OPEN -> HALF_OPEN: cuando transcurre recovery_timeout_seconds desde el último fallo.
    - HALF_OPEN -> CLOSED: cuando se acumulan half_open_success_threshold éxitos consecutivos.
    - HALF_OPEN -> OPEN: ante cualquier fallo.
    """

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.config = config or CircuitBreakerConfig()
        self.clock = clock or SystemClock()
        self._lock = threading.Lock()
        # Estado por servicio: service_name -> dict(state, failure_count, success_count, last_failure_time)
        self._service_states: Dict[str, Dict[str, Any]] = {}

    def _get_entry(self, service_name: str) -> Dict[str, Any]:
        if service_name not in self._service_states:
            self._service_states[service_name] = {
                "state": CircuitState.CLOSED,
                "failure_count": 0,
                "success_count": 0,
                "last_state_change": self.clock.now(),
                "last_failure_time": None,
            }
        return self._service_states[service_name]

    def get_state(self, service_name: str) -> CircuitState:
        with self._lock:
            entry = self._get_entry(service_name)
            current_state = entry["state"]

            if current_state == CircuitState.OPEN:
                last_failure = entry["last_failure_time"]
                if last_failure:
                    elapsed = (self.clock.now() - last_failure).total_seconds()
                    if elapsed >= self.config.recovery_timeout_seconds:
                        entry["state"] = CircuitState.HALF_OPEN
                        entry["success_count"] = 0
                        entry["last_state_change"] = self.clock.now()
                        return CircuitState.HALF_OPEN

            return entry["state"]

    def allow_request(self, service_name: str) -> bool:
        state = self.get_state(service_name)
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return True
        return False

    def record_success(self, service_name: str) -> None:
        with self._lock:
            entry = self._get_entry(service_name)
            state = entry["state"]

            if state == CircuitState.HALF_OPEN:
                entry["success_count"] += 1
                if entry["success_count"] >= self.config.half_open_success_threshold:
                    entry["state"] = CircuitState.CLOSED
                    entry["failure_count"] = 0
                    entry["success_count"] = 0
                    entry["last_state_change"] = self.clock.now()
            elif state == CircuitState.CLOSED:
                entry["failure_count"] = 0

    def record_failure(
        self,
        service_name: str,
        category: FailureCategory,
        error_message: Optional[str] = None,
    ) -> None:
        if category not in self.config.monitored_categories:
            return

        with self._lock:
            entry = self._get_entry(service_name)
            entry["last_failure_time"] = self.clock.now()
            state = entry["state"]

            if state == CircuitState.HALF_OPEN:
                entry["state"] = CircuitState.OPEN
                entry["failure_count"] = self.config.failure_threshold
                entry["success_count"] = 0
                entry["last_state_change"] = self.clock.now()
            elif state == CircuitState.CLOSED:
                entry["failure_count"] += 1
                if entry["failure_count"] >= self.config.failure_threshold:
                    entry["state"] = CircuitState.OPEN
                    entry["success_count"] = 0
                    entry["last_state_change"] = self.clock.now()

    def reset(self, service_name: str) -> None:
        with self._lock:
            entry = self._get_entry(service_name)
            entry["state"] = CircuitState.CLOSED
            entry["failure_count"] = 0
            entry["success_count"] = 0
            entry["last_failure_time"] = None
            entry["last_state_change"] = self.clock.now()


class InMemoryIdempotencyStore(IdempotencyStorePort):
    """Almacén de claves de idempotencia en memoria thread-safe."""

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def compute_payload_hash(self, payload: Any) -> str:
        if payload is None:
            return "empty"
        if isinstance(payload, str):
            serialized = payload
        else:
            try:
                serialized = json.dumps(payload, sort_keys=True, default=str)
            except Exception:
                serialized = str(payload)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._store.get(idempotency_key)
            return dict(item) if item else None

    def save(
        self,
        idempotency_key: str,
        payload_hash: str,
        result: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        with self._lock:
            self._store[idempotency_key] = {
                "idempotency_key": idempotency_key,
                "payload_hash": payload_hash,
                "result": result,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }


class JsonIdempotencyStore(IdempotencyStorePort):
    """
    Almacén persistente de idempotencia con almacenamiento JSON atómico (.tmp + fsync + os.replace).
    Crash-safe y resistente a reinicios.
    """

    def __init__(self, storage_dir: str):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _get_file_path(self, idempotency_key: str) -> str:
        safe_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return os.path.join(self.storage_dir, f"idemp_{safe_key}.json")

    def compute_payload_hash(self, payload: Any) -> str:
        if payload is None:
            return "empty"
        if isinstance(payload, str):
            serialized = payload
        else:
            try:
                serialized = json.dumps(payload, sort_keys=True, default=str)
            except Exception:
                serialized = str(payload)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            file_path = self._get_file_path(idempotency_key)
            if not os.path.exists(file_path):
                return None
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read idempotency record {idempotency_key}: {e}")
                return None

    def save(
        self,
        idempotency_key: str,
        payload_hash: str,
        result: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        with self._lock:
            file_path = self._get_file_path(idempotency_key)
            tmp_path = f"{file_path}.tmp_{os.getpid()}_{time.time_ns()}"
            record = {
                "idempotency_key": idempotency_key,
                "payload_hash": payload_hash,
                "result": result,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(record, f, indent=2, default=str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, file_path)
            except Exception as e:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise IOError(f"Could not write idempotency record to {file_path}: {e}") from e
