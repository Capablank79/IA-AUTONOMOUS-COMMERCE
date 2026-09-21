# R.7 — SELF-MONITORING: EXECUTION & VALIDATION REPORT

**Hito:** Hito R — Advanced Autonomy
**Sub-slice:** R.7 — Self-monitoring
**Fecha de Validación:** 2026-09-17
**Estado:** 🟢 VALIDADA
**Baseline Final del Sistema:** 2831 passed, 2 skipped, 0 failures, 0 errors (100% pass)
**Checkpoint Base:** `dea89524fef18813aee8f31c2701578243c7af84`

---

## 1. ROADMAP ALIGNMENT & RESPONSABILIDAD ARQUITECTURAL

El slice **R.7 — Self-monitoring** dentro de **Hito R — Advanced Autonomy** resuelve y valida de forma determinista la pregunta arquitectónica central:

> *“¿Puede el sistema autónomo observar su propio estado operacional durante una misión, detectar degradaciones relevantes y reaccionar de forma segura dentro de límites explícitos?”*

### Principios Fundamentales Implementados
1. **Reuse > Extend > Create (Self-monitoring != Parallel Monitoring Stack):** R.7 no duplica collectors, métricas de plataforma ni repositorios de alertas. Consume señales operacionales reales e interactúa directamente con los contratos existentes de R.1 (`MultiStepPlanningServicePort`), R.5 (`AgentCoordinatorPort`), R.6 (`LongRunningMissionServicePort`), K.1 (`AuditRepositoryPort`), K.2 (`AgentTraceService`) y P.8 (`ProductionAlertingService`).
2. **Epistemic Uncertainty Semantics (`UNKNOWN != HEALTHY`, `UNKNOWN != 0`):** La falta o incompletitud de señales requeridas, la presencia de registros K.3 desconocidos o datos de costes sin divisa comparable producen invariablemente `UNKNOWN` y nunca un falso `HEALTHY`. Cero explícito (`Decimal("0")`) se preserva como valor conocido.
3. **Deterministic Temporal Detection with ClockPort (Zero `time.sleep`):** La detección de anomalías temporales (*Stale Heartbeat*, *Temporal Stall*) opera exclusivamente mediante `ClockPort.now()`, comparando marcas de tiempo UTC (`last_seen_at`, `last_progress_at`) contra umbrales configurables (`stale_heartbeat_threshold_seconds`, `stall_timeout_seconds`) en presencia de trabajo activo no terminal.
4. **Failure Rate & Safe Sample Bounds:** Las tasas de fallo técnico requieren muestras válidas iguales o superiores a `min_failure_samples`. Si el denominador es cero o el histórico es insuficiente, el estado resultante es `UNKNOWN`.
5. **Cost Anomaly & Currency Homogeneity in Decimal:** Los cálculos de costes emplean aritmética estricta con `Decimal`. Se exige presencia explícita de `baseline_cost`, `observed_cost` y paridad exacta de divisas (`currency`). Divisas heterogéneas o datos ausentes producen `UNKNOWN`.
6. **Bounded Safe Responses & Inviolable Policy Precedence:** Las acciones de remediación (`CONTINUE_WITH_WARNING`, `PAUSE`, `BLOCK`, `REQUEST_REPLAN`, `REQUEST_DELEGATION`, `ESCALATE`) respetan límites estrictos (`max_remediation_actions_per_mission`, `max_replan_requests`, `max_delegation_requests`). Ante `POLICY_DENIED_VIOLATION`, `EMERGENCY_STOP_TRIGGERED` o agotamiento de presupuesto duro, el sistema fuerza `BLOCK` y rechaza replanes o delegaciones evasivas.
7. **Strict Multi-Tenant Isolation & Signal Provenance:** Toda `HealthSignal` exige acreditación explícita de `tenant_id` y `mission_id`. Cualquier mismatch respecto al `TenantContext` evaluado se rechaza antes de procesar o persistir el assessment, evitando contaminación cruzada.
8. **Zero-CoT & Sensitive Data Redaction:** Las señales, snapshots y auditorías pasan por sanitización recursiva de seguridad N.9, excluyendo tokens, credenciales, prompts privados, reasoning y scratchpads.

---

## 2. DISCOVERY & REUSE MATRIX

| Capability | Current Owner | Existing Implementation | R.7 Gap | Reuse / Extend / Create |
|---|---|---|---|---|
| **Health Signals & Models** | — | — | Señales inmutables con scope tenant/misión, completitud y severidad | **CREATE** (`src/domain/self_monitoring/models.py`) |
| **Assessment & Anomaly Engine** | — | — | Evaluación determinista de latidos, stall, fallos, conflictos, cuotas y costes | **CREATE** (`src/application/self_monitoring/self_monitoring_service.py`) |
| **Audit & Tracing Persistence** | K.1 / K.2 | `JsonAuditRepository`, `AgentTraceService` | Adapter canónico de eventos R.7 sin almacenamiento paralelo | **CREATE / REUSE** (`ProductionSelfMonitoringAuditAdapter`) |
| **Production Alerting** | P.8 | `ProductionAlertingService`, `ProductionAlertInstance` | Extensión mínima para emitir/escalar alertas de salud operacional (`MISSION_HEALTH_DEGRADED`) | **EXTEND** (`src/application/production_alerting/`) |
| **Remediation Dispatcher** | R.1 / R.5 / R.6 | `MultiStepPlanningService`, `AgentCoordinatorService`, `LongRunningMissionService` | Orquestador delgado para materializar replan, delegación y pausa | **CREATE / REUSE** (`SelfMonitoringActionDispatcher`) |
| **Multi-Tenant Persistence** | O.1 | Persistencia JSON atómica con `CrossTenantGuard` | Repositorio multi-tenant particionado de assessments y snapshots | **CREATE** (`JsonSelfMonitoringRepository`) |

---

## 3. MODELO DE DOMINIO Y CONTRATOS (R.7)

### A. Modelos de Dominio (`src/domain/self_monitoring/models.py`)
- `MissionHealthStatus`: `HEALTHY`, `DEGRADED`, `AT_RISK`, `BLOCKED`, `UNKNOWN`.
- `SignalCompleteness`: `COMPLETE`, `PARTIAL`, `UNKNOWN`.
- `SignalType`: `HEARTBEAT_LIVENESS`, `EXECUTION_PROGRESS`, `STALL_TEMPORAL`, `TECHNICAL_FAILURE_RATE`, `COORDINATION_CONFLICT`, `DELEGATION_REPETITION`, `BUDGET_CONSUMPTION_RATE`, `QUOTA_EXHAUSTION`, `RATE_LIMIT_SATURATION`, `LATENCY_ANOMALY`, `COST_SPIKE`, `POLICY_COMPLIANCE`, `EMERGENCY_STOP_STATUS`.
- `SelfMonitoringAction`: `NONE`, `CONTINUE_WITH_WARNING`, `PAUSE`, `BLOCK`, `REQUEST_REPLAN`, `REQUEST_DELEGATION`, `ESCALATE`.
- `HealthSignal` (frozen dataclass): `signal_id`, `signal_type`, `source`, `severity`, `observed_at`, `confidence`, `value`, `state_description`, `evidence_reference`, `metadata`, `tenant_id`, `mission_id`, `completeness`, `missing_fields`.
- `SelfMonitoringPolicy` (frozen dataclass): `stale_heartbeat_threshold_seconds`, `stall_timeout_seconds`, `max_remediation_actions_per_mission`, `max_replan_requests`, `max_delegation_requests`, `failure_rate_threshold`, `min_failure_samples`, `budget_warning_threshold_ratio`, `cost_spike_multiplier_threshold`, `debounce_consecutive_observations`, `required_signal_types`.
- `SelfMonitoringDecision` (frozen dataclass): `action`, `health_status`, `reason_codes`, `rationale`, `evaluated_at`, `suggested_target`, `suggested_payload`, `requires_escalation`, `escalation_severity`, `is_bounded`.
- `HealthAssessment` (frozen dataclass): `assessment_id`, `mission_id`, `status`, `decision`, `signals_evaluated`, `reason_codes`, `remediation_count`, `evaluated_at`, `snapshot`.

### B. Contratos y Puertos (`src/domain/self_monitoring/ports.py`)
- `SelfMonitoringRepositoryPort`: `save_assessment`, `get_latest_assessment`, `list_assessments`, `save_snapshot`, `get_latest_snapshot`.
- `SignalCollectorPort`: `collect_signals(mission_id, context)`.
- `SelfMonitoringAuditPort`: `record_assessment_audited`, `record_action_requested`, `record_escalation`, `record_recovery`.

---

## 4. INTEGRACIÓN PRODUCTIVA Y DISPATCHER DE REMEDIACIÓN

### A. Adapter de Auditoría y Trazabilidad (`ProductionSelfMonitoringAuditAdapter`)
- Reutiliza directamente `AuditRepositoryPort` (K.1) serializando `AuditRecord` inmutables con hashes y metadatos estrictamente whitelisted.
- Reutiliza `AgentTraceService` (K.2) emitiendo pasos de trazabilidad deterministas (`StepType.OBSERVE`, `StepType.EMIT_EVENT`, `StepType.SERVICE_CALL`).
- Reutiliza `ProductionAlertingService` (P.8) mediante `raise_or_escalate_alert` para alertas con severidad `MEDIUM`, `HIGH` o `CRITICAL`, deduplicadas canónicamente por `mission_id` y `tenant_id`.

### B. Orquestador de Despacho Seguro (`SelfMonitoringActionDispatcher`)
- `REQUEST_REPLAN`: Resuelve el plan canónico vía `ExecutionPlanRepositoryPort.get_plan_by_mission_id` e invoca `MultiStepPlanningServicePort.replan`.
- `REQUEST_DELEGATION`: Resuelve la sesión vía `CoordinationSessionRepositoryPort.get_session_by_mission` e invoca `AgentCoordinatorPort.delegate_task`.
- `PAUSE`: Resuelve el lease de worker activo vía `LeaseManagerPort.get_lease` e invoca `LongRunningMissionServicePort.pause_mission`.
- `BLOCK`: Invariante de seguridad inquebrantable; rechaza cualquier evasión mediante replan o delegación.

---

## 5. VALIDACIÓN Y MATRIZ DE PRUEBAS

### A. Pruebas Unitarias (`tests/unit/test_r7_self_monitoring_unit.py` — 28 passed)
- `test_1_healthy_assessment_with_complete_signals`: Misión saludable con señales completas requeridas.
- `test_2_missing_signals_unknown`: Ausencia de señales produce `UNKNOWN`.
- `test_3_stale_heartbeat_detected`: Latido expirado detectado deterministamente con `VirtualClock`.
- `test_4_temporal_stall_before_and_after_timeout`: Stall evaluado correctamente antes (saludable) y después del timeout (AT_RISK).
- `test_4c_temporal_stall_missing_evidence_yields_unknown`: Evidencia de stall incompleta produce `UNKNOWN`.
- `test_5_repeated_technical_failures`: Detección de ratio de fallos técnicos e indicación de replan.
- `test_6_coordination_conflict`: Detección de colisión de coordinación entre agentes.
- `test_7_repeated_delegation_near_bound`: Alerta de presión de delegaciones sucesivas.
- `test_8_budget_pressure`: Alerta temprana por consumo de presupuesto > 85%.
- `test_9_quota_exhaustion`: Detección de agotamiento de cuota O.7.
- `test_10_rate_limit_pressure`: Detección de saturación de rate limits P.11.
- `test_11_valid_cost_anomaly`: Detección de anomalías de coste relativo en `Decimal`.
- `test_12_mixed_currency_not_compared`: Rechazo de comparación entre divisas heterogéneas (`USD` vs `CLP`).
- `test_13_cost_missing_fields_yields_unknown_and_explicit_zero`: Preservación de `UNKNOWN` ante datos faltantes y preservación de `Decimal("0")` explícito.
- `test_14_failure_rate_denominator_zero`: Denominador cero o muestras insuficientes resultan en `UNKNOWN`.
- `test_15_request_replan_valid`: Decisión estructurada `REQUEST_REPLAN`.
- `test_16_policy_denial_no_replan`: Invariante anti-bypass: `POLICY_DENIED` fuerza `BLOCK` sin replan.
- `test_17_request_delegation_valid`: Decisión estructurada `REQUEST_DELEGATION`.
- `test_18_emergency_stop_block`: Parada de emergencia N.11 fuerza `BLOCK`.
- `test_19_pause_decision`: Misión pausada ante riesgo temporal.
- `test_20_escalation_on_remediation_limit`: Escalación inmediata al alcanzar el límite acotado de remediaciones.
- `test_21_tenant_and_mission_mismatch_rejection`: Rechazo con `ValueError` ante señales con tenant o misión discrepantes.
- `test_22_partial_completeness_never_produces_healthy`: Señales con completitud `PARTIAL` o `UNKNOWN` impiden `HEALTHY`.
- `test_23_required_signal_coverage_enforcement`: Falta de tipos de señales requeridas degrada a `UNKNOWN`.
- `test_24_tenant_isolation`: Aislamiento físico de persistencia entre Tenant A y Tenant B.
- `test_25_anti_cot_and_sensitive_data`: Sanitización recursiva N.9 y exclusión de CoT/secrets.
- `test_26_bounded_response`: Respeto estricto del límite global de acciones de remediación.
- `test_27_no_post_r7_feature`: Invariante de frontera arquitectónica que previene dependencias de Hito S no implementado.

### B. Pruebas de Integración (`tests/integration/test_r7_self_monitoring_integration.py` — 13 passed)
- `test_scenario_a_healthy_mission_no_intervention`: Flujo normal sin degradación.
- `test_scenario_b_stale_heartbeat_at_risk`: Detección y contención de latido vencido.
- `test_scenario_c_technical_failures_request_replan`: Disparo de replanificación acotada.
- `test_scenario_d_agent_unavailable_request_delegation`: Solicitud de delegación ante agente especialista no disponible.
- `test_scenario_e_policy_denied_block_no_replan`: Bloqueo inmediato por violación de políticas.
- `test_scenario_f_budget_hard_limit_block`: Bloqueo por presupuesto agotado.
- `test_scenario_g_recoverable_degradation_pause_checkpoint`: Pausa segura y generación de checkpoint.
- `test_scenario_h_critical_degradation_escalation`: Escalación a operaciones mediante P.8 ante fallo crítico.
- `test_scenario_i_tenant_isolation`: Verificación E2E de aislamiento estricto multi-tenant.
- `test_scenario_j_health_recovery_after_normalization`: Recuperación y auditoría de transición de estado tras normalización.
- `test_scenario_k_e2e_autonomous_workflow_integration`: Flujo integral multietapa R.1–R.7.
- `test_scenario_l_real_dispatcher_replan_delegation_pause_and_block_invariants`: Verificación del dispatcher real sobre contratos R.1, R.5 y R.6.
- `test_scenario_m_real_audit_adapter_integration_with_k1_k2_p8`: Verificación del adapter real persistiendo en K.1, K.2 y emitiendo alertas P.8 deduplicadas.

---

## 6. REGRESIÓN GLOBAL Y COMPROBACIONES DE ENTORNO

1. **R.7 Targeted:** `41 passed in 12.22s`.
2. **R.1–R.7 Suite Regression:** `223 passed in 6.63s`.
3. **Full System Regression Baseline:** `2831 passed, 2 skipped, 297 warnings in 196.66s` (0 failed, 0 errors).
4. **Deployment Automation (`deploy_validate.py`):** `5/5 CHECKS PASSED`.
5. **Database Migration (`db_migrate.py check`):** `Schema is UP TO DATE (revision: 001_initial_saas_schema)`.
6. **Git Hygiene:** `git diff --check` limpio, sin leaks en `.pytest_tmp`, cero commits no autorizados.

---

## 7. SIGUIENTE TAREA FORMAL

Con la validación formal de **R.7 — Self-monitoring**, concluyen todas las tareas unitarias del **Hito R — Advanced Autonomy** (R.1 a R.7 validadas). De acuerdo con la nomenclatura oficial del Roadmap y Gantt:

- **NEXT TASK ID:** `GATE P` (según nomenclatura actual de Hito R en Roadmap/Gantt)
- **NEXT TASK NAME:** `Gate Formal de Hito R — Advanced Autonomy`
- **CURRENT STATUS:** ⚪ PENDIENTE (NO ejecutado en esta iteración conforme a las instrucciones).
