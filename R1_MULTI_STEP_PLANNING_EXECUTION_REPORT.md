# R.1 — Multi-step Planning Execution Report
## Advanced Autonomy / Hierarchical Goal Decomposition, DAG Execution Plans & Safe Replanning

### 1. Roadmap Alignment & Executive Summary
El objetivo de **R.1 — Multi-step Planning** dentro del **Hito R — Advanced Autonomy** es responder de forma formal, determinista, acotada y tenant-safe a la pregunta arquitectural fundamental:
> *"¿Puede el agente transformar un objetivo complejo en un plan multi-paso explícito, válido, acíclico y ejecutable, respetando dependencias, budgets y políticas, y replanificar de forma segura cuando cambian las condiciones?"*

R.1 implementa el motor de planificación autónoma multi-paso desacoplado de la ejecución (`PLANNER != EXECUTOR`). El planificador se responsabiliza de la descomposición jerárquica de objetivos en sub-objetivos y pasos atómicos ejecutables, modelado y validación de grafos acíclicos dirigidos (DAG), ordenación topológica determinista, cálculo estricto de readiness de pasos, verificación de capacidades registradas, asignación de presupuestos respetando incertidumbres (`UNKNOWN`), replanificación acotada de subgrafos afectados por fallos técnicos preservando pasos completados, y prohibición estricta de evasión de políticas (`Anti-Policy Bypass`), todo ello integrado con observabilidad segura (Auditoría K.1 y Trazabilidad K.2) sin Chain-of-Thought (Anti-CoT).

---

### 2. Architecture & Design Principles (REUSE > EXTEND > CREATE)
- **Planner != Executor:** El planificador (`MultiStepPlanningService`) no ejecuta herramientas de bajo nivel ni sustituye a `ActionExecutor` o al bucle cognitivo autónomo. R.1 modela y versiona *qué* pasos y dependencias existen; el runtime autónomo existente decide *cómo* invocar las acciones autorizadas.
- **Aislamiento Multi-Tenant (O.1):** Todos los planes y pasos están estrictamente encapsulados por `tenant_id`. La capa de repositorio valida el contexto mediante `TenantContext` y `CrossTenantGuard`.
- **Gobernanza y Autorización (N.3 / N.4 / N.11):** Un plan generado no constituye una autorización de ejecución (`Plan != Permission`). Ante denegación por política (`POLICY_DENIED`) o activación de parada de emergencia (`Emergency Stop N.11`), el plan queda bloqueado (`BLOCKED`), impidiendo que el motor intente replanes evasivos.
- **Observabilidad Segura (K.1 / K.2 / N.9):** Emisión de eventos canónicos (`PLAN_CREATED`, `PLAN_VALIDATED`, `PLAN_REVISED`, `STEP_READY`, `STEP_BLOCKED`) sin incluir tokens de razonamiento interno (`Chain-of-Thought`), contraseñas ni PII.

---

### 3. Canonical Plan Model & Taxonomy

#### Modelos de Dominio (`src/domain/planning/models.py`)
- `ExecutionPlan`: Agregado inmutable que contiene `plan_id`, `mission_id`, `tenant_id`, `goal`, tupla de `PlanStep`, tupla de `StepDependency`, `version`, `status` (`PlanStatus`), `budget` (`PlanBudget`), `history` (`PlanVersionHistory`), `replan_count` y metadatos.
- `PlanStep`: Paso ejecutable o nodo intermedio con `step_id`, `objective`, `capability_name`, `action_type`, `assigned_agent`, `status` (`StepStatus`), `dependencies`, inputs requeridos, outputs esperados, `estimated_cost` y `is_cost_unknown`.
- `StepDependency`: Arista dirigida en el grafo que define precedencia entre `from_step_id` y `to_step_id` con `DependencyType.COMPLETION`.
- `PlanBudget`: Presupuesto con límites de costo monetario (`Decimal`), tokens, pasos máximos (`max_steps`) y replanes permitidos (`max_replans`).
- `PlanVersionHistory`: Registro inmutable de evolución de versiones con `version`, `reason`, `changed_step_ids`, `preserved_step_ids` y `timestamp`.
- `StepRationale`: Justificación estructurada y segura (sin CoT) del paso, dependencias y capacidad asignada.

#### Taxonomía de Fallos (`StepFailureType`)
1. `EXECUTION_FAILURE`: Error técnico o transitorio en la ejecución del paso (reintentable / replanificable).
2. `DEPENDENCY_FAILURE`: Falla en un paso predecesor requerido.
3. `CAPABILITY_UNAVAILABLE`: La capacidad o herramienta requerida no está registrada en el sistema.
4. `POLICY_DENIED`: Bloqueo por gobernanza, seguridad o reglas de autorización (NO replanificable).
5. `BUDGET_EXHAUSTED`: Exceso o agotamiento del presupuesto asignado.
6. `INVALID_INPUT`: Entradas faltantes o no conformes con el esquema requerido.
7. `UNKNOWN`: Causa de fallo indeterminada.

---

### 4. Mathematical Precision, DAG & UNKNOWN Semantics

1. **Detección Determinista de Ciclos (Algoritmo de Kahn):** `PlanValidator.validate_dag` computa in-degrees de cada nodo y reduce iterativamente el grafo. Si quedan nodos con in-degrees > 0, el plan es rechazado inmediatamente por ciclo (`DAG_CYCLE_DETECTED`).
2. **Ordenación Topológica Determinista:** Tie-breaking determinista alfabético (`sorted(queue)`) garantizando orden de ejecución reproducible entre nodos independientes.
3. **Cálculo Estricto de Readiness:** Un paso solo transiciona a `READY` si el 100% de sus dependencias directas están en estado `StepStatus.COMPLETED`. Si una dependencia falla o está pendiente, el paso no puede ser marcado como listo falsamente.
4. **Preservación Honesta de UNKNOWN != 0:** Si `is_cost_unknown=True`, el costo no se asume como $0.00 ni gratis. Se valida que los costos conocidos no excedan el presupuesto y se preserva el flag `is_cost_unknown` a nivel plan.
5. **Replanificación Acotada e Inmutable:**
   - Se reemplaza exclusivamente el subgrafo dependiente del paso fallido.
   - Los pasos previamente `COMPLETED` son inmutables y sus evidencias/outputs se preservan intactos.
   - El contador de versiones se incrementa (`version += 1`) y si `replan_count >= max_replans`, el plan transiciona a `FAILED`.

---

### 5. Services & Ports Implemented

- `src/domain/planning/ports.py`:
  - `ExecutionPlanRepositoryPort`: Puerto hexagonal para persistencia de planes con aislamiento multi-tenant.
  - `CapabilityRegistryPort`: Puerto para consulta y validación de capacidades registradas.
  - `MultiStepPlanningServicePort`: Caso de uso de descomposición, validación, readiness, completitud y replanificación.
- `src/domain/planning/validator.py`:
  - `PlanValidator`: Validador puro y determinista de estructura, DAG, capacidades, presupuestos y cálculo de readiness.
- `src/application/planning/multi_step_planning_service.py`:
  - `MultiStepPlanningService`: Servicio orquestador de planificación multi-paso, integración con Emergency Stop, emisión de trazas K.1/K.2 y control anti-bypass de políticas.
- `src/infrastructure/persistence/data/json/execution_plan_repository.py`:
  - `JsonExecutionPlanRepository` e `InMemoryExecutionPlanRepository`: Implementaciones adaptadoras con soporte multi-tenant y `CrossTenantGuard`.

---

### 6. Test Suite & Validation Results

#### Unit Tests (`tests/unit/test_r1_multi_step_planning_unit.py`) — 20/20 PASSED
1. `test_simple_decomposition`: Descomposición de objetivo simple en pasos hoja ejecutables.
2. `test_hierarchical_decomposition`: Descomposición en sub-objetivos y pasos atómicos con mapeo jerárquico.
3. `test_valid_dag`: Aceptación de DAG válido acíclico.
4. `test_cycle_rejected`: Rechazo estricto de grafos con dependencia circular directa o indirecta.
5. `test_missing_dependency_rejected`: Rechazo de dependencias hacia pasos inexistentes.
6. `test_deterministic_topological_order`: Ordenación topológica determinista con tie-breaking alfabético.
7. `test_readiness_requires_dependencies`: Un paso no está READY hasta que sus dependencias son COMPLETED.
8. `test_independent_steps_parallel_ready`: Pasos sin dependencias mutuas quedan READY en paralelo.
9. `test_capability_unavailable`: Rechazo de plan con capacidad no registrada.
10. `test_budget_allocation`: Asignación válida de presupuestos monetarios y de pasos.
11. `test_budget_overflow_rejected`: Rechazo de plan cuya suma de costos excede el presupuesto límite.
12. `test_unknown_cost_preserved`: Preservación estricta de incertidumbre `UNKNOWN` en costos sin forzar 0.
13. `test_replan_after_technical_failure`: Replanificación de subgrafo ante fallo técnico con nueva versión de plan.
14. `test_completed_step_preserved_on_replan`: Inmutabilidad de pasos `COMPLETED` durante replan.
15. `test_policy_denial_not_bypassed`: Prohibición de replan evasivo ante `POLICY_DENIED` (plan queda BLOCKED).
16. `test_max_replan_bound`: Bloqueo o fallo del plan al alcanzar el límite máximo de replanes (`max_replans`).
17. `test_plan_version_increment`: Incremento estricto del número de versión y registro en historial.
18. `test_tenant_scoping`: Aislamiento multi-tenant estricto con `CrossTenantGuard`.
19. `test_anti_cot_projection`: Justificación estructurada sin filtración de Chain-of-Thought.
20. `test_no_r2_plus_references`: Verificación estricta de no importación ni acoplamiento con R.2+.

#### Integration Tests (`tests/integration/test_r1_multi_step_planning_integration.py`) — 10/10 PASSED
- **Escenario A:** Descomposición de objetivo complejo y validación de DAG con orden topológico.
- **Escenario B:** Cadena secuencial de dependencias y progresión ordenada de readiness.
- **Escenario C:** Ramas paralelas independientes (fork-join) listas para despacho simultáneo.
- **Escenario D:** Fallo de paso con replanificación acotada de subgrafo y preservación de pasos completados.
- **Escenario E:** Detección de agotamiento de presupuesto bloqueando la ejecución de pasos restantes.
- **Escenario F:** Intento de evasión de política denegada (`POLICY_DENIED`) bloqueado sin replan evasivo.
- **Escenario G:** Parada de Emergencia activa (`N.11`) bloqueando el despacho de pasos afectados.
- **Escenario H:** Aislamiento de datos y operaciones entre Tenant A y Tenant B.
- **Escenario I:** Consumo del plan por el runtime/executor existente sin duplicación de motores.
- **Escenario J:** Emisión completa de eventos de auditoría K.1 y trazas de agente K.2.

#### Global Regression Results
- **Pytest Full Suite:** **2616 passed, 18 skipped, 0 failures** (Baseline: 2602 passed; 30 tests nuevos de R.1 incorporados exitosamente sin regresiones).
- **Deployment Automation Suite (`python scripts/deploy_validate.py`):** 5/5 checks PASSED.
- **Git Hygiene:** Sin archivos de volcado temporal, sin secretos ni modificaciones a R.2+.

---

### 7. Gantt & Roadmap Update

- **Hito Q + Gate P:** 🟢 CERRADOS
- **Hito R — Advanced Autonomy:** 🟡 EN PROGRESO
- **Task R.1 — Multi-step Planning:** 🟢 VALIDADA
- **Task R.2 — Sub-missions:** ⚪ PENDIENTE
- **Gate Q:** ⚪ PENDIENTE
