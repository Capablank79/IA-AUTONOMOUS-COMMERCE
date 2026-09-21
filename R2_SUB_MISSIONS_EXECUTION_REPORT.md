# R.2 — Sub-missions Execution Report
## Advanced Autonomy / Hierarchical Mission Delegation, Child Lifecycle & Safe Result Propagation

### 1. Roadmap Alignment & Executive Summary
El objetivo de **R.2 — Sub-missions** dentro del **Hito R — Advanced Autonomy** es responder de forma formal, determinista, acotada y tenant-safe a la pregunta arquitectural fundamental:
> *"¿Puede una misión padre delegar partes autocontenidas de su objetivo en sub-misiones explícitas, aisladas y trazables, y recomponer sus resultados de forma segura?"*

R.2 extiende el modelo canónico de misiones (`Mission`) y el repositorio multi-tenant (`JsonTenantMissionRepository`) permitiendo la creación de jerarquías padre-hijo acotadas (`parent_mission_id`, `root_mission_id`, `depth`, `delegation_key`, `is_required`). Las sub-misiones ejecutan trabajo autocontenido a través de los componentes de runtime existentes (`AutonomousLoop`, `ActionExecutor`), integrando sus salidas estructuradas de vuelta en el plan de ejecución de R.1 (`ExecutionPlan` / `PlanStep`). Toda la delegación respeta límites de profundidad y abanico, aislamiento estricto entre hermanos y tenants (O.1), políticas de parada de emergencia (N.11), prevención de bucles o evasiones de políticas, y emite auditoría K.1 y trazas K.2 seguras sin Chain-of-Thought (Anti-CoT).

---

### 2. Architecture & Design Principles (REUSE > EXTEND > CREATE)
- **Sub-mission != PlanStep:** R.1 divide un `ExecutionPlan` en pasos atómicos de ejecución; R.2 crea misiones hijas completas con identidad, ciclo de vida, contexto aislado y presupuesto propio derivado del padre.
- **Sub-mission != Specialist Agent:** R.2 delega **trabajo** utilizando exclusivamente las capacidades y motores de runtime existentes (`AutonomousLoop`, `ActionExecutor`). No introduce nuevos tipos de agentes (reservados para R.3+).
- **Mission Source of Truth:** No se creó un almacenamiento paralelo para sub-misiones. Se extendió el agregador canónico `Mission` y `JsonTenantMissionRepository` para implementar `SubMissionRepositoryPort`.
- **Aislamiento Multi-Tenant (O.1):** Las misiones padre e hijas residen en el mismo tenant. `CrossTenantGuard` y el almacenamiento aislado por tenant impiden cualquier acceso, delegación o lectura cruzada.
- **Gobernanza y Autorización (N.3 / N.4 / N.11):** La creación de sub-misiones no evade políticas ni cuotas. Si la política deniega una acción o la parada de emergencia N.11 está activa para la misión/tenant, la sub-misión no puede crearse ni ejecutarse.
- **Presupuesto Acotado y Aislamiento de Hermanos:** La suma de los presupuestos asignados a las hijas más el reservado del padre no excede el presupuesto disponible (`sum(sibling_budgets) + allocated <= parent_budget`). `UNKNOWN != unlimited`.
- **Observabilidad Segura (K.1 / K.2 / N.9):** Emisión de eventos auditables canónicos (`SUB_MISSION_CREATED`, `SUB_MISSION_STARTED`, `SUB_MISSION_COMPLETED`, `SUB_MISSION_FAILED`, `SUB_MISSION_CANCELLED`, `SUB_MISSION_BLOCKED`, `SUB_MISSION_RESULT_PROPAGATED`) y trazas K.2 estructuradas sin secretos ni Chain-of-Thought.

---

### 3. Canonical Sub-mission Contracts & Models

#### Modelos de Dominio (`src/domain/sub_mission/models.py`)
- `SubMissionScope`: Definición inmutable del alcance con `objective`, `expected_outcome`, `completion_condition`, `expected_output_keys`, `target_resource` y `constraints`. Valida cadenas no vacías y sanitiza metadatos.
- `SubMissionCreationContract`: Contrato de delegación con `parent_mission_id`, `tenant_id`, `sub_mission_type`, `scope`, `inputs`, `allocated_budget`, `priority`, `delegation_key`, `plan_id`, `plan_step_id`, `is_required` y `correlation_id`.
- `SubMissionResultContract`: Resultado estructurado retornado por la hija con `mission_id`, `parent_mission_id`, `tenant_id`, `status`, `outputs`, `evidence_refs`, `failure_type`, `failure_reason`, `cost_spent`, `tokens_spent`, `completed_at` e `is_required`.
- `SubMissionHierarchyPolicy`: Parámetros de acotamiento jerárquico (`max_depth=3`, `max_children_per_parent=10`, `max_total_descendants=50`).
- `SubMissionNode`: Proyección de árbol jerárquico para observabilidad y visualización en dashboards (Q.4).

#### Taxonomía de Fallos de Sub-misión (`SubMissionFailureType`)
1. `TECHNICAL_FAILURE`: Error de infraestructura o fallo en ejecución de herramientas del runtime.
2. `POLICY_DENIED`: Bloqueo por política o gobernanza de seguridad.
3. `BUDGET_EXHAUSTED`: Consumo total del presupuesto asignado a la sub-misión.
4. `CANCELLED`: Cancelación explícita o propagada desde el padre.
5. `INVALID_INPUT`: Inputs o parámetros inconsistentes con el contrato esperado.
6. `DEPENDENCY_FAILURE`: Falla en dependencias de datos o precondiciones requeridas.
7. `EMERGENCY_STOP_BLOCKED`: Bloqueo por activación de parada de emergencia N.11.
8. `HIERARCHY_VIOLATION`: Intento de ciclo, exceso de profundidad o de abanico de hijos.
9. `UNKNOWN`: Causa indeterminada de fallo.

---

### 4. Mathematical Precision, Invariants & Hierarchy Safety

1. **Invariantes Jerárquicos y Detección de Ciclos:**
   - Una sub-misión no puede ser su propio padre (`child != parent`).
   - El padre debe existir, ser no-terminal (`status not in COMPLETED, FAILED, ABORTED`) y pertenecer al mismo `tenant_id`.
   - Detección de ciclos determinista en la cadena de ancestros (`HierarchyCycleDetectedError`).
2. **Límites de Profundidad y Abanico (Fan-out Bounds):**
   - Profundidad estrictamente acotada (`depth <= max_depth`). Superar el límite genera rechazo determinista (`MaxDepthExceededError`).
   - Conteo de hijos directos acotado (`child_count < max_children_per_parent`). Superar el límite genera `MaxChildrenExceededError`.
3. **Preservación y Partición de Presupuesto:**
   - Si el padre tiene límites de tokens o costos, la sub-misión no puede tener presupuesto indefinido (`UNKNOWN != unlimited`).
   - Control de presupuestos entre hermanos: una sub-misión no puede reclamar presupuesto reservado para otra sub-misión activa.
4. **Idempotencia y Concurrencia:**
   - La combinación `(parent_mission_id, delegation_key)` previene la creación de sub-misiones duplicadas. Solicitudes repetidas retornan la misma entidad lógica existente.
   - Sincronización mediante lock reentrante en memoria (`threading.RLock`) y verificación atómica en el repositorio.
5. **Propagación Segura de Resultados y Fallos:**
   - Cuando una sub-misión finaliza exitosamente (`COMPLETED`), sus salidas tipadas se propagan al paso correspondiente del plan R.1 (`PlanStep.status = COMPLETED`, `actual_outputs = outputs`).
   - El fallo de una sub-misión obligatoria (`is_required=True`) marca el `PlanStep` como `FAILED` y permite a R.1 iniciar su ciclo de replanificación acotada de subgrafos.
   - Si el fallo es por `POLICY_DENIED` o `EMERGENCY_STOP_BLOCKED`, la regla Anti-Policy Bypass bloquea el plan impidiendo reintentos evasivos.
6. **Cancelación en Cascada:**
   - La cancelación o aborto del padre (`cancel_hierarchy`) transiciona inmediatamente todos los descendientes activos a `ABORTED` persistiendo la razón de cancelación. Los hijos previamente completados permanecen inmutables en el histórico.

---

### 5. Services & Ports Implemented

- `src/domain/sub_mission/ports.py`:
  - `SubMissionRepositoryPort`: Consulta de hijos, descendientes, delegation key y persistencia de resultados estructurados.
  - `SubMissionServicePort`: Orquestación de creación, propagación, cancelación y consulta de árbol jerárquico.
- `src/domain/sub_mission/validator.py`:
  - `SubMissionHierarchyValidator`: Validador puro de invariantes jerárquicos, ciclos, profundidad, límites de abanico y asignación de presupuestos.
- `src/application/sub_mission/service.py`:
  - `SubMissionService`: Servicio principal de aplicación que integra validación, Emergency Stop N.11, persistencia multi-tenant, sincronización con R.1 `MultiStepPlanningService`, emisión de auditoría K.1 y trazas K.2.
- `src/infrastructure/persistence/data/json/tenant_mission_repository.py`:
  - Extensión de persistencia multi-tenant JSON con soporte para campos jerárquicos (`parent_mission_id`, `root_mission_id`, `depth`, `delegation_key`, `is_required`) y almacenamiento de resultados estructurados `SubMissionResultContract`.

---

### 6. Test Suite & Verification Results

#### Pruebas Unitarias (`tests/unit/test_r2_sub_missions_unit.py`)
22 pruebas unitarias cubriendo todos los aspectos requeridos:
- Creación válida de sub-misiones con prefijo y correlación canónica.
- Enlace determinista `parent_mission_id` / `root_mission_id` y cálculo de profundidad.
- Aislamiento estricto de tenant (`same_tenant_required`).
- Detección y rechazo de ciclos en la jerarquía.
- Control de profundidad máxima (`max_depth`) y abanico máximo (`max_children_per_parent`).
- Validación de alcance explícito (`SubMissionScope`) y rechazo de cadenas vacías o compuestas solo de espacios.
- Herencia mínima de contexto y exclusión estricta de secretos y razonamiento interno (Anti-CoT).
- Partición y asignación de presupuesto respetando `UNKNOWN != unlimited`.
- Aislamiento de presupuestos entre hermanos.
- Propagación de resultados exitosos hacia planes y pasos de R.1.
- Propagación de fallos de sub-misiones obligatorias vs. comportamiento de sub-misiones opcionales.
- Bloqueo de compleción del padre mientras existan sub-misiones obligatorias activas.
- Cancelación jerárquica en cascada respetando inmutabilidad de resultados completados.
- Creación idempotente y prevención de duplicados concurrentes por `delegation_key`.
- Validación de que no se implementaron componentes de R.3 (agentes especialistas).

#### Pruebas de Integración y E2E (`tests/integration/test_r2_sub_missions_integration.py`)
11 escenarios de integración y E2E (Escenarios A–K):
- **Escenario A:** Jerarquía válida de padre con 2 hijos a distintos niveles de profundidad.
- **Escenario B:** Ejecución de sub-misiones a través del runtime existente (`AutonomousLoop` + `ActionExecutor`) sin duplicación de ejecutores.
- **Escenario C:** Desbloqueo y progresión del padre tras la compleción de sub-misiones obligatorias.
- **Escenario D:** Mapeo de fallo técnico a replanificación acotada de subgrafo en R.1 sin loops de spawn infinito.
- **Escenario E:** Denegación por política (`POLICY_DENIED`) respetada, impidiendo bypass o replanes evasivos.
- **Escenario F:** Manejo seguro de agotamiento de presupuesto (`BudgetExceededError`).
- **Escenario G:** Cancelación en cascada del padre abortando descendientes activos y preservando históricos completados.
- **Escenario H:** Aislamiento estricto entre Tenant A y Tenant B.
- **Escenario I:** Delegación concurrente con múltiples hilos resolviendo una única sub-misión lógica.
- **Escenario J:** Trazabilidad y auditoría completa de extremo a extremo (K.1 `SUB_MISSION_*` y K.2 `AgentTraceRecord`).
- **Escenario K (E2E Sintético):** Flujo completo de extremo a extremo: Misión padre -> Plan R.1 -> Creación de sub-misiones -> Ejecución en runtime -> Propagación estructurada -> Fallo técnico controlado -> Replan acotado de subgrafo -> Ejecución de paso sustituto -> Paso final -> Completitud del padre sin efectos secundarios reales.

---

### 7. Regression & Deployment Validation

- **Pruebas Específicas R.2:**
  - `tests/unit/test_r2_sub_missions_unit.py`: 22 passed
  - `tests/integration/test_r2_sub_missions_integration.py`: 11 passed
  - **Subtotal R.2:** 33 passed, 0 failed.
- **Regresión R.1 Multi-step Planning:**
  - `tests/unit/test_r1_multi_step_planning_unit.py`: 20 passed
  - `tests/integration/test_r1_multi_step_planning_integration.py`: 10 passed
  - **Subtotal R.1:** 30 passed, 0 failed.
- **Regresión Completa del Repositorio:**
  - **Resultado:** `2649 passed, 18 skipped, 0 failures, 0 errors`
  - **Delta respecto a baseline inicial (2616 passed):** +33 tests nuevos correspondientes a R.2.
- **Validación de Despliegue (`scripts/deploy_validate.py`):**
  - Config, imports, migrations, secrets scan, syntax: **ALL CHECKS PASSED (exit code 0)**.
- **Higiene de Git:**
  - `git diff --check`: 0 errores / limpio.
  - Sin volcados de runtime, sin `.env`, sin secretos, sin archivos de R.3+.
  - NO commit / NO push.

---

### 8. Conclusión & Próximos Pasos
**Hito R — TASK 18.2 / R.2 (Sub-missions)** se encuentra completamente implementada, integrada y validada con 0 fallos.
- **Estado de R.2:** 🟢 VALIDADA.
- **Estado de Hito R:** 🟡 EN PROGRESO.
- **Próxima Tarea del Roadmap:** **TASK 18.3 / R.3 — Specialist Agents** (⚪ PENDIENTE).
