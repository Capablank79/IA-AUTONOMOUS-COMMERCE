"""
Algoritmo y motor de fusión determinista de resultados (Hito R.4 — Agent Coordination).

Define:
- DeterministicResultMerger: Lógica pura de dominio para consolidar resultados de múltiples agentes
  hacia tareas downstream (fan-in), detectando inconsistencias, colisiones de mutación y contradicciones.
"""

from typing import Mapping, Sequence, Tuple, Dict, Any, Optional, List
from decimal import Decimal
import json

from src.domain.agent_coordination.models import (
    MergeStrategy,
    MergeResult,
    CoordinationFailureType,
)
from src.domain.security.models import sanitize_security_data, deep_freeze


class DeterministicResultMerger:
    """
    Motor determinista de fusión de resultados multi-agente.
    """

    @classmethod
    def merge(
        cls,
        results_by_source: Mapping[str, Mapping[str, Any]],
        strategy: MergeStrategy = MergeStrategy.KEYED_MERGE,
        precedence_order: Sequence[str] = (),
        expected_output_keys: Sequence[str] = (),
    ) -> MergeResult:
        """
        Fusiona un diccionario de resultados indexado por source_id (agent_id o task_id).

        :param results_by_source: Mapeo {source_id: {key: value}}
        :param strategy: MergeStrategy
        :param precedence_order: Secuencia de source_ids ordenados de mayor a menor prioridad
        :param expected_output_keys: Claves esperadas en el resultado
        :return: MergeResult con success=True/False, merged_data, y lista de conflictos
        """
        if not results_by_source:
            return MergeResult(
                success=True,
                merged_data={},
                strategy_used=strategy,
                conflicts=(),
            )

        sanitized_sources = {
            src: sanitize_security_data(data)
            for src, data in sorted(results_by_source.items(), key=lambda x: str(x[0]))
        }

        if strategy == MergeStrategy.FAIL_ON_CONFLICT:
            return cls._merge_fail_on_conflict(sanitized_sources)
        elif strategy == MergeStrategy.STRICT_IDENTICAL:
            return cls._merge_strict_identical(sanitized_sources)
        elif strategy == MergeStrategy.EXPLICIT_PRECEDENCE:
            return cls._merge_explicit_precedence(sanitized_sources, precedence_order)
        elif strategy == MergeStrategy.UNION or strategy == MergeStrategy.KEYED_MERGE:
            return cls._merge_keyed(sanitized_sources)
        else:
            return MergeResult(
                success=False,
                strategy_used=strategy,
                conflicts=(f"UNKNOWN_STRATEGY_{strategy}",),
            )

    @classmethod
    def _merge_keyed(cls, sources: Mapping[str, Mapping[str, Any]]) -> MergeResult:
        merged: Dict[str, Any] = {}
        conflicts: List[str] = []
        sources_by_key: Dict[str, List[Tuple[str, Any]]] = {}

        for src, data in sorted(sources.items(), key=lambda x: str(x[0])):
            if not isinstance(data, (dict, Mapping)):
                conflicts.append(f"Source {src} output is not a dictionary")
                continue
            for k, v in sorted(data.items(), key=lambda x: str(x[0])):
                sources_by_key.setdefault(k, []).append((src, v))

        for k, source_val_pairs in sorted(sources_by_key.items(), key=lambda x: str(x[0])):
            first_src, first_val = source_val_pairs[0]
            # Check if multiple sources provide different values
            collision = False
            for other_src, other_val in source_val_pairs[1:]:
                if not cls._values_equal(first_val, other_val):
                    collision = True
                    conflicts.append(
                        f"Conflict on key '{k}': source '{first_src}' produced {first_val!r} "
                        f"vs source '{other_src}' produced {other_val!r}"
                    )
                    break
            if not collision:
                merged[k] = first_val

        success = len(conflicts) == 0
        return MergeResult(
            success=success,
            merged_data=merged if success else {},
            strategy_used=MergeStrategy.KEYED_MERGE,
            conflicts=tuple(conflicts),
        )

    @classmethod
    def _merge_strict_identical(cls, sources: Mapping[str, Mapping[str, Any]]) -> MergeResult:
        # All sources must provide dictionaries that are identical across all shared keys
        return cls._merge_keyed(sources)

    @classmethod
    def _merge_fail_on_conflict(cls, sources: Mapping[str, Mapping[str, Any]]) -> MergeResult:
        # If any key overlaps, fail immediately unless values are identical
        return cls._merge_keyed(sources)

    @classmethod
    def _merge_explicit_precedence(
        cls,
        sources: Mapping[str, Mapping[str, Any]],
        precedence_order: Sequence[str],
    ) -> MergeResult:
        merged: Dict[str, Any] = {}
        conflicts: List[str] = []

        # Determine effective order: explicit precedence followed by remaining sorted alphabetically
        ordered_sources: List[str] = []
        for p in precedence_order:
            if p in sources:
                ordered_sources.append(p)
        for s in sorted(sources.keys()):
            if s not in ordered_sources:
                ordered_sources.append(s)

        # Higher priority wins (precedence goes from index 0 to len-1)
        # We iterate in reverse so higher priority overwrites lower
        for src in reversed(ordered_sources):
            data = sources[src]
            if not isinstance(data, (dict, Mapping)):
                conflicts.append(f"Source {src} output is not a dictionary")
                continue
            for k, v in sorted(data.items(), key=lambda x: str(x[0])):
                merged[k] = v

        success = len(conflicts) == 0
        return MergeResult(
            success=success,
            merged_data=merged if success else {},
            strategy_used=MergeStrategy.EXPLICIT_PRECEDENCE,
            conflicts=tuple(conflicts),
            metadata={"precedence_order": tuple(ordered_sources)},
        )

    @classmethod
    def _values_equal(cls, val1: Any, val2: Any) -> bool:
        if type(val1) != type(val2):
            # Check if numeric decimal/float comparison
            if isinstance(val1, (int, float, Decimal)) and isinstance(val2, (int, float, Decimal)):
                return Decimal(str(val1)) == Decimal(str(val2))
            return False
        if isinstance(val1, (dict, Mapping)):
            if set(val1.keys()) != set(val2.keys()):
                return False
            return all(cls._values_equal(val1[k], val2[k]) for k in val1)
        if isinstance(val1, (list, tuple)):
            if len(val1) != len(val2):
                return False
            return all(cls._values_equal(a, b) for a, b in zip(val1, val2))
        return val1 == val2
