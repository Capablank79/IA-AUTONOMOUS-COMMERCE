"""
Adaptadores de persistencia para métricas de monitoreo de producción (P.7).

Define:
- InMemoryMetricRepository: Repositorio en memoria thread-safe aislado por ApplicationEnvironment.
- JsonMetricRepository: Repositorio JSON estructurado bajo `base_data_dir / "monitoring" / {environment} / "metrics.json"`.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringMetric,
    MonitoringScope,
    MonitoringError,
    MonitoringIntegrityError,
    validate_metric_labels,
)
from src.domain.monitoring.ports import MetricRepositoryPort

logger = logging.getLogger(__name__)


class InMemoryMetricRepository(MetricRepositoryPort):
    """
    Repositorio de métricas en memoria thread-safe, con aislamiento estricto por ApplicationEnvironment.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Mapeo: Dict[ApplicationEnvironment, List[MetricSample]]
        self._samples_by_env: Dict[ApplicationEnvironment, List[MetricSample]] = {
            ApplicationEnvironment.DEVELOPMENT: [],
            ApplicationEnvironment.STAGING: [],
            ApplicationEnvironment.PRODUCTION: [],
        }

    def record_sample(self, sample: MetricSample) -> None:
        with self._lock:
            env = normalize_environment_name(sample.environment)
            if env not in self._samples_by_env:
                self._samples_by_env[env] = []
            self._samples_by_env[env].append(sample)

    def record_samples(self, samples: Sequence[MetricSample]) -> None:
        with self._lock:
            for s in samples:
                self.record_sample(s)

    def get_samples(
        self,
        environment: ApplicationEnvironment,
        metric_type: Optional[MetricType] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        scope: Optional[MonitoringScope] = None,
        tenant_id: Optional[str] = None,
    ) -> Tuple[MetricSample, ...]:
        env = normalize_environment_name(environment)
        if start_time and start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        if end_time and end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        with self._lock:
            raw_list = self._samples_by_env.get(env, [])
            res: List[MetricSample] = []
            for s in raw_list:
                if metric_type is not None and s.metric_type != metric_type:
                    continue
                if scope is not None and s.scope != scope:
                    continue
                if tenant_id is not None and s.tenant_id != tenant_id:
                    continue
                if start_time is not None and s.timestamp < start_time:
                    continue
                if end_time is not None and s.timestamp > end_time:
                    continue
                res.append(s)
            return tuple(res)

    def get_latest_sample_timestamp(
        self,
        environment: ApplicationEnvironment,
    ) -> Optional[datetime]:
        env = normalize_environment_name(environment)
        with self._lock:
            samples = self._samples_by_env.get(env, [])
            if not samples:
                return None
            return max(s.timestamp for s in samples)

    def clear(self, environment: Optional[ApplicationEnvironment] = None) -> None:
        with self._lock:
            if environment is None:
                for k in self._samples_by_env.keys():
                    self._samples_by_env[k].clear()
            else:
                env = normalize_environment_name(environment)
                if env in self._samples_by_env:
                    self._samples_by_env[env].clear()


class JsonMetricRepository(MetricRepositoryPort):
    """
    Repositorio JSON en disco para muestras de monitoreo operacional por entorno.
    """

    def __init__(self, base_dir: Union[str, Path]) -> None:
        self._base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_env_file(self, environment: ApplicationEnvironment) -> Path:
        env = normalize_environment_name(environment)
        env_dir = self._base_dir / "monitoring" / env.value
        env_dir.mkdir(parents=True, exist_ok=True)
        return env_dir / "metrics_samples.json"

    def _read_samples(self, environment: ApplicationEnvironment) -> List[MetricSample]:
        file_path = self._get_env_file(environment)
        if not file_path.exists():
            return []
        try:
            raw_text = file_path.read_text(encoding="utf-8")
            if not raw_text.strip():
                return []
            data = json.loads(raw_text)
            samples: List[MetricSample] = []
            for item in data:
                ts = datetime.fromisoformat(item["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                samples.append(
                    MetricSample(
                        metric_type=MetricType(item["metric_type"]),
                        value=item["value"],
                        timestamp=ts,
                        environment=ApplicationEnvironment(item["environment"]),
                        scope=MonitoringScope(item.get("scope", "PLATFORM")),
                        tenant_id=item.get("tenant_id"),
                        unit=MetricUnit(item.get("unit", "COUNT")),
                        labels=item.get("labels", {}),
                    )
                )
            return samples
        except Exception as exc:
            logger.error(f"Error reading metric samples from {file_path}: {exc}")
            return []

    def _write_samples(self, environment: ApplicationEnvironment, samples: Sequence[MetricSample]) -> None:
        file_path = self._get_env_file(environment)
        data = [s.to_dict() for s in samples]
        temp_file = file_path.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_file.replace(file_path)

    def record_sample(self, sample: MetricSample) -> None:
        with self._lock:
            env = normalize_environment_name(sample.environment)
            samples = self._read_samples(env)
            samples.append(sample)
            self._write_samples(env, samples)

    def record_samples(self, samples: Sequence[MetricSample]) -> None:
        if not samples:
            return
        with self._lock:
            # Agrupar por entorno
            by_env: Dict[ApplicationEnvironment, List[MetricSample]] = {}
            for s in samples:
                env = normalize_environment_name(s.environment)
                if env not in by_env:
                    by_env[env] = []
                by_env[env].append(s)
            for env, env_samples in by_env.items():
                existing = self._read_samples(env)
                existing.extend(env_samples)
                self._write_samples(env, existing)

    def get_samples(
        self,
        environment: ApplicationEnvironment,
        metric_type: Optional[MetricType] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        scope: Optional[MonitoringScope] = None,
        tenant_id: Optional[str] = None,
    ) -> Tuple[MetricSample, ...]:
        env = normalize_environment_name(environment)
        if start_time and start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        if end_time and end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        with self._lock:
            samples = self._read_samples(env)
            res: List[MetricSample] = []
            for s in samples:
                if metric_type is not None and s.metric_type != metric_type:
                    continue
                if scope is not None and s.scope != scope:
                    continue
                if tenant_id is not None and s.tenant_id != tenant_id:
                    continue
                if start_time is not None and s.timestamp < start_time:
                    continue
                if end_time is not None and s.timestamp > end_time:
                    continue
                res.append(s)
            return tuple(res)

    def get_latest_sample_timestamp(
        self,
        environment: ApplicationEnvironment,
    ) -> Optional[datetime]:
        env = normalize_environment_name(environment)
        with self._lock:
            samples = self._read_samples(env)
            if not samples:
                return None
            return max(s.timestamp for s in samples)

    def clear(self, environment: Optional[ApplicationEnvironment] = None) -> None:
        with self._lock:
            if environment is None:
                for env in (ApplicationEnvironment.DEVELOPMENT, ApplicationEnvironment.STAGING, ApplicationEnvironment.PRODUCTION):
                    f = self._get_env_file(env)
                    if f.exists():
                        f.unlink()
            else:
                f = self._get_env_file(environment)
                if f.exists():
                    f.unlink()
