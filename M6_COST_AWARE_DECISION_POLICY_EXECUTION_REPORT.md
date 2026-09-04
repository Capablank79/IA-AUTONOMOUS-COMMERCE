# M.6 COST-AWARE DECISION POLICY — EXECUTION REPORT

## 1. Resumen Ejecutivo y Estado

* **Componente**: M.6 — Cost-aware Decision Policy (Transversal M: Control de Coste e Inferencia).
* **Estado M.6**: 🟢 VALIDADA.
* **Estado Hito M**: 🟡 EN PROGRESO.
* **Estado Gate L**: ⚪ PENDIENTE (No ejecutado accidentalmente).
* **Baseline Previo**: 1514 passed, 1 skipped, 0 failures.
* **Resultado Actual**: 1536 passed, 1 skipped, 0 failures (22 nuevos tests agregados: 15 unitarios + 7 de integración/E2E).

---

## 2. Responsabilidad Arquitectónica de M.6

M.6 responde deterministamente a la pregunta:
> *"Entre opciones técnicamente válidas, ¿qué decisión de inferencia cumple la política de coste sin violar requisitos mínimos de calidad, capacidad o criticidad?"*

### Principios Fundamentales Implementados
1. **Quality First (No False Economy)**: Descarte estricto y prioritario de rutas antes de cualquier evaluación económica si no satisfacen las capacidades técnicas requeridas (M.1 / M.5), no alcanzan el piso de calidad (`QualityRequirement`) o violan restricciones de tareas críticas (`TaskCriticality.HIGH`). *Un modelo barato e incapaz jamás es seleccionado.*
2. **Aritmética Monetaria Exacta**: Uso exclusivo de `decimal.Decimal` para tarifas de precios, costos estimados, importes acumulados y presupuestos. Prohibición total de tipos `float`.
3. **Semántica Rigurosa de Incertidumbre (`UNKNOWN != FREE`)**: Si las tarifas de precios no están registradas o la información económica es insuficiente, el coste estimado es `UNKNOWN`. En tareas críticas o bajo políticas de presupuesto estricto, esto previene aprobaciones silenciosas.
4. **Tratamiento del Impacto de Caché (M.4)**: Reconocimiento explícito de omisión de inferencia (`CacheLookupStatus.HIT`) marcando el coste incremental estimado como `Decimal("0.00")` e informando `cache_impact_avoided=True`, sin alterar registros contables históricos de K.3.
5. **Alineación con Compresión y Presupuesto de Contexto (M.2 / M.3)**: Estimación de coste sobre el conteo final de tokens tras presupuestación y compresión determinista, sin duplicar operaciones de compresión.
6. **Separación Estricta entre Estimación y Hecho (`ESTIMATED` vs `ACTUAL`)**: M.6 genera estimaciones previas (`RouteCostEstimate` y `CostAwareDecision`), y delega la contabilidad de costes observados post-inferencia a K.3 (`CostTrackingService` / `CostRecord`).
7. **Inmutabilidad, Auditoría y Sanitización (K.8)**: Modelos con `frozen=True`, colecciones inmutables (`MappingProxyType`, tuplas), checksum SHA-256 canónico y sanitización recursiva de claves API, tokens de autenticación y cadenas de pensamiento (*Chain-of-Thought*).

---

## 3. Matriz de Componentes (Discovery & Reuse Matrix)

| Capacidad / Entidad | Ubicación | Estrategia | Descripción / Vínculo |
|---|---|---|---|
| **Cost Models** | `src/domain/cost/models.py` | REUSE | Reutilización de `PricingRate`, `CostRecord`, `CostType`, `UsageRecord` y `Currency` (K.3). |
| **Pricing Catalog** | `src/application/cost/pricing_catalog.py` | REUSE | Consulta no intrusiva de tarifas vigentes por proveedor y modelo (K.3). |
| **Model Routes & Capabilities** | `src/domain/model_routing/models.py` | REUSE | Reutilización de `ModelRoute`, `RouteCapability`, `QualityRequirement`, `TaskCriticality` (M.1). |
| **Context Budget & Tokens** | `src/domain/context_budget/models.py` | REUSE | Extracción de tokens de entrada solicitados y reserva de salida (M.2). |
| **Prompt Compression Result** | `src/domain/prompt_compression/models.py` | REUSE | Consumo del recuento de tokens final post-compresión (M.3). |
| **Cache Lookup Result** | `src/domain/caching/models.py` | REUSE | Detección de Cache HIT e inferencia evitada con coste incremental cero (M.4). |
| **Task Requirements** | `src/domain/model_selection/models.py` | REUSE | Integración con perfiles y requerimientos de selección de tareas (M.5). |
| **Cost Aware Domain Models** | `src/domain/cost_aware_policy/models.py` | CREATE | `CostAwareDecisionStatus`, `CostAwareReasonCode`, `RouteCostEstimate`, `CostAwarePolicy`, `CostAwareRequest`, `CostAwareDecision`. |
| **Cost Decision Service** | `src/application/cost_aware_policy/cost_aware_decision_service.py` | CREATE | Servicio de evaluación y selección determinista *Quality First*. |

---

## 4. Auditoría y Verificación de Reglas

* **¿M.6 duplica K.3?**
  **NO.** M.6 utiliza el `PricingCatalogPort` de K.3 para estimaciones previas a la inferencia y no crea un segundo ledger contable. La persistencia de costos reales consumidos sigue perteneciendo a `CostTrackingService` (K.3).
* **¿Se reutilizan M.1–M.5?**
  **SÍ.** M.6 orquesta y respeta los contratos de M.1 (rutas y capacidades), M.2 (tokens estimados/reservados), M.3 (reducción por compresión), M.4 (omisión de inferencia por caché) y M.5 (taxonomía de requerimientos de tareas).
* **¿Cost UNKNOWN se trata como 0?**
  **NO.** `UNKNOWN != FREE`. Una tarifa desconocida genera estado `UNKNOWN` y razón `UNKNOWN_PRICING` / `UNKNOWN_COST`, impidiendo falsos accesos económicos en tareas críticas.
* **¿Cheapest always wins?**
  **NO.** Se aplica estrictamente el filtro de capacidades obligatorias y piso de calidad (`QualityRequirement`). Si un modelo más barato no cumple las restricciones técnicas, es descartado con `CAPABILITY_UNMET` o `QUALITY_UNMET`.
* **¿Quality / Criticality pueden sacrificarse?**
  **NO.** Las tareas con criticidad `HIGH` rechazan modelos degradados (`allow_degraded_for_critical=False`) y precios desconocidos (`allow_unknown_cost_for_critical=False`).
* **¿El presupuesto es configurable y versionado?**
  **SÍ.** `CostAwarePolicy` incluye `policy_id`, `version`, `max_cost_per_inference`, `max_cost_by_task_class`, `max_cost_per_mission`, `allowed_currencies` y checksum SHA-256.
* **¿Estimated y Actual están separados?**
  **SÍ.** `estimated_cost` en `CostAwareDecision` representa la cota prevista y `CostRecord.amount` en K.3 representa el consumo real medido.
* **¿El impacto de caché es correcto?**
  **SÍ.** Un Cache HIT documenta inferencia evitada con coste incremental `Decimal("0.00")` sin distorsionar la procedencia ni los registros históricos.
* **¿La decisión es determinista?**
  **SÍ.** Mismos parámetros y catálogo producen idéntica decisión, estimación y ordenación determinista con desempate lexicográfico por `route_id`.
* **¿Se ejecutó Gate L accidentalmente?**
  **NO.** Gate L permanece en estado ⚪ PENDIENTE para su posterior ejecución formal.

---

## 5. Resumen de Tests y Cobertura

### Tests Unitarios (`tests/unit/test_m6_cost_aware_decision_policy_unit.py`) — 15 Tests Passed
1. `test_1_route_within_budget`: Ruta válida dentro del presupuesto -> APPROVED.
2. `test_2_route_over_budget`: Ruta válida que excede el presupuesto -> REJECTED (`EXCEEDS_BUDGET`).
3. `test_3_cheaper_valid_route_selected`: Entre dos rutas válidas y capaces, se selecciona la más económica.
4. `test_4_cheap_incapable_route_excluded`: Ruta barata sin capacidad técnica requerida excluida (`CAPABILITY_UNMET`).
5. `test_5_cheap_low_quality_route_excluded`: Ruta barata con calidad inferior al piso excluida (`QUALITY_UNMET`).
6. `test_6_high_criticality_preserved`: Tarea crítica rechaza modelos degradados o de baja calidad.
7. `test_7_unknown_cost_not_zero`: Coste desconocido no se normaliza a 0.00.
8. `test_8_missing_pricing_produces_unknown`: Modelo sin tarifa en catálogo resulta en estado UNKNOWN.
9. `test_9_decimal_cost_precision`: Verificación de precisión monetaria sin flotantes.
10. `test_10_cache_hit_cost_handling`: Cache HIT resulta en coste incremental 0.00 y motivo `CACHE_HIT_AVOIDED`.
11. `test_11_compressed_token_count_used`: Ingesta y uso directo del recuento de tokens post-compresión (M.3).
12. `test_12_deterministic_tie_break`: Desempate determinista lexicográfico por `route_id`.
13. `test_13_policy_versioning_and_checksum`: Inmutabilidad y checksum SHA-256 de la política y decisión.
14. `test_14_estimated_vs_actual_separation`: Coexistencia sin colisión entre estimación M.6 y registro K.3.
15. `test_15_no_false_approval`: Solicitud sin rutas válidas produce `NO_ELIGIBLE_OPTION`.

### Tests de Integración (`tests/integration/test_m6_cost_aware_decision_policy_integration.py`) — 7 Tests Passed
* **Escenario A**: Tarea M.5 -> Rutas M.1 -> Presupuesto M.2 -> Selección consciente de coste M.6 dentro del techo.
* **Escenario B**: Ruta más barata carece de capacidad técnica (ej. `VISION`) -> Descartada en favor de ruta capaz.
* **Escenario C**: Todas las rutas válidas superan el techo presupuestario -> REJECTED / NO_ELIGIBLE_OPTION.
* **Escenario D**: Tarifa no registrada en catálogo en tarea crítica -> UNKNOWN.
* **Escenario E**: Compresión de prompt M.3 reduce tokens -> Coste estimado proporcionalmente menor.
* **Escenario F**: Cache HIT M.4 -> Inferencia evitada y coste incremental 0.00.
* **Escenario G & E2E Flow**: Pipeline completo integrado (Misión -> M.5 -> M.1 -> M.2 -> M.3 -> M.4 -> M.6 -> Mock Inference -> Registro contable en K.3).

---

## 6. Estado de Git e Higiene

* `git ls-files .pytest_tmp`: Limpio.
* `git diff --check`: Sin errores de espacios en blanco ni conflictos.
* Sin commits ni pushes realizados.

---

## 7. Próximo Paso Inmediato

* **Próxima Tarea**: `GATE L — FORMAL HITO M VALIDATION` (Demostración de que una misión comercial completa posee coste de inferencia medible y controlable a través de todo el Transversal M).
