# R.3 — Specialist Agents Execution Report
## Advanced Autonomy / Specialized Capabilities, Contracts & Safe Agent Selection

### 1. Resultado ejecutivo

R.3 implementa la entidad especializada que ejecuta una capacidad concreta (**WHO / CAPABILITY**) sin duplicar la unidad delegada de trabajo de R.2 (**WHAT**) ni crear un runtime autónomo paralelo. El resultado reutiliza R.1 Multi-step Planning, R.2 Sub-missions, `ActionExecutor`, Tool Registry, Policy/Auth, aislamiento multi-tenant y observabilidad K.1/K.2.

**Estado:** 🟢 VALIDADA.

### 2. Modelos y contratos

Se incorporaron modelos de dominio inmutables para:

- `SpecialistAgentDefinition`.
- `AgentCapability` y `AgentCapabilityContract`.
- `AgentAvailability` (`AVAILABLE`, `UNAVAILABLE`, `DEGRADED`, `UNKNOWN`).
- `AgentSelectionResult`.
- `AgentExecutionContext`.
- `AgentExecutionResult`.
- Estados de ejecución, selección y taxonomía canónica de fallos.

Los contratos de entrada y salida se validan estrictamente. Los resultados `PARTIAL` también deben cumplir el contrato de salida. Los datos se congelan profundamente y se sanitizan para excluir credenciales, secretos y razonamiento privado.

### 3. Registry y selección determinista

`SpecialistAgentRegistry` mantiene definiciones aisladas por tenant, rechaza duplicados y conflictos, y valida referencias de acciones y tools contra los catálogos existentes.

`SpecialistAgentService` aplica selección **capability-first** y elegibilidad por:

1. capability requerida;
2. política;
3. disponibilidad;
4. allowlist de action types;
5. allowlist de tools;
6. presupuesto y coste estimado;
7. prioridad y `agent_id` como desempate estable.

La selección es fail-safe: `UNKNOWN` availability no equivale a `AVAILABLE`, y coste desconocido no equivale a cero.

### 4. Frontera de ejecución segura

Cada especialista resuelve un binding explícito hacia un `ActionExecutor` protegido. El servicio rechaza bindings no marcados como frontera guardada y no ejecuta herramientas directamente.

Las allowlists y el presupuesto se comprueban antes de alcanzar el executor. Las denegaciones de política se expresan como fallos estructurados y no se reintentan ni convierten en rutas alternativas evasivas.

### 5. Idempotencia, concurrencia y aislamiento

La idempotencia utiliza el scope multidimensional:

`(tenant_id, mission_id, capability_id, action_type, tool_id, idempotency_key)`

El payload material se protege mediante huella SHA-256. Reutilizar la misma clave con un payload distinto produce conflicto `INVALID_INPUT`. Un `threading.RLock` protege la sección crítica para que ejecuciones concurrentes equivalentes produzcan una única llamada al executor.

El registro, la selección y la ejecución están aislados por tenant; una definición perteneciente a otro tenant nunca es elegible.

### 6. Integración R.1 / R.2 y observabilidad

- `execute_plan_step` adapta `PlanStep` de R.1 preservando `budget_remaining` como dato explícito y separado de `estimated_cost`.
- `execute_mission` consume Mission/Sub-mission sin alterar su identidad ni jerarquía.
- `propagate_to_sub_mission` convierte resultados finales en `SubMissionResultContract` para la propagación formal de R.2 hacia R.1.
- Los resultados `PARTIAL` no se presentan como completitud final de una sub-misión.
- Se emiten los eventos canónicos `SPECIALIST_SELECTED`, `SPECIALIST_EXECUTION_STARTED`, `SPECIALIST_EXECUTION_COMPLETED`, `SPECIALIST_EXECUTION_FAILED` y `SPECIALIST_BLOCKED`.
- Auditoría, trazas, fallos y metadata se sanitizan profundamente.

### 7. Evidencia de pruebas

#### Suite específica R.3

- Unitarios: 26 pruebas.
- Integración: 10 escenarios A–J.
- Resultado: **36 passed, 0 failed**.

Los escenarios cubren registro y binding, selección explícita, incertidumbre de disponibilidad/coste, aislamiento tenant, bloqueos previos a side effects, no-bypass de políticas, integración con R.1, propagación a R.2, auditoría/trazas/costes y ejecución idempotente sanitizada.

#### Regresión R.1 / R.2

- R.1 unit + integration: 30 pruebas.
- R.2 unit + integration: 33 pruebas.
- Resultado: **63 passed, 0 failed**.

#### Regresión completa

- **2701 passed**.
- **2 skipped**.
- **0 failed**.
- **289 warnings** de deprecación, sin errores de ejecución.

### 8. Validación operacional

- `scripts/deploy_validate.py`: **5/5 checks PASSED**.
- `scripts/db_migrate.py check`: **Schema UP TO DATE**, revisión `001_initial_saas_schema`.
- VS Code diagnostics: **0 diagnósticos**.
- No se ejecutaron side effects externos reales; las pruebas usan mocks/fakes.
- NO commit.
- NO push.

### 9. Alcance y siguiente tarea

No se implementó R.4+:

- sin Agent Coordination;
- sin Dynamic Delegation;
- sin consenso, negociación o votación;
- sin loops dinámicos de reasignación;
- sin un runtime paralelo.

**Hito R:** 🟡 EN PROGRESO.

**Siguiente tarea del Roadmap:** R.4 — Agent Coordination (⚪ PENDIENTE). Debe iniciarse únicamente con autorización explícita.
