"""
Estimador de tokens determinista y desacoplado de SDKs (Hito M.2).

Transversal M — Control de Coste e Inferencia.
"""

import json
from typing import Any, Optional, Sequence
import math

from src.domain.context_budget.models import InputTokensBreakdown
from src.domain.context_budget.ports import TokenEstimatorPort


class DeterministicTokenEstimator(TokenEstimatorPort):
    """
    Estimador de tokens determinista estándar para arquitectura desacoplada de SDKs.
    Aplica una heurística determinista y conservadora estándar (4 caracteres ~= 1 token o conteo de palabras),
    asegurando un mínimo de 0 tokens y resultados siempre enteros.
    """

    def __init__(self, chars_per_token: float = 4.0):
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token

    def estimate_text_tokens(self, text: str, model_id: Optional[str] = None) -> int:
        if not text:
            return 0
        # Heurística canónica determinista: techo de longitud / chars_per_token
        # Para garantizar un piso realista, consideramos también conteo de palabras
        raw_chars = len(text)
        token_estimate = math.ceil(raw_chars / self._chars_per_token)
        return int(max(1, token_estimate))

    def _estimate_structure_tokens(self, item: Any, model_id: Optional[str] = None) -> int:
        if item is None:
            return 0
        if isinstance(item, str):
            return self.estimate_text_tokens(item, model_id=model_id)
        if isinstance(item, (list, tuple, set)):
            return sum(self._estimate_structure_tokens(x, model_id=model_id) for x in item)
        if isinstance(item, dict):
            serialized = json.dumps(item, sort_keys=True, default=str)
            return self.estimate_text_tokens(serialized, model_id=model_id)
        # Objeto genérico
        return self.estimate_text_tokens(str(item), model_id=model_id)

    def estimate_breakdown(
        self,
        system_instructions: Optional[str] = None,
        user_input: Optional[str] = None,
        memory_context: Optional[str] = None,
        tool_schemas: Optional[Sequence[Any]] = None,
        retrieved_evidence: Optional[Sequence[Any]] = None,
        conversation_history: Optional[Sequence[Any]] = None,
        other: Optional[Any] = None,
        model_id: Optional[str] = None,
    ) -> InputTokensBreakdown:
        sys_tokens = self.estimate_text_tokens(system_instructions or "", model_id=model_id)
        usr_tokens = self.estimate_text_tokens(user_input or "", model_id=model_id)
        mem_tokens = self.estimate_text_tokens(memory_context or "", model_id=model_id)
        tool_tokens = self._estimate_structure_tokens(tool_schemas, model_id=model_id) if tool_schemas else 0
        ev_tokens = self._estimate_structure_tokens(retrieved_evidence, model_id=model_id) if retrieved_evidence else 0
        hist_tokens = self._estimate_structure_tokens(conversation_history, model_id=model_id) if conversation_history else 0
        oth_tokens = self._estimate_structure_tokens(other, model_id=model_id) if other is not None else 0

        return InputTokensBreakdown(
            system_instructions=sys_tokens,
            user_input=usr_tokens,
            memory_context=mem_tokens,
            tool_schemas=tool_tokens,
            retrieved_evidence=ev_tokens,
            conversation_history=hist_tokens,
            other=oth_tokens,
        )
