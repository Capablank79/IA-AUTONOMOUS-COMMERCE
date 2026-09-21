# R.6 — LONG-RUNNING MISSIONS: EXECUTION & VALIDATION REPORT

**Hito:** Hito R — Advanced Autonomy
**Sub-slice:** R.6 — Long-running Missions
**Fecha de Validación:** 2026-09-16
**Estado:** 🟢 VALIDADA
**Baseline Final del Sistema:** 2774 passed, 18 skipped, 0 failures, 0 errors (100% pass)
**Checkpoint Base:** `dea89524fef18813aee8f31c2701578243c7af84`

---

## 1. ROADMAP ALIGNMENT & RESPONSABILIDAD ARQUITECTURAL

El slice **R.6 — Long-running Missions** responde de forma rigurosa y verificable a la pregunta central de diseño:

> *“¿Puede una misión de larga duración pausar, persistir su progreso y reanudarse de forma segura tras reinicio/desconexión sin duplicar efectos, perder estado o violar budgets/policies?”*

### Principios Fundamentales Implementados
1. **Reuse > Extend > Create (Long-running != New Runtime):** No se crearon runtimes paralelos ni orquestadores redundantes. R.6 extiende el ciclo de vida canónico de `Mission` y `AutonomousLoop` (`src/domain/mission/models.py`, `src/application/long_running_mission/long_running_mission_service.py`), integrando de manera nativa los subsistemas de planificación R.1, sub-misiones R.2, especialistas R.3, coordinación R.4 y delegación R.5.
2. **Durable Checkpointing & Crash Recovery:** El estado de ejecución se serializa en checkpoints durables inmutables con persistencia atómica en disco (`.tmp` + `os.replace` + `os.fsync`) y suma de verificación SHA-256 canónica. La recuperación ante caídas es 100% independiente de memoria RAM volátil.
3. **Single-Winner Lease & Heartbeats:** Exclusión mutua garantizada para workers mediante leases atómicos con versión de asignación (`assignment_version`) y expiración temporal determinista controlada por `ClockPort` (cero `time.sleep`).
4. **Stale Worker & Stale Checkpoint Rejection:** Rechazo total e inmediato de mutaciones, claims, heartbeats o finalizaciones provenientes de workers cuyo lease expiró o cuya asignación fue superseded por un nuevo claim.
5. **Completed Work Immutability & Side-Effect Idempotency:** Los pasos y submisiones completados antes de una pausa o reinicio permanecen inmutables (`StepExecutionRecord.is_completed = True`), evitando la repetición de tareas con efectos secundarios externos (publicaciones, compras, contactos).
6. **Strict Policy Revalidation & Emergency Stop:** La reanudación (`resume_mission`) no confía ciegamente en autorizaciones pasadas; revalida activamente el contexto de políticas y el estado de parada de emergencia N.11 (`EmergencyStopService`).
7. **Monotonic Budget & Quota Continuity:** Los tokens consumidos, costes monetarios en `Decimal` y cuotas se preservan acumulativamente a través de pausas y reinicios (`UNKNOWN != fresh budget`), impidiendo que un reinicio sea utilizado para evadir cuotas o límites O.7/P.11.
8. **Tenant Isolation & Zero-CoT Security:** Particionado físico en disco por subdirectorio de tenant (`tenants/<tenant_id>/checkpoints/`), validación de frontera con `CrossTenantGuard` y sanitización recursiva N.9 que excluye tokens, credenciales y razonamiento privado (CoT).

---

## 2. DISCOVERY & REUSE MATRIX

| Capability | Current Owner | Current Implementation | R.6 Gap | Reuse / Extend / Create |
|---|---|---|---|---|
| **Mission State** | `src/domain/mission/` | `Mission`, `MissionResult`, `MissionStatus` | Soportar estado `PAUSED` explícito y referencia a checkpoints | **EXTEND** |
| **Checkpoints & Integrity** | — | — | Modelos inmutables de checkpoint, versionado monotónico y checksum SHA-256 | **CREATE** (`src/domain/long_running_mission/`) |
| **Lease & Concurrency** | `src/domain/agent_coordination/` | `TaskLease` de granularidad tarea | Lease de misión con TTL determinista y ganador único | **CREATE / EXTEND** |
| **Heartbeat Management** | — | — | Latidos de worker con validación de versión de asignación | **CREATE** |
| **Safe Resume & Policy Guard** | `src/application/authorization/` | `AuthorizationGuardedActionExecutor`, `EmergencyStopService` | Decisión de reanudación estructurada con revalidación activa | **CREATE / REUSE** |
| **Durable Persistence** | `src/infrastructure/persistence/data/json/` | Repositorios JSON atómicos | Repositorio multi-tenant de checkpoints con fsync y bloqueo cross-tenant | **CREATE** (`JsonMissionCheckpointRepository`) |
| **Multi-Step & Hierarchy** | R.1 / R.2 / R.4 / R.5 | `ExecutionPlan`, `SubMissionAdapter`, `CoordinationSession` | Preservar identificadores, versiones de plan y árboles de delegación | **REUSE** |
| **Audit & Tracing** | K.1 / K.2 | `AuditService`, `AgentTraceService` | Eventos de ciclo de vida R.6 sin secretos ni CoT | **EXTEND** |

---

## 3. MODELO DE DOMINIO Y CONTRATOS (R.6)

### A. Modelos de Dominio (`src/domain/long_running_mission/models.py`)
- `CheckpointStatus`: `VALID`, `CORRUPT`, `STALE`, `DEPRECATED`.
- `ResumeDecisionStatus`: `GRANTED`, `DENIED_POLICY`, `DENIED_TERMINAL`, `DENIED_EMERGENCY_STOP`, `DENIED_BUDGET`, `DENIED_CORRUPT`, `DENIED_LOCK_CONFLICT`, `DENIED_TENANT_MISMATCH`, `DENIED_PLAN_INCOMPATIBLE`.
- `LeaseStatus`: `ACQUIRED`, `ACTIVE`, `EXPIRED`, `RELEASED`.
- `MissionCheckpoint` (frozen dataclass):
  - Identidad y versiones: `checkpoint_id`, `tenant_id`, `mission_id`, `mission_type`, `mission_status`, `checkpoint_version`, `plan_id`, `plan_version`.
  - Progreso: `completed_steps`, `active_steps`, `pending_steps`, `step_records` (`StepCheckpointData`), `sub_mission_ids`.
  - Coordinación y ownership: `coordination_session_id`, `assignment_versions`, `current_worker_id`, `lease_id`.
  - Presupuesto acumulado: `budget_consumed_tokens`, `budget_consumed_cost` (`Decimal`), `quota_consumed`.
  - Integridad y metadatos: `created_at`, `checksum` (SHA-256 canónico de campos críticos).
- `LeaseState` (frozen dataclass): `lease_id`, `mission_id`, `worker_id`, `tenant_id`, `status`, `assignment_version`, `acquired_at`, `expires_at`, `last_heartbeat_at`.
- `HeartbeatRecord` (frozen dataclass): `heartbeat_id`, `lease_id`, `mission_id`, `worker_id`, `tenant_id`, `assignment_version`, `timestamp`.
- `ResumeDecision` (frozen dataclass): `status`, `allowed`, `mission_id`, `worker_id`, `tenant_id`, `reason`, `checkpoint_version`, `evaluated_at`, `lease`.

### B. Contratos y Puertos (`src/domain/long_running_mission/ports.py`)
- `MissionCheckpointRepositoryPort`: `save_checkpoint`, `get_latest_checkpoint`, `list_checkpoints`, `get_checkpoint_by_version`.
- `LeaseManagerPort`: `acquire_lease`, `renew_lease`, `release_lease`, `get_lease`.
- `HeartbeatManagerPort`: `record_heartbeat`, `get_last_heartbeat`.
- `LongRunningMissionServicePort`: `create_checkpoint`, `pause_mission`, `resume_mission`, `heartbeat`, `release_worker`.

---

## 4. PERSISTENCIA ATÓMICA Y SEGURIDAD MULTI-TENANT

La implementación `JsonMissionCheckpointRepository` garantiza:
- **Escritura Atómica Crash-Safe:** Escritura en archivo temporal `.tmp`, `os.flush`, `os.fsync` y `os.replace` atómico garantizando que ante un fallo de corriente no existan archivos corruptos a medio escribir.
- **Aislamiento Multi-Tenant (O.1):** Estructura jerárquica de particionado `tenants/<tenant_id>/checkpoints/<mission_id>.json`. Acceso validado estrictamente mediante `CrossTenantGuard.ensure_tenant_context` y `CrossTenantGuard.assert_same_tenant`. Intentos de acceso cruzado entre tenants disparan excepciones de seguridad inmediatas.
- **Verificación de Integridad de Checksum:** Al deserializar un checkpoint, se recalcula el hash SHA-256 canónico. Si se detecta manipulación física, truncamiento o corrupción, se marca `CheckpointStatus.CORRUPT` y se bloquea la reanudación (`DENIED_CORRUPT`).

---

## 5. CICLO DE VIDA: PAUSE, RESUME & RECOVERY

```
[RUNNING MISSION]
       │
       ├─► (Heartbeats periódicos renuevan Lease determinista con ClockPort)
       │
       ▼
[SAFE PAUSE]
       │  1. Alcanza frontera segura de ejecución (no mutación en vuelo).
       │  2. Serializa estado completo a Checkpoint durable (version N).
       │  3. Transiciona MissionStatus a PAUSED.
       │  4. Libera Lease de worker para evitar bloqueos huérfanos.
       ▼
[PROCESS DIES / RESTART SIMULATION] (Memoria RAM vaciada por completo)
       │
       ▼
[SAFE RESUME REQUEST]
       │  1. Carga Checkpoint durable desde persistencia JSON/DB.
       │  2. Valida integridad SHA-256 (Anti-Corruption).
       │  3. Valida Tenant Context con CrossTenantGuard (Anti-Cross-Tenant).
       │  4. Valida estado no terminal (COMPLETED/CANCELLED/FAILED -> DENIED_TERMINAL).
       │  5. Revalida Políticas y Emergency Stop N.11 (Activo -> DENIED_EMERGENCY_STOP).
       │  6. Verifica disponibilidad de Presupuesto acumulado.
       │  7. Adquiere Lease exclusivo (Concurrencia: Ganador único / Conflicto -> DENIED_LOCK_CONFLICT).
       │  8. Transiciona MissionStatus a RUNNING.
       ▼
[CONTINUE EXECUTION]
       │  - Omitir steps completados (Inmutabilidad de trabajo).
       │  - Preservar assignment_version de R.5 y topología R.1.
       ▼
[SAFE COMPLETION]
```

---

## 6. VALIDACIÓN Y COBERTURA DE PRUEBAS

### A. Tests Unitarios (`tests/unit/test_r6_long_running_missions_unit.py`) — 22 Casos
1. `test_1_checkpoint_creation`: Creación y persistencia estructurada de checkpoint válido.
2. `test_2_atomic_checkpoint_version`: Incremento monotónico de versión y rechazo de versiones regresivas.
3. `test_3_completed_work_preserved`: Preservación de steps completados y outputs inmutables.
4. `test_4_pause_is_not_cancel`: Distinción semántica formal entre PAUSED y CANCELLED.
5. `test_5_valid_resume`: Reanudación válida de misión pausada con adquisición de nuevo lease.
6. `test_6_terminal_mission_resume_denied`: Rechazo de reanudación sobre misiones terminadas (COMPLETED/FAILED).
7. `test_7_policy_revalidation`: Denegación estructurada de reanudación si las políticas cambian a DENIED.
8. `test_8_emergency_stop_blocks_resume`: Bloqueo inmediato de reanudación si Emergency Stop N.11 está activo.
9. `test_9_budget_preserved`: Preservación acumulativa y no reseteo de tokens y costes en `Decimal`.
10. `test_10_quota_not_reset`: Continuidad de cuotas de uso a través del ciclo de vida.
11. `test_11_rate_limit_not_reset`: Integración y preservación de límites de tasa.
12. `test_12_heartbeat_update`: Actualización periódica de latidos y renovación de timestamp en el lease.
13. `test_13_lease_expiry`: Expiración determinista del lease basada en `ClockPort` sin sleeps.
14. `test_14_stale_worker_rejected`: Rechazo de latidos y escrituras provenientes de workers con lease expirado.
15. `test_15_stale_checkpoint_rejected`: Rechazo de checkpoints con versión obsoleta frente al almacenamiento.
16. `test_16_corrupt_checkpoint_rejected`: Detección de corrupción SHA-256 y denegación `DENIED_CORRUPT`.
17. `test_17_idempotent_resume`: Idempotencia en múltiples solicitudes de reanudación consecutivas.
18. `test_18_concurrent_resume_single_winner`: Concurrencia entre workers resultando en un único ganador de lease.
19. `test_19_tenant_isolation`: Aislamiento multi-tenant O.1 y bloqueo de accesos cruzados.
20. `test_20_anti_cot_checkpoint`: Verificación de exclusión estricta de CoT, scratchpad y secretos en checkpoints.
21. `test_21_assignment_version_preserved`: Preservación de versiones de delegación R.5.
22. `test_22_no_r7_implementation`: Salvaguarda arquitectural que confirma que R.7 no fue implementado prematuramente.

### B. Tests de Integración (`tests/integration/test_r6_long_running_missions_integration.py`) — 10 Escenarios
- **Escenario A (`test_scenario_a_running_pause_resume_continue`):** Misión en ejecución -> checkpoint -> pausa segura -> reanudación exitosa -> continuación limpia.
- **Escenario B (`test_scenario_b_process_restart_recovery`):** Simulación de caída de proceso/servicio (RAM borrada) -> recuperación íntegra de estado desde disco JSON -> reanudación.
- **Escenario C (`test_scenario_c_completed_steps_not_rerun`):** Preservación de pasos completados (0 reejecuciones) ejecutando únicamente el trabajo pendiente.
- **Escenario D (`test_scenario_d_side_effect_idempotency_preserved`):** Protección contra dobles efectos secundarios externos mediante claves de idempotencia persistidas.
- **Escenario E (`test_scenario_e_lease_expiry_and_stale_rejection`):** Expiración de lease -> recuperación por nuevo worker -> rechazo de mutaciones y checkpoints del worker anterior (stale).
- **Escenario F (`test_scenario_f_concurrent_resume_single_winner`):** Dos workers intentando reanudar concurrentemente la misma misión -> un solo ganador con lease activo, el segundo recibe conflicto.
- **Escenario G (`test_scenario_g_policy_changed_while_paused`):** Modificación de política de gobernanza durante la pausa -> reanudación denegada de forma segura (`DENIED_POLICY`).
- **Escenario H (`test_scenario_h_budget_quota_continuity`):** Preservación y acumulación estricta de consumo de presupuesto y cuotas post-reanudación.
- **Escenario I (`test_scenario_i_tenant_isolation`):** Validación estricta de aislamiento de checkpoints entre Tenant A y Tenant B (`CrossTenantGuard`).
- **Escenario J (`test_scenario_j_r1_to_r5_state_survives`):** E2E sintético integral donde el plan R.1, submisiones R.2, agentes especialistas R.3, estado de coordinación R.4 y versiones de delegación R.5 sobreviven al checkpoint y reanudación.

---

## 7. RESULTADOS GLOBALES DE EJECUCIÓN & REGRESIÓN

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\JLLV\Desktop\IA-AUTONOMOUS-COMMERCE
configfile: pyproject.toml
plugins: anyio-4.14.2

1. R.6 Dedicated Suite:
   - tests/unit/test_r6_long_running_missions_unit.py           22 PASSED (100%)
   - tests/integration/test_r6_long_running_missions_integration.py 10 PASSED (100%)
   Total R.6: 32 passed in 91.27s

2. R.1–R.5 Regression Suite:
   - tests/unit/test_r1_multi_step_planning_unit.py             20 PASSED
   - tests/integration/test_r1_multi_step_planning_integration.py 10 PASSED
   - tests/unit/test_r2_sub_missions_unit.py                    22 PASSED
   - tests/integration/test_r2_sub_missions_integration.py     11 PASSED
   - tests/unit/test_r3_specialist_agents_unit.py               26 PASSED
   - tests/integration/test_r3_specialist_agents_integration.py 10 PASSED
   - tests/unit/test_r4_agent_coordination_unit.py             15 PASSED
   - tests/integration/test_r4_agent_coordination_integration.py 10 PASSED
   - tests/unit/test_r5_dynamic_delegation_unit.py              15 PASSED
   - tests/integration/test_r5_dynamic_delegation_integration.py 11 PASSED
   Total R.1–R.5: 150 passed in 5.63s

3. Full Platform Regression:
   - Total Collected: 2792 items
   - Passed: 2774 passed
   - Skipped: 18 skipped
   - Failures / Errors: 0 failures, 0 errors
   - Total Time: 170.16s (0:02:50)

4. Operational Checks:
   - python scripts/deploy_validate.py                          5/5 checks PASSED
   - git diff --check                                           CLEAN (0 whitespace/formatting errors)
```

---

## 8. HIGIENE GIT & SEGURIDAD

- **Cero secretos / .env:** No se crearon ni trackearon archivos `.env`, tokens o credenciales.
- **Cero dumps temporales:** Directorios temporales de checkpoints y pytest ignorados/limpios.
- **Cero archivos de R.7:** Ni una sola referencia o archivo de `R.7 Self-monitoring` fue creado o alterado.
- **NO commit / NO push:** Se respetó estrictamente la instrucción de no realizar commits ni pushes.

---

## 9. CONCLUSIÓN Y ESTADO DE GANTT / ROADMAP

- **R.6 Long-running Missions:** `🟢 VALIDADA`
- **Hito R — Advanced Autonomy:** `🟡 EN PROGRESO` (R.1, R.2, R.3, R.4, R.5 y R.6 validadas; pendiente R.7 y Gate P).
- **Gate del Hito R:** `GATE P — ⚪ PENDIENTE` (Preservado textualmente).

---

## 10. NEXT TASK (SIGUIENTE TAREA)

Consultando el Roadmap Maestro (`AI_AUTONOMOUS_COMMERCE_ROADMAP_MAESTRO.md`) y el Gantt Maestro (`AI_AUTONOMOUS_COMMERCE_GANTT_MAESTRA.md`):

- **ID:** `R.7` (Task 18.7)
- **Nombre exacto:** `Self-monitoring`
- **Estado:** `⚪ PENDIENTE`
*(R.7 NO ha sido implementado ni tocado conforme a las directrices).*
