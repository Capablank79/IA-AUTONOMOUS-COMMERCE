# REPORTE DE EJECUCIÓN: M.3 — PROMPT COMPRESSION

## STATUS

- **Transversal:** M — Control de Coste e Inferencia
- **Hito:** M.3 — Prompt Compression
- **Estado:** 🟢 VALIDADA
- **Resultado específico actual:** 15 passed, 0 failures, 0 errors
- **Checkpoint:** incluido en commit de cierre Hito M + Gate L

## ROADMAP / GANTT ALIGNMENT

M.3 corresponde a Prompt Compression dentro del Transversal M. El código y las pruebas actuales confirman que M.3 está validada. La Gantt maestra registra Hito M como completo y Gate L como pasado. Gate L valida M.3 dentro del flujo comercial M.1–M.6 y K.3. No se implementó ni modificó lógica de Hito N durante esta reconstrucción documental.

## DISCOVERY / REUSE

| Capacidad | Ubicación | Decisión |
|---|---|---|
| Modelos y evaluación de presupuesto M.2 | `src/domain/context_budget/` | **REUSE** de `ContextBudgetDecision`, `ContextBudgetStatus` e `InputTokensBreakdown`. |
| Estimación de tokens M.2 | `src/application/context_budget/token_estimator.py` | **REUSE** de `DeterministicTokenEstimator` mediante `TokenEstimatorPort`. |
| Sanitización y congelamiento | `src/domain/model_routing/models.py` | **REUSE** de `sanitize_routing_data` y `deep_freeze` para metadata. |
| Modelos de compresión | `src/domain/prompt_compression/models.py` | Implementación propia de M.3. |
| Puerto de compresión | `src/domain/prompt_compression/ports.py` | Implementación propia de M.3. |
| Compresor determinista | `src/application/prompt_compression/deterministic_compressor.py` | Implementación propia de M.3. |

## ARCHITECTURE

La capa de dominio contiene contratos, enums y modelos inmutables. `PromptCompressionPort` define `compress_context`. La capa de aplicación implementa el puerto mediante `DeterministicPromptCompressor`, recibe un estimador por inyección o reutiliza el estimador determinista de M.2, normaliza el payload a `ContextItem`, aplica un pipeline ordenado y reconstruye un `CompressedContextPayload` auditable.

El flujo validado es: evaluación M.2 → compresión M.3 cuando existe desborde → nuevo `InputTokensBreakdown` → reevaluación M.2. Si el contexto ya cabe, M.3 devuelve `UNCHANGED`; si no puede caber sin destruir contexto protegido, devuelve `CANNOT_COMPRESS`.

## DOMAIN MODELS

- `CompressionRequest`: payload original, presupuesto objetivo opcional, decisión M.2 opcional, modelo, política y metadata sanitizada.
- `CompressionPolicy`: política inmutable y versionada con estrategias habilitables, límites de historial/evidencia y prioridades por tipo de componente.
- `CompressionResult`: estado, conteos original/final, target, payload comprimido, acciones, componentes preservados/reducidos, breakdown final, política, rationale y checksum calculable.
- `CompressionAction`: acción inmutable con tipo, componente objetivo, IDs afectados, tokens ahorrados y rationale.
- `ContextItem`: unidad granular inmutable con tipo, contenido, prioridad, orden, conteo opcional y metadata sanitizada.
- `RawContextPayload` y `CompressedContextPayload`: contratos tipados de entrada y salida.

Estados canónicos confirmados: `COMPRESSED`, `UNCHANGED`, `CANNOT_COMPRESS`, `UNKNOWN`, `ERROR`.

## COMPRESSION POLICY

La política por defecto se identifica como `default_deterministic_m3_policy`, versión `1.0.0`. Permite deduplicación, poda de historial, límite de evidencia y compactación estructurada. Sus límites por defecto conservan como máximo 5 elementos de historial y 10 elementos de evidencia cuando las estrategias se activan por exceso de presupuesto. Las políticas personalizadas preservan su ID y versión en el resultado.

## PRIORITIES

Prioridades reales confirmadas en código:

1. `PROTECTED`
2. `HIGH_PRIORITY`
3. `NORMAL`
4. `LOW_PRIORITY`
5. `REMOVABLE`

Por defecto, instrucciones del sistema, entrada actual del usuario y esquemas de tools son `PROTECTED`; memoria y evidencia recuperada son `NORMAL`; historial y otros elementos son `LOW_PRIORITY`. Los `ContextItem` personalizados pueden declarar su prioridad explícitamente.

## COMPRESSION ACTIONS

El pipeline determinista aplica, en orden:

1. `DROP_DUPLICATES`: elimina duplicados exactos no protegidos por firma de tipo y contenido.
2. `COMPACT_STRUCTURED`: serializa estructuras `dict`/`list` como JSON canónico compacto cuando reduce tokens.
3. `PRUNE_OLDEST_HISTORY`: poda primero historial no protegido más antiguo.
4. `LIMIT_OPTIONAL_EVIDENCE`: limita evidencia no protegida ni de alta prioridad.
5. `REMOVE_LOW_PRIORITY`: elimina elementos restantes `REMOVABLE` o `LOW_PRIORITY` sólo mientras persiste el exceso.

`NO_OP` existe como tipo de acción canónico, aunque la rama `UNCHANGED` representa la ausencia de acciones con una tupla vacía.

## M.2 INTEGRATION

M.3 acepta `ContextBudgetDecision`; cuando no se entrega un target explícito, usa `available_input_tokens`. El servicio reutiliza `TokenEstimatorPort` y `DeterministicTokenEstimator`. La integración prueba el flujo M.1 → M.2 `OVER_BUDGET` → M.3 `COMPRESSED` → reevaluación M.2 `WITHIN_BUDGET`.

## TOKEN ACCOUNTING

Cada item recibe un conteo explícito o una estimación determinista. El total es la suma entera de los items. El resultado expone `original_token_count`, `final_token_count`, `tokens_saved` y un `InputTokensBreakdown` final con instrucciones, usuario, memoria, tools, evidencia, historial y otros. La prueba unitaria confirma que el total del breakdown coincide con `final_token_count` y que los tokens ahorrados coinciden con la diferencia entre conteos.

## PROTECTED CONTEXT

Los items `PROTECTED` no se eliminan durante deduplicación, poda de historial, límite de evidencia ni remoción por baja prioridad. Las instrucciones del sistema, la entrada actual del usuario y los esquemas de tools tienen esa prioridad por defecto. Cuando esos componentes por sí solos exceden el target, el resultado es `CANNOT_COMPRESS`, conserva el contenido intacto y no aplica truncamiento ciego.

## DETERMINISM

El orden se basa en `sequence_order`; las firmas y serializaciones estructuradas usan claves ordenadas; las acciones registran IDs y tokens; los componentes se reportan ordenados. `CompressionResult.calculate_checksum()` serializa un payload canónico y calcula SHA-256. La prueba de checksum ejecuta dos compresiones equivalentes y obtiene el mismo hash hexadecimal de 64 caracteres.

## SECURITY

La metadata de `ContextItem` y `CompressionRequest` pasa por `sanitize_routing_data` y `deep_freeze`. M.3 no persiste prompts, credenciales, tokens ni Chain-of-Thought, ni añade logging de esos contenidos. La preservación de componentes protegidos evita degradar silenciosamente instrucciones de seguridad o requisitos actuales del usuario.

## UNIT TESTS

Comando ejecutado:

`python -m pytest tests/unit/test_m3_prompt_compression_unit.py -vv`

Resultado real actual: **11 passed in 0.23s; 0 failures; 0 errors**.

Cobertura comprobada: `UNCHANGED`, deduplicación, compactación, poda de historial, límite de evidencia, preservación protegida, `CANNOT_COMPRESS`, presupuesto desconocido/inválido, políticas versionadas, checksum y accounting.

## INTEGRATION / E2E

Comando ejecutado:

`python -m pytest tests/integration/test_m3_prompt_compression_integration.py -vv`

Resultado real actual: **4 passed in 0.35s; 0 failures; 0 errors**.

Escenarios comprobados:

- M.1 → M.2 `OVER_BUDGET` → M.3 `COMPRESSED` → M.2 `WITHIN_BUDGET`.
- Contexto ya ajustado → `UNCHANGED`.
- Presupuesto imposible → `CANNOT_COMPRESS` con contexto crítico intacto.
- Integración con `LoopState`, construcción de prompt y boundary `OmniRouteDecisionProvider` mockeado.

Gate L registra además 7 pruebas E2E pasadas, regresión M.1–M.6 + K.3 de 160 pruebas pasadas y regresión completa de **1543 passed, 1 skipped, 0 failures, 0 errors**.

## BOUNDARIES M.4–M.6

M.3 no implementa caché M.4, selección de modelo por tarea M.5 ni política económica M.6. Produce un contexto final y accounting que esas capas pueden consumir. M.4 usa el contexto efectivo para claves de caché; M.6 usa tokens finales post-compresión para estimar coste; ninguna de esas responsabilidades está dentro del compresor.

## FILES CREATED / MODIFIED

Implementación y pruebas M.3 existentes verificadas:

- `src/domain/prompt_compression/__init__.py`
- `src/domain/prompt_compression/models.py`
- `src/domain/prompt_compression/ports.py`
- `src/application/prompt_compression/__init__.py`
- `src/application/prompt_compression/deterministic_compressor.py`
- `tests/unit/test_m3_prompt_compression_unit.py`
- `tests/integration/test_m3_prompt_compression_integration.py`

Archivo creado en esta reconstrucción:

- `M3_PROMPT_COMPRESSION_EXECUTION_REPORT.md`

No se modificó lógica productiva de M.3 durante la reconstrucción.

## GIT STATUS

Antes de crear este reporte, `git status --short --branch` mostró `master...origin/master`, cambios no staged correspondientes a M.1–M.6, Gate L, Gantt e integraciones legítimas. El reporte M.3 era el único reporte formal faltante. El estado definitivo de staging y checkpoint se verifica después de crear y validar este archivo.

## FINAL DECISION

La evidencia actual de código y pruebas confirma que M.3 está completa, determinista, integrada con M.2, protege contexto crítico, evita truncamiento ciego y respeta los límites de M.4–M.6.

**M.3 — Prompt Compression → 🟢 VALIDADA**

Checkpoint de Hito M + Gate L ejecutado tras verificaciones de consistencia, higiene, diff, staging controlado y seguridad.
