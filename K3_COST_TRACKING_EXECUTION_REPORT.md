# K.3 COST TRACKING — INFORME DE EJECUCIÓN Y VALIDACIÓN

**Hito:** K — Observability / Evaluation / Reliability  
**Tarea:** K.3 — Cost Tracking  
**Estado:** 🟢 VALIDADA  
**Fecha de Validación:** 2026-09-01  
**Responsable:** TraeCode Master Agent  

---

## 1. STATUS

- **K.1 Audit Trail:** 🟢 VALIDADA
- **K.2 Agent Trace:** 🟢 VALIDADA
- **K.3 Cost Tracking:** 🟢 VALIDADA
- **K.4 Evaluation Harness:** ⚪ PENDIENTE
- **K.5–K.8:** ⚪ / 🟡 (Según estado previo)
- **Gate J:** ⚪ PENDIENTE
- **Hito M (Cost / Inference Control):** ⚪ PENDIENTE (Sin alteraciones ni implementación prematura)

---

## 2. ROADMAP / GANTT

Se reconcilió el alcance exacto de K.3 a partir de:
- `docs/market-intelligence/AI_AUTONOMOUS_COMMERCE_ROADMAP_MAESTRO.md` (Sección 11.3)
- `docs/market-intelligence/AI_AUTONOMOUS_COMMERCE_GANTT_MAESTRA.md` (Sección 13, Hito K)

K.3 proporciona medición estructurada, trazable, persistente y auditable de costes operacionales, respondiendo con exactitud:
- **WHAT COST:** Tipo de costo, proveedor y servicio/modelo/tool.
- **WHO CAUSED IT:** Proveedor/agente/módulo invocador.
- **FOR WHICH MISSION:** `mission_id`.
- **FOR WHICH EXECUTION:** `execution_id`, `trace_id`, `cycle_id`.
- **WHEN:** `occurred_at` (timestamp UTC ISO 8601 del hecho).
- **HOW MUCH:** Cantidad consumida (`usage_quantity`), tarifa unitaria (`unit_cost`) y costo total (`total_cost`) en aritmética exacta `Decimal`.
- **IN WHICH UNIT/CURRENCY:** `usage_unit` (`TOKENS`, `REQUESTS`, etc.) y `currency` explícita (e.g. `USD`, `CLP`, etc.).
- **BASED ON WHICH SOURCE:** `pricing_source` y `pricing_version` / vigencia temporal.

---

## 3. GIT STATE

- Branch: `master` (up to date with `origin/master`).
- Commits realizados: 0 (conforme a la regla de no commit / no push).
- Estado de Diff: Limpio de conflictos (`git diff --check` PASS).
- Preservación íntegra de artefactos previos de K.1 Audit Trail y K.2 Agent Trace.

---

## 4. DISCOVERY & CLASIFICACIÓN

- **REUSE:**
  - `src/domain/agent_trace/models.py` & `src/application/agent_trace/agent_trace_service.py` (K.2) para enlace de trazas y correlación de identificadores.
  - `src/domain/audit/models.py` & `src/infrastructure/persistence/data/json/audit_repository.py` (K.1) para emisión opcional y no invasiva de eventos `COST_RECORDED`.
- **EXTEND:**
  - `src/application/mission/autonomous_loop.py` y `src/application/continuous_mission/service.py` para instrumentar medición transparente sin alterar su lógica operativa.
- **CREATE:**
  - `src/domain/cost/models.py`: Modelos inmutables (`CostRecord`, `UsageRecord`, `PricingRate`, `CostSummary`, `CurrencyCostSummary`, `CostType`, `UsageUnit`).
  - `src/domain/cost/ports.py`: Contratos de catálogo de precios (`PricingCatalogPort`) y persistencia (`CostRepositoryPort`).
  - `src/application/cost/pricing_catalog.py`: Implementación in-memory de tarifas con versionado y vigencias temporales.
  - `src/infrastructure/persistence/data/json/cost_repository.py`: Repositorio JSON append-only, atómico (`.tmp` + `os.replace` + `fsync`), indexado y seguro con checksums SHA-256.
  - `src/application/cost/cost_tracking_service.py`: Servicio de orquestación, normalización de usage, cálculo determinista, agregaciones multi-moneda y aislamiento de fallos.

---

## 5. GAP ANALYSIS & BOUNDARIES

### K.2 vs. K.3 Boundary
- **K.2 Agent Trace:** Registra la ejecución secuencial, causal y operacional de pasos del agente (`START`, `OBSERVE`, `SERVICE_CALL`, `POLICY_EVALUATION`, `TOOL_CALL`, etc.). No almacena balances contables como único almacén.
- **K.3 Cost Tracking:** Es el dueño de la métrica económica. Almacena registros de costes inmutables vinculados mediante `execution_id`, `trace_id`, `mission_id` y `correlation_id`.

### K.3 vs. Hito M Boundary
- **K.3:** MIDE costes con total fidelidad y neutralidad observacional. No toma decisiones de ahorro, no rutea modelos, no impone presupuestos que cancelen ejecuciones de negocio, no comprime prompts ni altera flujos.
- **Hito M:** Utilizará los datos medidos por K.3 para optimización, ruteo por coste y caching cuando sea su turno de desarrollo.

---

## 6. COST DOMAIN MODEL

Modelos inmutables (`@dataclass(frozen=True)`):
- `CostType`: `INFERENCE`, `TOOL_CALL`, `EXTERNAL_API`, `COMPUTE_OPERATIONAL`.
- `UsageUnit`: `TOKENS`, `REQUESTS`, `SECONDS`, `ITEMS`, `UNKNOWN`.
- `UsageRecord`: Encapsula `input_quantity`, `output_quantity`, `total_quantity`, unidad y detalles. Incluye fábrica `from_tokens(prompt_tokens, completion_tokens)` y `unknown()`.
- `PricingRate`: Tarifa estructurada con escala (`rate_scale`, e.g. 1M para tokens), tarifa plana (`flat_rate`), tarifas de entrada/salida (`input_rate`, `output_rate`), moneda y vigencia temporal (`effective_from`, `effective_to`).
- `CostRecord`: Registro auditable inmutable con `cost_id`, `occurred_at`, `cost_type`, `provider`, `service_or_model`, `execution_id`, `usage`, `unit_cost`, `total_cost`, `currency`, `pricing_source`, `pricing_version`, `trace_id`, `mission_id`, `cycle_id`, `idempotency_key`, `checksum` SHA-256 y metadata sanitizada.

---

## 7. COST CALCULATION & EXACT DECIMAL ARITHMETIC

- Todos los cálculos monetarios utilizan `decimal.Decimal` (prohibido `float`).
- Para inferencia token-based con escala de 1,000,000 tokens:
  $$\text{cost}_{\text{input}} = \left(\frac{\text{input\_tokens}}{\text{rate\_scale}}\right) \times \text{input\_rate}$$
  $$\text{cost}_{\text{output}} = \left(\frac{\text{output\_tokens}}{\text{rate\_scale}}\right) \times \text{output\_rate}$$
  $$\text{total\_cost} = \text{cost}_{\text{input}} + \text{cost}_{\text{output}}$$
- Para llamadas a APIs/Tools con tarifa plana por request:
  $$\text{total\_cost} = \text{flat\_rate} \times \text{quantity}$$

---

## 8. SEMÁNTICA UNKNOWN (UNKNOWN ≠ 0.00)

- Si el proveedor no reporta tokens o el catálogo no contiene una tarifa aplicable, el registro se marca explícitamente como `total_cost = None` (`is_unknown = True`).
- Se distingue claramente `ZERO_COST` (`total_cost = Decimal('0.00')`) de `UNKNOWN_COST` (`total_cost = None`).
- Los resúmenes agregados (`CostSummary`) contabilizan de forma separada `known_total`, `known_record_count` y `unknown_record_count`, impidiendo asumir falsamente que costos no identificados fueron gratuitos.

---

## 9. MULTI-CURRENCY SEGREGATION

- `CostSummary` no suma montos de diferentes monedas en un escalar único ni realiza conversiones FX opacas.
- Los totales se agrupan en un diccionario `by_currency: Dict[str, CurrencyCostSummary]`, asegurando total integridad contable.

---

## 10. PRICING CATALOG & VERSIONING

- Contrato `PricingCatalogPort` con lookup determinista por proveedor, modelo/servicio y fecha de ocurrencia.
- La versión de la tarifa y la fuente quedan auditadas permanentemente en el `CostRecord`.
- Los registros históricos no se recalculan si las tarifas vigentes cambian posteriormente.

---

## 11. PERSISTENCE & IDEMPOTENCY

- `CostRepositoryPort` implementado mediante `JsonCostRepository`.
- Persistencia atómica (`.tmp` + `os.replace` + `os.fsync`) en directorio configurable.
- Índice maestro `.jsonl` para escaneos y agregaciones eficientes.
- Control de integridad con hash SHA-256 determinista sobre el contenido canónico.
- Idempotencia garantizada por clave compuesta (`execution_id:trace_id:cost_type:provider:service`) y deduplicación en memoria/disco ante replays.

---

## 12. SECURITY & FAILURE ISOLATION

- **Sanitización recursiva:** Eliminación y ofuscación de API keys, contraseñas, tokens JWT, headers `Authorization`, PANs bancarios y reasoning privado de LLMs.
- **Aislamiento de fallos (`isolate_failures=True`):** Cualquier excepción en el subsistema de cost tracking queda encapsulada sin interrumpir la ejecución de misiones de negocio.

---

## 13. TEST SUITE & VERIFICACIÓN

### Pruebas Unitarias (`tests/unit/test_k3_cost_tracking_unit.py`)
29 pruebas cubriendo exhaustivamente todos los requerimientos A–AC:
- **A:** Immutable CostRecord
- **B:** Inference usage normalization
- **C:** Input tokens recording
- **D:** Output tokens recording
- **E:** Token cost calculation
- **F:** Request cost calculation
- **G:** Exact Decimal precision
- **H:** Pricing lookup
- **I:** Pricing versioning
- **J:** Effective date pricing lookup
- **K:** UNKNOWN usage handling
- **L:** UNKNOWN pricing handling
- **M:** Zero cost vs Unknown cost distinction
- **N:** Explicit currency preservation
- **O:** Multi-currency separation
- **P:** Mission-level aggregation
- **Q:** Execution-level aggregation
- **R:** Cycle-level aggregation
- **S:** Provider/model breakdown aggregation
- **T:** Known cost total computation
- **U:** Unknown record counting
- **V:** Idempotency verification
- **W:** Replay safety
- **X:** Durable persistence
- **Y:** Restart and reload consistency
- **Z:** AgentTrace correlation linkage
- **AA:** AuditTrail event linkage
- **AB:** Security sanitization of sensitive metadata
- **AC:** Passive observation verification (no budget/routing modification)

### Pruebas de Integración y E2E (`tests/integration/test_k3_cost_tracking_integration.py`)
5 escenarios de integración profunda:
1. `test_k3_full_mission_trace_cost_pipeline`: Flujo continuo Misión → Traza → Inferencia → Persistencia → Agregación.
2. `test_k3_mixed_known_and_unknown_costs_e2e`: Manejo simultáneo de operaciones con y sin tarifa conocida.
3. `test_k3_durability_and_restart_reload_e2e`: Destrucción y recreación de repositorios confirmando estabilidad idéntica de totales.
4. `test_k3_replay_and_idempotency_e2e`: Ingesta repetida de ejecuciones sin duplicación de costes contables.
5. `test_k3_multicurrency_segregation_e2e`: Operaciones combinadas en USD y CLP sin mezcla errónea.

### Regresión Completa
- **Comando:** `python -m pytest`
- **Resultado:** **1026 passed, 1 skipped, 0 failures** en 33.18s.
- **Regresiones introducidas:** 0.

---

## 14. ARCHITECTURE AUDIT CHECKLIST

- [x] K.3 separado e independiente del dominio central de Agent Trace.
- [x] K.3 enlaza Agent Trace por `execution_id`, `trace_id`, `mission_id`.
- [x] K.3 enlaza Audit Trail mediante evento `COST_RECORDED`.
- [x] Captura de usage real/observable sin estimaciones opacas.
- [x] Semántica estricta `UNKNOWN != 0.00`.
- [x] Aritmética de dinero 100% en `Decimal`.
- [x] Catálogo de precios desacoplado (`PricingCatalogPort`).
- [x] Precios versionados con vigencia temporal.
- [x] Estabilidad de costos históricos tras reload.
- [x] Agregaciones por misión, ejecución, ciclo y proveedor/modelo.
- [x] Moneda explícita y agregación multi-moneda segura.
- [x] Idempotencia y seguridad ante replays.
- [x] Persistencia atómica con sincronización a disco (`fsync`).
- [x] Sanitización recursiva de datos sensibles.
- [x] Aislamiento de fallos de medición.
- [x] Cero ruteo de modelos, caching o limitación presupuestaria (Hito M intacto).
- [x] K.4–K.8 y Gate J no implementados.

---

## 15. ARCHIVOS CREADOS / MODIFICADOS

### Archivos Creados:
- `src/domain/cost/__init__.py`
- `src/domain/cost/models.py`
- `src/domain/cost/ports.py`
- `src/application/cost/__init__.py`
- `src/application/cost/pricing_catalog.py`
- `src/application/cost/cost_tracking_service.py`
- `src/infrastructure/persistence/data/json/cost_repository.py`
- `tests/unit/test_k3_cost_tracking_unit.py`
- `tests/integration/test_k3_cost_tracking_integration.py`
- `K3_COST_TRACKING_EXECUTION_REPORT.md`

### Archivos Modificados:
- `docs/market-intelligence/AI_AUTONOMOUS_COMMERCE_GANTT_MAESTRA.md`

---

## 16. DECISIÓN FINAL & NEXT TASK

- **Decisión:** **K.3 Cost Tracking queda marcada como 🟢 VALIDADA**.
- **Siguiente Tarea:** **K.4 — Evaluation Harness** (a implementar en el siguiente prompt conforme a la Carta Gantt Maestra).
