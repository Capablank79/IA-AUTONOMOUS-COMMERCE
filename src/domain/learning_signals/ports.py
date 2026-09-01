from abc import ABC, abstractmethod
from typing import Optional, List
from src.domain.learning_signals.models import LearningSignalRecord, LearningSignalType, LearningSignalSubjectType


class LearningSignalRepositoryPort(ABC):
    """
    Puerto primario de repositorio para guardar y consultar LearningSignalRecord.
    """

    @abstractmethod
    def save_signal(self, signal: LearningSignalRecord) -> None:
        """Persiste una señal de aprendizaje."""
        pass

    @abstractmethod
    def get_signal_by_id(self, signal_id: str) -> Optional[LearningSignalRecord]:
        """Obtiene una señal por su ID único."""
        pass

    @abstractmethod
    def get_signals_by_subject(self, subject_type: LearningSignalSubjectType, subject_id: str) -> List[LearningSignalRecord]:
        """Obtiene señales filtradas por sujeto."""
        pass

    @abstractmethod
    def get_signals_by_type(self, signal_type: LearningSignalType) -> List[LearningSignalRecord]:
        """Obtiene señales filtradas por tipo de señal."""
        pass

    @abstractmethod
    def get_signal_by_idempotency_key(self, idempotency_key: str) -> Optional[LearningSignalRecord]:
        """Obtiene una señal por su clave de idempotencia."""
        pass

    @abstractmethod
    def list_all(self) -> List[LearningSignalRecord]:
        """Lista todas las señales de aprendizaje conservadas."""
        pass
