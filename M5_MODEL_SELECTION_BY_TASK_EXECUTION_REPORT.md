# INFORME DE EJECUCIÓN: M.5 — MODEL SELECTION BY TASK

**Fecha:** 2026-09-03  
**Transversal:** M — Control de Coste e Inferencia  
**Hito:** M.5 — Model Selection by Task  
**Estado:** 🟢 VALIDADA  
**Baseline de Regresión:** 1514 passed, 1 skipped, 0 failures (Baseline inicial: 1491 passed, 1 skipped)  
**Acciones Git:** NO commit, NO push  

---

## 1. OBJETIVO Y RESPONSABILIDAD DE M.5

El Hito **M.5 Model Selection by Task** responde de manera centralizada, determinista y auditable a la pregunta arquitectónica:
> *"Dado este tipo de tarea y contexto de negocio, ¿qué requisitos técnicos, de capacidad, calidad y criticidad de modelo necesita antes de ejecutar el routing?"*

### Transformación de Flujo:
```
Task / TaskType / MissionType
  ↓
M.5: TaskModelProfile / TaskSelectionRequirements
  ↓
M.1: RoutingRequest
  ↓
M.1: DeterministicModelRoutingStrategy
  ↓
RoutingDecision (Selected Route / Reason Codes)
```

M.5 **NO reemplaza a M.1**, sino que actúa como capa superior de especificación declarativa de requerimientos técnicos de la tarea, delegando a M.1 la selección concreta de la ruta óptima disponible.

---

## 2. TAXONOMÍA REAL Y MATRIZ DE REQUERIMIENTOS

Se implementó la taxonomía sobre tareas y misiones reales del codebase sin inventar catálogos ficticios:

| TASK TYPE | COMPLEJIDAD | CRITICIDAD | CAPACIDADES REQUERIDAS | CALIDAD MÍNIMA | LATENCIA |
|---|---|---|---|---|---|
| `MARKET_DISCOVERY` | `MEDIUM` | `HIGH` | `STRUCTURED_OUTPUT` | `HIGH` | `NORMAL` |
| `MARKET_ANALYSIS` | `MEDIUM` | `HIGH` | `STRUCTURED_OUTPUT` | `HIGH` | `NORMAL` |
| `EXTRACTION` | `LOW` | `MEDIUM` | `STRUCTURED_OUTPUT`, `JSON_MODE` | `STANDARD` | `LOW_LATENCY` |
| `CLASSIFICATION` | `LOW` | `LOW` | `STRUCTURED_OUTPUT` | `STANDARD` | `LOW_LATENCY` |
| `SUPPLIER_SEARCH` | `MEDIUM` | `HIGH` | `STRUCTURED_OUTPUT`, `TOOL_USE` | `HIGH` | `NORMAL` |
| `SUPPLIER_DISCOVERY`| `MEDIUM` | `HIGH` | `STRUCTURED_OUTPUT`, `TOOL_USE` | `HIGH` | `NORMAL` |
| `PROFIT_EVALUATION` | `HIGH` | `CRITICAL` | `STRUCTURED_OUTPUT`, `REASONING` | `SUPERIOR` | `NORMAL` |
| `CAPITAL_ALLOCATION`| `HIGH` | `CRITICAL` | `STRUCTURED_OUTPUT`, `REASONING` | `SUPERIOR` | `NORMAL` |
| `COMMERCIAL_REASONING`| `HIGH` | `CRITICAL` | `STRUCTURED_OUTPUT`, `REASONING` | `SUPERIOR` | `NORMAL` |
| `COMMERCIAL_PUBLICATION`| `HIGH` | `CRITICAL` | `STRUCTURED_OUTPUT`, `TOOL_USE` | `HIGH` | `NORMAL` |
| `LISTING_GENERATION`| `MEDIUM` | `MEDIUM` | `STRUCTURED_OUTPUT` | `STANDARD` | `NORMAL` |
| `POLICY_SENSITIVE_DECISION`| `HIGH` | `CRITICAL` | `STRUCTURED_OUTPUT`, `REASONING` | `SUPERIOR` | `NORMAL` |
| `POLICY_EVALUATION` | `HIGH` | `CRITICAL` | `STRUCTURED_OUTPUT`, `REASONING` | `SUPERIOR` | `NORMAL` |
| `TOOL_EXECUTION_PLANNING`| `HIGH` | `HIGH` | `TOOL_USE`, `STRUCTURED_OUTPUT` | `HIGH` | `LOW_LATENCY` |
| `VISION_ANALYSIS` | `MEDIUM` | `MEDIUM` | `VISION`, `STRUCTURED_OUTPUT` | `HIGH` | `NORMAL` |
| `UNKNOWN` | `UNKNOWN` | `LOW` | Ninguna | `MINIMAL` | `NORMAL` |

---

## 3. ARTEFACTOS IMPLEMENTADOS

### Dominio (`src/domain/model_selection/`):
- `TaskComplexity` (`LOW`, `MEDIUM`, `HIGH`, `UNKNOWN`): Semántica explícita sin scores opacos.
- `SelectionStatus` (`SUCCESS`, `UNKNOWN_TASK`, `NO_PROFILE`, `ROUTING_FAILED`, `ERROR`).
- `TaskModelProfile`: Entidad inmutable (`frozen=True`) que encapsula la especificación declarativa de requerimientos por tarea.
- `TaskSelectionPolicy`: Política versionada (`policy_id`, `policy_version`, perfiles de tarea, fallback explícito y flags de escalado).
- `TaskSelectionRequest`: Petición tipada e inmutable de selección de modelo por tarea.
- `TaskSelectionRequirements`: Requerimientos resueltos con método canónico `.to_m1_routing_request()` para delegación sin acoplamiento invasivo.
- `ModelSelectionResult`: Resultado determinista y estructurado con trazabilidad completa (perfil, requerimientos, decisión M.1, checksum SHA-256, reason codes y timestamp UTC).
- `ports.py`: Contratos abstractos `TaskSelectionPolicyPort` y `ModelSelectionByTaskServicePort`.

### Aplicación (`src/application/model_selection/`):
- `ModelSelectionByTaskService`: Orquestador que resuelve requerimientos según la política activa y delega deterministamente a `DeterministicModelRoutingStrategy` (M.1).
- `DefaultTaskSelectionPolicyProvider`: Proveedor de políticas de selección en memoria extensible.
- `create_default_task_selection_policy()`: Fábrica canónica de políticas para todas las tareas estándar del sistema.

---

## 4. INTEGRACIÓN Y COMPATIBILIDAD CON EL PIPELINE TRANSVERSAL M

Se validó formalmente el pipeline completo de inferencia:
```
[Task: MARKET_DISCOVERY]
        ↓
1. M.5 Model Selection by Task (resuelve perfil: HIGH quality, STRUCTURED_OUTPUT)
        ↓
2. M.1 Model Routing (evalúa rutas candidatas disponibles y selecciona gpt-4o / claude-3-7-sonnet)
        ↓
3. M.2 Context Budgeting (evalúa tokens solicitados vs context window, reserva de output y margen)
        ↓
4. M.3 Prompt Compression (en caso de desborde, aplica compresión determinista sin tocar datos protegidos)
        ↓
5. M.4 Inference Caching (evalúa huella SHA-256 canónica: MISS inicial -> Inferencia -> HIT posterior)
        ↓
[Inference Boundary / Provider Execution]
```

---

## 5. AUDITORÍA DE SEGURIDAD Y FRONTERAS

1. **¿M.5 duplica M.1?**  
   *NO*. M.5 resuelve la pregunta *"¿Qué necesita la tarea?"*, construyendo un `RoutingRequest` y delegando a `ModelRoutingStrategyPort` de M.1.
2. **¿La taxonomía de tareas corresponde a tareas reales?**  
   *SÍ*. Alineada con los `MissionType` y módulos del dominio comercial (`MARKET_DISCOVERY`, `PROFIT_EVALUATION`, `CAPITAL_ALLOCATION`, `COMMERCIAL_PUBLICATION`, etc.).
3. **¿Las reglas de selección están centralizadas?**  
   *SÍ*. Centralizadas en `TaskSelectionPolicy` versionada y auditable.
4. **¿Una tarea UNKNOWN obtiene modelo por default silenciosamente?**  
   *NO*. Tareas no reconocidas retornan `UNKNOWN_TASK` o `NO_PROFILE` preservando la incertidumbre sin inventar rutas default.
5. **¿Un capability mismatch puede ganar la selección?**  
   *NO*. El filtrado de capacidades en M.1 excluye estrictamente rutas incompatibles.
6. **¿Se respeta la criticidad?**  
   *SÍ*. Tareas críticas exigen perfiles de calidad `SUPERIOR` o `HIGH` y activan escalado si se declara en la solicitud.
7. **¿Quality y Latency son explícitas?**  
   *SÍ*. Se reutilizan directamente las enumeraciones `QualityRequirement` y `LatencyRequirement` de M.1.
8. **¿Se implementó M.6 accidentalmente?**  
   *NO*. M.5 solo transporta el techo de coste si el perfil lo especifica, sin optimización económica invasiva ("cheapest always wins").
9. **¿La selección es determinista?**  
   *SÍ*. Misma tarea + misma política + mismas rutas = idéntico resultado con checksum verificable.
10. **¿Pueden persistirse secretos o CoT?**  
    *NO*. Sanitización recursiva vía `sanitize_routing_data` y exclusión de cadenas de razonamiento (CoT).

---

## 6. SUITE DE PRUEBAS Y VERIFICACIÓN

### Tests Unitarios: `tests/unit/test_m5_model_selection_by_task_unit.py` (15/15 PASSED)
- `test_1_known_task_profile_resolution`
- `test_2_unknown_task_handling`
- `test_3_low_complexity_task`
- `test_4_high_complexity_task`
- `test_5_critical_task_escalation`
- `test_6_required_tool_use_capability`
- `test_7_required_structured_output_capability`
- `test_8_required_vision_capability`
- `test_9_quality_requirement_enforcement`
- `test_10_latency_requirement_enforcement`
- `test_11_incapable_route_excluded_via_m1`
- `test_12_deterministic_result`
- `test_13_policy_versioning`
- `test_14_no_m6_economic_optimization`
- `test_15_security_sanitization_and_no_cot`

### Tests de Integración: `tests/integration/test_m5_model_selection_by_task_integration.py` (8/8 PASSED)
- `test_scenario_a_simple_structured_extraction`
- `test_scenario_b_high_complexity_reasoning_task`
- `test_scenario_c_tool_use_requirement_excludes_non_tool_routes`
- `test_scenario_d_high_criticality_commercial_task`
- `test_scenario_e_unknown_task_explicit_status`
- `test_scenario_f_preferred_route_unavailable_fallback`
- `test_scenario_g_pipeline_m5_to_m1_to_m2_to_m3_to_m4`
- `test_e2e_mission_orchestration_flow`

### Regresión Global del Proyecto:
- **1514 passed, 1 skipped, 0 failures** en 58.71s.

---

## 7. PRÓXIMO PASO
- **M.6 — Cost-aware Decision Policy** (Pendiente para el siguiente hito, sin tocar en la presente ejecución).
