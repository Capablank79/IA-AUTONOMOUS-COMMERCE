"""
Servicio de Compresión Determinista de Prompt y Contexto (Prompt Compression Service - Hito M.3).

Transversal M — Control de Coste e Inferencia.

M.3 responde:
"Si el contexto excede el presupuesto, ¿cómo lo reducimos de forma determinista sin perder información crítica?"

Principios deterministas:
1. OVER_BUDGET input -> compressed context -> nuevo token estimate -> WITHIN_BUDGET (si es posible).
2. Prioridades estrictas: PROTECTED (system instructions, user request, critical tools) NUNCA se descartan.
3. Estrategias deterministas aplicadas en pipeline ordenado:
   - Paso 1: Deduplicación exacta de contenido (DROP_DUPLICATES).
   - Paso 2: Compactación estructurada (COMPACT_STRUCTURED - normaliza JSON eliminando indentación redundante).
   - Paso 3: Poda de historial antiguo de baja prioridad (PRUNE_OLDEST_HISTORY - conserva los N más recientes).
   - Paso 4: Límite de evidencia opcional/no prioritaria (LIMIT_OPTIONAL_EVIDENCE - conserva los top N).
   - Paso 5: Remoción de items REMOVABLE o LOW_PRIORITY restantes.
4. Si tras todas las estrategias el contexto sigue superando el target budget, retorna CANNOT_COMPRESS sin truncar destructivamente los elementos protegidos.
5. Si el contexto ya estaba WITHIN_BUDGET (o no requería compresión), retorna UNCHANGED.
6. Manejo de UNKNOWN / ERROR cuando el target budget es desconocido o no computable.
7. Auditabilidad total: registro exacto de cada acción aplicada, tokens ahorrados, componentes preservados y reducidos, y checksum SHA-256 canónico.
"""

from copy import deepcopy
import json
from typing import Any, List, Optional, Sequence, Set, Tuple, Union

from src.domain.context_budget.models import (
    ContextBudgetDecision,
    ContextBudgetStatus,
    InputTokensBreakdown,
)
from src.domain.context_budget.ports import TokenEstimatorPort
from src.application.context_budget.token_estimator import DeterministicTokenEstimator
from src.domain.prompt_compression.models import (
    CompressionAction,
    CompressionActionType,
    CompressionPolicy,
    CompressionRequest,
    CompressionResult,
    CompressionStatus,
    ContextComponentType,
    ContextItem,
    CompressedContextPayload,
    PriorityLevel,
    RawContextPayload,
)
from src.domain.prompt_compression.ports import PromptCompressionPort


class DeterministicPromptCompressor(PromptCompressionPort):
    """
    Implementación del compresor determinista de prompt / contexto para el Hito M.3.
    """

    def __init__(
        self,
        token_estimator: Optional[TokenEstimatorPort] = None,
        default_policy: Optional[CompressionPolicy] = None,
    ):
        self._token_estimator = token_estimator or DeterministicTokenEstimator(chars_per_token=4.0)
        self._default_policy = default_policy or CompressionPolicy(
            policy_id="default_deterministic_m3_policy",
            version="1.0.0",
        )

    def _estimate_item_tokens(self, item: ContextItem, model_id: Optional[str] = None) -> int:
        """Estima los tokens de un ContextItem usando el TokenEstimator determinista."""
        if item.token_count is not None:
            return item.token_count
        content = item.content
        if isinstance(content, str):
            return self._token_estimator.estimate_text_tokens(content, model_id=model_id)
        if isinstance(content, (dict, list, tuple)):
            serialized = json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
            return self._token_estimator.estimate_text_tokens(serialized, model_id=model_id)
        return self._token_estimator.estimate_text_tokens(str(content), model_id=model_id)

    def _build_initial_items(
        self,
        raw_payload: RawContextPayload,
        policy: CompressionPolicy,
        model_id: Optional[str] = None,
    ) -> List[ContextItem]:
        """
        Normaliza el RawContextPayload a una lista canónica de ContextItems tipados y ordenados.
        """
        items: List[ContextItem] = []
        seq = 0

        # 1. System instructions
        if raw_payload.system_instructions:
            item = ContextItem(
                item_id="system_instructions_0",
                component_type=ContextComponentType.SYSTEM_INSTRUCTIONS,
                content=raw_payload.system_instructions,
                priority=policy.default_priorities.get(ContextComponentType.SYSTEM_INSTRUCTIONS, PriorityLevel.PROTECTED),
                sequence_order=seq,
            )
            items.append(
                ContextItem(
                    item_id=item.item_id,
                    component_type=item.component_type,
                    content=item.content,
                    priority=item.priority,
                    sequence_order=item.sequence_order,
                    token_count=self._estimate_item_tokens(item, model_id=model_id),
                )
            )
            seq += 1

        # 2. Tool schemas
        if raw_payload.tool_schemas:
            for idx, tool in enumerate(raw_payload.tool_schemas):
                item = ContextItem(
                    item_id=f"tool_schema_{idx}",
                    component_type=ContextComponentType.TOOL_SCHEMAS,
                    content=tool,
                    priority=policy.default_priorities.get(ContextComponentType.TOOL_SCHEMAS, PriorityLevel.PROTECTED),
                    sequence_order=seq,
                )
                items.append(
                    ContextItem(
                        item_id=item.item_id,
                        component_type=item.component_type,
                        content=item.content,
                        priority=item.priority,
                        sequence_order=item.sequence_order,
                        token_count=self._estimate_item_tokens(item, model_id=model_id),
                    )
                )
                seq += 1

        # 3. Memory context
        if raw_payload.memory_context is not None:
            if isinstance(raw_payload.memory_context, (list, tuple)):
                for idx, mem in enumerate(raw_payload.memory_context):
                    item = ContextItem(
                        item_id=f"memory_context_{idx}",
                        component_type=ContextComponentType.MEMORY_CONTEXT,
                        content=mem,
                        priority=policy.default_priorities.get(ContextComponentType.MEMORY_CONTEXT, PriorityLevel.NORMAL),
                        sequence_order=seq,
                    )
                    items.append(
                        ContextItem(
                            item_id=item.item_id,
                            component_type=item.component_type,
                            content=item.content,
                            priority=item.priority,
                            sequence_order=item.sequence_order,
                            token_count=self._estimate_item_tokens(item, model_id=model_id),
                        )
                    )
                    seq += 1
            else:
                item = ContextItem(
                    item_id="memory_context_0",
                    component_type=ContextComponentType.MEMORY_CONTEXT,
                    content=raw_payload.memory_context,
                    priority=policy.default_priorities.get(ContextComponentType.MEMORY_CONTEXT, PriorityLevel.NORMAL),
                    sequence_order=seq,
                )
                items.append(
                    ContextItem(
                        item_id=item.item_id,
                        component_type=item.component_type,
                        content=item.content,
                        priority=item.priority,
                        sequence_order=item.sequence_order,
                        token_count=self._estimate_item_tokens(item, model_id=model_id),
                    )
                )
                seq += 1

        # 4. Retrieved evidence
        if raw_payload.retrieved_evidence:
            for idx, ev in enumerate(raw_payload.retrieved_evidence):
                item = ContextItem(
                    item_id=f"retrieved_evidence_{idx}",
                    component_type=ContextComponentType.RETRIEVED_EVIDENCE,
                    content=ev,
                    priority=policy.default_priorities.get(ContextComponentType.RETRIEVED_EVIDENCE, PriorityLevel.NORMAL),
                    sequence_order=seq,
                )
                items.append(
                    ContextItem(
                        item_id=item.item_id,
                        component_type=item.component_type,
                        content=item.content,
                        priority=item.priority,
                        sequence_order=item.sequence_order,
                        token_count=self._estimate_item_tokens(item, model_id=model_id),
                    )
                )
                seq += 1

        # 5. Conversation history
        if raw_payload.conversation_history:
            for idx, msg in enumerate(raw_payload.conversation_history):
                item = ContextItem(
                    item_id=f"conversation_history_{idx}",
                    component_type=ContextComponentType.CONVERSATION_HISTORY,
                    content=msg,
                    priority=policy.default_priorities.get(ContextComponentType.CONVERSATION_HISTORY, PriorityLevel.LOW_PRIORITY),
                    sequence_order=seq,
                )
                items.append(
                    ContextItem(
                        item_id=item.item_id,
                        component_type=item.component_type,
                        content=item.content,
                        priority=item.priority,
                        sequence_order=item.sequence_order,
                        token_count=self._estimate_item_tokens(item, model_id=model_id),
                    )
                )
                seq += 1

        # 6. Other context
        if raw_payload.other is not None:
            item = ContextItem(
                item_id="other_0",
                component_type=ContextComponentType.OTHER,
                content=raw_payload.other,
                priority=policy.default_priorities.get(ContextComponentType.OTHER, PriorityLevel.LOW_PRIORITY),
                sequence_order=seq,
            )
            items.append(
                ContextItem(
                    item_id=item.item_id,
                    component_type=item.component_type,
                    content=item.content,
                    priority=item.priority,
                    sequence_order=item.sequence_order,
                    token_count=self._estimate_item_tokens(item, model_id=model_id),
                )
            )
            seq += 1

        # 7. User input (siempre al final o con alta prioridad)
        if raw_payload.user_input:
            item = ContextItem(
                item_id="user_input_0",
                component_type=ContextComponentType.USER_INPUT,
                content=raw_payload.user_input,
                priority=policy.default_priorities.get(ContextComponentType.USER_INPUT, PriorityLevel.PROTECTED),
                sequence_order=seq,
            )
            items.append(
                ContextItem(
                    item_id=item.item_id,
                    component_type=item.component_type,
                    content=item.content,
                    priority=item.priority,
                    sequence_order=item.sequence_order,
                    token_count=self._estimate_item_tokens(item, model_id=model_id),
                )
            )
            seq += 1

        # 8. Custom items provistos directamente
        for custom_item in raw_payload.custom_items:
            token_count = custom_item.token_count
            if token_count is None:
                token_count = self._estimate_item_tokens(custom_item, model_id=model_id)
            items.append(
                ContextItem(
                    item_id=custom_item.item_id,
                    component_type=custom_item.component_type,
                    content=custom_item.content,
                    priority=custom_item.priority,
                    sequence_order=custom_item.sequence_order if custom_item.sequence_order else seq,
                    token_count=token_count,
                    is_duplicate=custom_item.is_duplicate,
                    metadata=custom_item.metadata,
                )
            )
            seq += 1

        return items

    def _calculate_total_tokens(self, items: Sequence[ContextItem]) -> int:
        return sum(it.token_count or 0 for it in items)

    def _calculate_breakdown(self, items: Sequence[ContextItem]) -> InputTokensBreakdown:
        breakdown_dict = {
            "system_instructions": 0,
            "user_input": 0,
            "memory_context": 0,
            "tool_schemas": 0,
            "retrieved_evidence": 0,
            "conversation_history": 0,
            "other": 0,
        }
        for item in items:
            t = item.token_count or 0
            if item.component_type == ContextComponentType.SYSTEM_INSTRUCTIONS:
                breakdown_dict["system_instructions"] += t
            elif item.component_type == ContextComponentType.USER_INPUT:
                breakdown_dict["user_input"] += t
            elif item.component_type == ContextComponentType.MEMORY_CONTEXT:
                breakdown_dict["memory_context"] += t
            elif item.component_type == ContextComponentType.TOOL_SCHEMAS:
                breakdown_dict["tool_schemas"] += t
            elif item.component_type == ContextComponentType.RETRIEVED_EVIDENCE:
                breakdown_dict["retrieved_evidence"] += t
            elif item.component_type == ContextComponentType.CONVERSATION_HISTORY:
                breakdown_dict["conversation_history"] += t
            else:
                breakdown_dict["other"] += t

        return InputTokensBreakdown(**breakdown_dict)

    def _reconstruct_payload(self, items: Sequence[ContextItem]) -> CompressedContextPayload:
        """Reconstruye el payload tipado a partir de los items retenidos."""
        sys_inst: Optional[str] = None
        user_in: Optional[str] = None
        mem_items: List[Any] = []
        tools: List[Any] = []
        evidences: List[Any] = []
        histories: List[Any] = []
        others: List[Any] = []

        # Ordenar por sequence_order para determinismo
        sorted_items = sorted(items, key=lambda x: x.sequence_order)

        for it in sorted_items:
            if it.component_type == ContextComponentType.SYSTEM_INSTRUCTIONS:
                sys_inst = it.content if isinstance(it.content, str) else str(it.content)
            elif it.component_type == ContextComponentType.USER_INPUT:
                user_in = it.content if isinstance(it.content, str) else str(it.content)
            elif it.component_type == ContextComponentType.MEMORY_CONTEXT:
                mem_items.append(it.content)
            elif it.component_type == ContextComponentType.TOOL_SCHEMAS:
                tools.append(it.content)
            elif it.component_type == ContextComponentType.RETRIEVED_EVIDENCE:
                evidences.append(it.content)
            elif it.component_type == ContextComponentType.CONVERSATION_HISTORY:
                histories.append(it.content)
            elif it.component_type == ContextComponentType.OTHER:
                others.append(it.content)

        mem_val: Optional[Union[str, Tuple[Any, ...]]] = None
        if len(mem_items) == 1 and isinstance(mem_items[0], str):
            mem_val = mem_items[0]
        elif len(mem_items) > 0:
            mem_val = tuple(mem_items)

        other_val: Optional[Any] = None
        if len(others) == 1:
            other_val = others[0]
        elif len(others) > 1:
            other_val = tuple(others)

        return CompressedContextPayload(
            system_instructions=sys_inst,
            user_input=user_in,
            memory_context=mem_val,
            tool_schemas=tuple(tools) if tools else None,
            retrieved_evidence=tuple(evidences) if evidences else None,
            conversation_history=tuple(histories) if histories else None,
            other=other_val,
            items=tuple(sorted_items),
        )

    def compress_context(
        self,
        request: CompressionRequest,
        policy: Optional[CompressionPolicy] = None,
    ) -> CompressionResult:
        """
        Ejecuta el proceso determinista de compresión de contexto respetando prioridades.
        """
        active_policy = policy or request.policy or self._default_policy

        # 1. Determinar target budget
        target_budget = request.target_budget_tokens
        if target_budget is None and request.budget_decision is not None:
            target_budget = request.budget_decision.available_input_tokens

        # Validar target budget
        if target_budget is None or target_budget <= 0:
            # Budget desconocido o no computable
            return CompressionResult(
                status=CompressionStatus.UNKNOWN if target_budget is None else CompressionStatus.ERROR,
                original_token_count=None,
                final_token_count=None,
                target_budget_tokens=target_budget,
                compressed_payload=None,
                actions_applied=(),
                preserved_components=(),
                reduced_components=(),
                final_breakdown=None,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                rationale="Target budget is unknown or non-positive; cannot compress deterministically without budget constraint",
            )

        # 2. Construir lista inicial de items
        items = self._build_initial_items(
            request.raw_payload,
            active_policy,
            model_id=request.model_id,
        )
        initial_tokens = self._calculate_total_tokens(items)

        # Si el input inicial ya está dentro del presupuesto -> UNCHANGED
        if initial_tokens <= target_budget:
            compressed_payload = self._reconstruct_payload(items)
            preserved = tuple(sorted(list({it.component_type.value for it in items})))
            breakdown = self._calculate_breakdown(items)
            return CompressionResult(
                status=CompressionStatus.UNCHANGED,
                original_token_count=initial_tokens,
                final_token_count=initial_tokens,
                target_budget_tokens=target_budget,
                compressed_payload=compressed_payload,
                actions_applied=(),
                preserved_components=preserved,
                reduced_components=(),
                final_breakdown=breakdown,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                rationale=f"Context is already within target budget ({initial_tokens} <= {target_budget} tokens)",
            )

        actions_applied: List[CompressionAction] = []
        reduced_components_set: Set[str] = set()

        # =========================================================================
        # ESTRATEGIA 1: Deduplicación exacta de contenido (DROP_DUPLICATES)
        # =========================================================================
        if active_policy.allow_drop_duplicates and self._calculate_total_tokens(items) > target_budget:
            seen_signatures: Set[str] = set()
            deduped_items: List[ContextItem] = []
            dropped_ids: List[str] = []
            saved_tokens_dedup = 0

            for it in items:
                if it.priority == PriorityLevel.PROTECTED:
                    deduped_items.append(it)
                    continue

                # Generar firma determinista del contenido
                sig_content = it.content
                if isinstance(sig_content, (dict, list)):
                    sig = f"{it.component_type.value}::" + json.dumps(sig_content, sort_keys=True, default=str)
                else:
                    sig = f"{it.component_type.value}::" + str(sig_content).strip()

                if sig in seen_signatures:
                    # Item duplicado
                    dropped_ids.append(it.item_id)
                    saved_tokens_dedup += (it.token_count or 0)
                    reduced_components_set.add(it.component_type.value)
                else:
                    seen_signatures.add(sig)
                    deduped_items.append(it)

            if dropped_ids:
                items = deduped_items
                actions_applied.append(
                    CompressionAction(
                        action_type=CompressionActionType.DROP_DUPLICATES,
                        target_component=ContextComponentType.OTHER,
                        item_ids_affected=tuple(dropped_ids),
                        tokens_saved=saved_tokens_dedup,
                        rationale=f"Dropped {len(dropped_ids)} duplicate context item(s)",
                    )
                )

        # =========================================================================
        # ESTRATEGIA 2: Compactación estructurada (COMPACT_STRUCTURED)
        # =========================================================================
        if active_policy.allow_compact_structured and self._calculate_total_tokens(items) > target_budget:
            compacted_items: List[ContextItem] = []
            compacted_ids: List[str] = []
            saved_tokens_compact = 0

            for it in items:
                # Si el contenido es un dict o list o un JSON string con indentaciones/espacios superfluos
                if isinstance(it.content, (dict, list)):
                    compact_json = json.dumps(it.content, sort_keys=True, separators=(",", ":"), default=str)
                    new_token_count = self._token_estimator.estimate_text_tokens(compact_json, model_id=request.model_id)
                    old_token_count = it.token_count or 0
                    if new_token_count < old_token_count:
                        saved = old_token_count - new_token_count
                        saved_tokens_compact += saved
                        compacted_ids.append(it.item_id)
                        compacted_items.append(
                            ContextItem(
                                item_id=it.item_id,
                                component_type=it.component_type,
                                content=compact_json,
                                priority=it.priority,
                                sequence_order=it.sequence_order,
                                token_count=new_token_count,
                                is_duplicate=it.is_duplicate,
                                metadata=it.metadata,
                            )
                        )
                        reduced_components_set.add(it.component_type.value)
                        continue
                compacted_items.append(it)

            if compacted_ids:
                items = compacted_items
                actions_applied.append(
                    CompressionAction(
                        action_type=CompressionActionType.COMPACT_STRUCTURED,
                        target_component=ContextComponentType.OTHER,
                        item_ids_affected=tuple(compacted_ids),
                        tokens_saved=saved_tokens_compact,
                        rationale=f"Compacted {len(compacted_ids)} structured item(s) to minified format",
                    )
                )

        # =========================================================================
        # ESTRATEGIA 3: Poda de historial antiguo de baja prioridad (PRUNE_OLDEST_HISTORY)
        # =========================================================================
        if active_policy.allow_prune_history and self._calculate_total_tokens(items) > target_budget:
            history_items = [it for it in items if it.component_type == ContextComponentType.CONVERSATION_HISTORY and it.priority != PriorityLevel.PROTECTED]
            
            # Si hay más historial del límite o si excedemos presupuesto, podar los más antiguos primero
            if history_items:
                # Ordenar por sequence_order ascendente (más antiguos primero)
                history_items_sorted = sorted(history_items, key=lambda x: x.sequence_order)
                pruned_ids: List[str] = []
                saved_tokens_hist = 0

                # Conservar como máximo los max_history_items_to_keep más recientes
                excess_count = max(0, len(history_items_sorted) - active_policy.max_history_items_to_keep)
                to_prune = history_items_sorted[:excess_count]

                # Si aún superamos presupuesto, continuar podando de los más antiguos que queden no protegidos
                remaining_history = history_items_sorted[excess_count:]
                current_total = self._calculate_total_tokens(items) - sum(it.token_count or 0 for it in to_prune)
                
                idx = 0
                while current_total > target_budget and idx < len(remaining_history):
                    # Conservamos al menos el último mensaje de historial si es posible, salvo que sea indispensable podar
                    if idx == len(remaining_history) - 1 and len(remaining_history) > 1 and current_total - (remaining_history[idx].token_count or 0) < target_budget:
                        pass
                    to_prune.append(remaining_history[idx])
                    current_total -= (remaining_history[idx].token_count or 0)
                    idx += 1

                for p_item in to_prune:
                    pruned_ids.append(p_item.item_id)
                    saved_tokens_hist += (p_item.token_count or 0)

                if pruned_ids:
                    prune_set = set(pruned_ids)
                    items = [it for it in items if it.item_id not in prune_set]
                    reduced_components_set.add(ContextComponentType.CONVERSATION_HISTORY.value)
                    actions_applied.append(
                        CompressionAction(
                            action_type=CompressionActionType.PRUNE_OLDEST_HISTORY,
                            target_component=ContextComponentType.CONVERSATION_HISTORY,
                            item_ids_affected=tuple(pruned_ids),
                            tokens_saved=saved_tokens_hist,
                            rationale=f"Pruned {len(pruned_ids)} oldest conversation history item(s)",
                        )
                    )

        # =========================================================================
        # ESTRATEGIA 4: Límite de evidencia opcional/no prioritaria (LIMIT_OPTIONAL_EVIDENCE)
        # =========================================================================
        if active_policy.allow_limit_evidence and self._calculate_total_tokens(items) > target_budget:
            evidence_items = [
                it for it in items
                if it.component_type == ContextComponentType.RETRIEVED_EVIDENCE and it.priority not in (PriorityLevel.PROTECTED, PriorityLevel.HIGH_PRIORITY)
            ]

            if evidence_items:
                # Podar primero los de menor prioridad (LOW_PRIORITY antes de NORMAL) y luego por sequence_order descendente
                evidence_items_sorted = sorted(
                    evidence_items,
                    key=lambda x: (
                        0 if x.priority == PriorityLevel.LOW_PRIORITY else 1,
                        x.sequence_order
                    )
                )

                pruned_ev_ids: List[str] = []
                saved_tokens_ev = 0

                # Mantener como máximo max_evidence_items_to_keep
                excess_ev = max(0, len(evidence_items) - active_policy.max_evidence_items_to_keep)
                to_prune_ev = evidence_items_sorted[:excess_ev]

                current_total = self._calculate_total_tokens(items) - sum(it.token_count or 0 for it in to_prune_ev)
                remaining_ev = evidence_items_sorted[excess_ev:]
                
                idx = 0
                while current_total > target_budget and idx < len(remaining_ev):
                    to_prune_ev.append(remaining_ev[idx])
                    current_total -= (remaining_ev[idx].token_count or 0)
                    idx += 1

                for p_item in to_prune_ev:
                    pruned_ev_ids.append(p_item.item_id)
                    saved_tokens_ev += (p_item.token_count or 0)

                if pruned_ev_ids:
                    prune_ev_set = set(pruned_ev_ids)
                    items = [it for it in items if it.item_id not in prune_ev_set]
                    reduced_components_set.add(ContextComponentType.RETRIEVED_EVIDENCE.value)
                    actions_applied.append(
                        CompressionAction(
                            action_type=CompressionActionType.LIMIT_OPTIONAL_EVIDENCE,
                            target_component=ContextComponentType.RETRIEVED_EVIDENCE,
                            item_ids_affected=tuple(pruned_ev_ids),
                            tokens_saved=saved_tokens_ev,
                            rationale=f"Limited {len(pruned_ev_ids)} optional retrieved evidence item(s)",
                        )
                    )

        # =========================================================================
        # ESTRATEGIA 5: Remoción de items REMOVABLE o LOW_PRIORITY restantes
        # =========================================================================
        if self._calculate_total_tokens(items) > target_budget:
            low_prio_items = [
                it for it in items
                if it.priority in (PriorityLevel.REMOVABLE, PriorityLevel.LOW_PRIORITY)
            ]
            if low_prio_items:
                removed_ids: List[str] = []
                saved_tokens_low = 0
                current_total = self._calculate_total_tokens(items)

                for it in low_prio_items:
                    if current_total <= target_budget:
                        break
                    removed_ids.append(it.item_id)
                    saved_tokens_low += (it.token_count or 0)
                    current_total -= (it.token_count or 0)
                    reduced_components_set.add(it.component_type.value)

                if removed_ids:
                    rem_set = set(removed_ids)
                    items = [it for it in items if it.item_id not in rem_set]
                    actions_applied.append(
                        CompressionAction(
                            action_type=CompressionActionType.REMOVE_LOW_PRIORITY,
                            target_component=ContextComponentType.OTHER,
                            item_ids_affected=tuple(removed_ids),
                            tokens_saved=saved_tokens_low,
                            rationale=f"Removed {len(removed_ids)} low priority / removable context item(s)",
                        )
                    )

        # 3. Evaluar resultado final tras aplicar todas las estrategias permitidas
        final_tokens = self._calculate_total_tokens(items)
        compressed_payload = self._reconstruct_payload(items)
        preserved_components = tuple(sorted(list({it.component_type.value for it in items})))
        reduced_components = tuple(sorted(list(reduced_components_set)))
        final_breakdown = self._calculate_breakdown(items)

        if final_tokens <= target_budget:
            # Compresión exitosa dentro del presupuesto
            return CompressionResult(
                status=CompressionStatus.COMPRESSED,
                original_token_count=initial_tokens,
                final_token_count=final_tokens,
                target_budget_tokens=target_budget,
                compressed_payload=compressed_payload,
                actions_applied=tuple(actions_applied),
                preserved_components=preserved_components,
                reduced_components=reduced_components,
                final_breakdown=final_breakdown,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                rationale=(
                    f"Successfully compressed context from {initial_tokens} to {final_tokens} tokens "
                    f"(target budget: {target_budget} tokens, saved: {initial_tokens - final_tokens} tokens)"
                ),
            )

        # Si aún no cabe tras podar todo lo no protegido -> CANNOT_COMPRESS
        # Los componentes PROTECTED se mantienen intactos sin mutilación opaca
        return CompressionResult(
            status=CompressionStatus.CANNOT_COMPRESS,
            original_token_count=initial_tokens,
            final_token_count=final_tokens,
            target_budget_tokens=target_budget,
            compressed_payload=compressed_payload,
            actions_applied=tuple(actions_applied),
            preserved_components=preserved_components,
            reduced_components=reduced_components,
            final_breakdown=final_breakdown,
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            rationale=(
                f"Context could not be reduced below target budget ({final_tokens} > {target_budget} tokens). "
                f"Protected components ({', '.join(preserved_components)}) were strictly preserved to prevent semantic destruction."
            ),
        )
