# REPORTE DE EJECUCIÓN: M.2 — CONTEXT BUDGETING

**Fecha de Ejecución:** 2026-09-03  
**Transversal:** M — Control de Coste e Inferencia  
**Hito:** M.2 — Context Budgeting  
**Estado:** 🟢 VALIDADA  
**Baseline previo:** 1432 passed, 1 skipped (0 failures, 0 errors)  
**Resultado actual:** 1453 passed, 1 skipped (0 failures, 0 errors)  
**Commits:** NO commit / NO push realizado (según instrucciones)  

---

## 1. RESUMEN EJECUTIVO

Se ha diseñado, implementado y validado exitosamente el hito **M.2 — Context Budgeting**, componente del **Transversal M — Control de Coste e Inferencia**.

M.2 responde formalmente a la pregunta de dominio:
> *“¿Cuánto contexto puede consumir esta inferencia y cómo evitamos exceder ese presupuesto?”*

La implementación proporciona una evaluación determinista, estructurada y reproducible de presupuestos de contexto (`ContextBudgetService`, `DeterministicTokenEstimator`), apoyada en modelos de dominio inmutables y congelados (`ContextBudgetRequest`, `ContextBudgetDecision`, `ContextBudgetPolicy`, `InputTokensBreakdown`), plenamente integrada con la estrategia de enrutamiento **M.1** (`ModelRoute`, `RoutingDecision`).

---

## 2. MATRIZ DE REUTILIZACIÓN Y DESCUBRIMIENTO (Discovery Matrix)

| CAPABILITY | LOCATION | REUSE / EXTEND / CREATE |
|---|---|---|
| Model Route & Routing Decision | `src/domain/model_routing/` | **EXTEND** (se añadió `context_window: Optional[int]` inmutable y validado a `ModelRoute`). |
| Inferencia & Provider Gateway | `src/infrastructure/llm/omniroute_decision_provider.py` | **REUSE** (probado en boundary e inferencia determinista). |
| Secret Sanitization & Deep Freeze | `src/domain/model_routing/models.py` | **REUSE** (`sanitize_routing_data`, `deep_freeze`). |
| Token Accounting & Breakdowns | `src/domain/context_budget/models.py` | **CREATE** (`InputTokensBreakdown`, `ContextBudgetRequest`, `ContextBudgetDecision`, `ContextBudgetPolicy`). |
| Deterministic Token Estimation | `src/application/context_budget/token_estimator.py` | **CREATE** (`DeterministicTokenEstimator` implementando `TokenEstimatorPort`). |
| Budget Evaluation Service | `src/application/context_budget/context_budget_service.py` | **CREATE** (`ContextBudgetService` implementando `ContextBudgetServicePort`). |

---

## 3. ARQUITECTURA Y COMPONENTES IMPLEMENTADOS

### 3.1 Dominio Inmutable (`src/domain/context_budget/`)
- **`models.py`**:
  - `ContextBudgetStatus`: Estados formales `WITHIN_BUDGET`, `OVER_BUDGET`, `UNKNOWN`, `ERROR`.
  - `BudgetExclusionReason`: Códigos estructurados de desborde/fallo `INPUT_TOO_LARGE`, `OUTPUT_RESERVATION_EXCEEDED`, `MODEL_CONTEXT_UNKNOWN`, `TOKEN_ESTIMATE_UNKNOWN`, `INVALID_PARAMETERS`, `SAFETY_MARGIN_EXCEEDED`.
  - `InputTokensBreakdown`: Desglose inmutable (`frozen=True`) de tokens de entrada (`system_instructions`, `user_input`, `memory_context`, `tool_schemas`, `retrieved_evidence`, `conversation_history`, `other`).
  - `ContextBudgetPolicy`: Política declarativa versionada con `default_reserved_output_tokens` y `safety_margin_tokens`.
  - `ContextBudgetRequest`: Solicitud inmutable de evaluación de presupuesto por `ModelRoute`, `RoutingDecision` o `route_id`.
  - `ContextBudgetDecision`: Decisión inmutable con campos enteros, razones estructuradas, rationale y cálculo de checksum SHA-256 canónico.
- **`ports.py`**:
  - `TokenEstimatorPort`: Puerto para estimación determinista y desacoplada de SDKs externos.
  - `ContextBudgetServicePort`: Puerto primario de evaluación presupuestaria.

### 3.2 Aplicación (`src/application/context_budget/`)
- **`token_estimator.py` (`DeterministicTokenEstimator`)**:
  - Estimación determinista basada en heurística canónica estandarizada (caracteres a tokens con piso entero no negativo).
  - Cálculo de desglose estructurado sin acoplamiento a SDKs.
- **`context_budget_service.py` (`ContextBudgetService`)**:
  - Resolución de ruta (`ModelRoute` directa, `RoutingDecision` M.1 o búsqueda en registro).
  - Aritmética canónica en enteros (`int`, cero floats):
    $$\text{available\_input} = \text{context\_window} - \text{reserved\_output} - \text{safety\_margin}$$
  - Evaluación rigurosa:
    - $\text{requested\_input} \le \text{available\_input} \implies \text{WITHIN\_BUDGET}$
    - $\text{requested\_input} > \text{available\_input} \implies \text{OVER\_BUDGET}$
  - Preservación estricta de incertidumbre: `context_window` desconocido o conteo desconocido produce `UNKNOWN` (`UNKNOWN != safe`).
  - Protección absoluta de la reserva de salida (`reserved_output_tokens`) y del margen de seguridad (`safety_margin_tokens`).

---

## 4. AUDITORÍA DE ARQUITECTURA (Architecture Audit)

1. **¿El context window está duplicado/hardcodeado?**  
   *No. Proviene canónicamente de `ModelRoute.context_window` o la configuración del registro.*
2. **¿M.2 modifica prompts?**  
   *No. M.2 únicamente mide y evalúa el presupuesto. No altera cadenas de texto ni prompt templates.*
3. **¿OVER_BUDGET puede continuar silenciosamente?**  
   *No. Devuelve `is_within_budget = False` con razón estructurada (`INPUT_TOO_LARGE` / `OUTPUT_RESERVATION_EXCEEDED`), permitiendo una parada explícita o hand-off aguas arriba.*
4. **¿UNKNOWN puede convertirse en safe?**  
   *No. Cualquier contexto o estimación `UNKNOWN` devuelve estado `UNKNOWN` con `is_within_budget = False`.*
5. **¿La reserva de salida (output reservation) puede quedar en 0 accidentalmente?**  
   *No. Se aplican defaults versionados de política (`default_reserved_output_tokens = 1024`, `safety_margin_tokens = 256`) a menos que se especifique explícitamente un valor mayor o igual a 0.*
6. **¿Se reutilizó M.1?**  
   *Sí. `ContextBudgetService` acepta directamente `RoutingDecision` de M.1, extrayendo la ruta seleccionada de forma transparente.*
7. **¿Se implementó M.3–M.6 accidentalmente?**  
   *No. No se implementó compresión de prompts (M.3), caché semántica (M.4), selector económico complejo (M.5) ni política de costes (M.6).*
8. **¿Se registran prompts/secretos innecesariamente?**  
   *No. Los metadatos son sanitizados vía `sanitize_routing_data` y no se persiste Chain-of-Thought ni claves de autorización.*

---

## 5. VALIDACIÓN Y SUITE DE PRUEBAS

### 5.1 Pruebas Unitarias (`tests/unit/test_m2_context_budgeting_unit.py`)
14 tests unitarios cubriendo el 100% de los requerimientos:
- `test_1_within_budget`: Evaluación exitosa dentro de presupuesto.
- `test_2_exact_boundary`: Caso límite exacto (`requested_input == available_input`).
- `test_3_over_budget`: Desborde de presupuesto devuelve `OVER_BUDGET` e `INPUT_TOO_LARGE`.
- `test_4_output_reservation_protection`: Reserva de salida protegida ante ventanas pequeñas.
- `test_5_safety_margin_protection`: Margen de seguridad reduce `available_input` adecuadamente.
- `test_6_negative_and_invalid_values_rejected`: Rechazo estricto de floats y enteros negativos.
- `test_7_unknown_context_window`: Ventana desconocida produce `UNKNOWN` (no safe).
- `test_8_unknown_token_estimate`: Conteo desconocido produce `UNKNOWN`.
- `test_9_deterministic_estimation_and_breakdown`: Estimador determinista y desglose de tokens.
- `test_10_m1_route_integration`: Integración con `ModelRoute`, `RoutingDecision` y registro M.1.
- `test_11_policy_versioning`: Políticas versionadas y verificación de checksum SHA-256.
- `test_12_no_truncation`: Desborde preserva los datos del breakdown sin truncamiento.
- `test_13_no_compression`: Ausencia de métodos de compresión o alteración de prompts.
- `test_14_no_m3_m6_logic`: Ausencia de caching, selección económica y políticas M.3-M.6.

### 5.2 Pruebas de Integración y E2E (`tests/integration/test_m2_context_budgeting_integration.py`)
7 escenarios de integración y flujo End-to-End:
- `test_scenario_a_routing_decision_to_budget_within`: M.1 RoutingDecision -> Context window -> M.2 within budget.
- `test_scenario_b_large_context_over_budget`: Contexto grande desborda presupuesto deterministamente.
- `test_scenario_c_and_d_same_request_different_capacity_routes`: Misma petición da `OVER_BUDGET` en ruta pequeña y `WITHIN_BUDGET` en ruta grande.
- `test_scenario_e_unknown_route_capacity`: Capacidad desconocida genera estado `UNKNOWN`.
- `test_scenario_f_reserved_output_always_protected`: Capacidad de salida no se reduce para acomodar input.
- `test_scenario_g_decision_handed_to_inference_boundary`: Decisión evaluada antes de pasar al gateway OpenAI-compatible.
- `test_e2e_mission_input_to_routing_to_budget_to_inference_boundary`: Flujo E2E completo (Mission -> M.1 -> M.2 -> Boundary) con ramas WITHIN_BUDGET (procede) y OVER_BUDGET (parada explícita sin compresión).

### 5.3 Resultados de Ejecución
- **M.2 Unit:** 14 passed in 0.35s
- **M.2 Integration:** 7 passed in 0.35s
- **M.1 Regression:** 22 passed in 0.38s
- **Full Test Suite:** 1453 passed, 1 skipped, 0 failures, 0 errors in 63.48s

---

## 6. ESTADO DE HITOS Y ROADMAP

- **M.1 Model Routing Strategy** → 🟢 VALIDADA
- **M.2 Context Budgeting** → 🟢 VALIDADA
- **M.3 Prompt Compression** → ⚪ PENDIENTE
- **M.4 Caching** → ⚪ PENDIENTE
- **M.5 Model Selection by Task** → ⚪ PENDIENTE
- **M.6 Cost-aware Decision Policy** → ⚪ PENDIENTE
- **Gate L** → ⚪ PENDIENTE
- **Hito M** → 🟡 EN PROGRESO

---

## 7. PRÓXIMA TAREA (Next Task)

**M.3 — Prompt Compression**  
*(No implementada en este ciclo, reservada para la siguiente fase según instrucciones).*
