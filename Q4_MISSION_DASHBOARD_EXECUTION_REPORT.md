# Q.4 — MISSION DASHBOARD EXECUTION REPORT
**Business Intelligence / Autonomous Mission Status, Progress & Outcome Visibility**

---

## 1. ROADMAP ALIGNMENT & RESPONSABILIDAD
- **Hito:** Q — Business Intelligence
- **Task:** Q.4 — Mission Dashboard
- **Responsabilidad de Negocio:** Responder con rigor técnico y analítico a la pregunta canónica:
  > *“¿Puede un usuario autorizado visualizar, filtrar y auditar el estado, progreso, cronología unificada y resultados de las misiones autónomas del tenant de forma consultiva y sin interferir en la ejecución?”*
- **Naturaleza del Dashboard:**
  - Estrictamente **consultivo, proyectivo y explicable**.
  - **DASHBOARD != EXECUTION ENGINE:** El dashboard no muta estados de misiones, no dispara tareas del scraper, no reintenta misiones fallidas ni interactúa directamente con motores de inferencia LLM o APIs de marketplaces.
  - Basado en datos reales preexistentes: Modelo de Misiones (`Mission`, `MissionResult`), Auditoría inmutable (Hito K.1 `AuditRecord`), Trazas operacionales de agentes (Hito K.2 `AgentTraceRecord`) y enriquecimiento consultivo con Hitos Q.1, Q.2 y Q.3.
  - Aislamiento multi-tenant por defecto (*Default Deny* / storage físico particionado con `CrossTenantGuard` y `TenantContext`).
  - Semántica estricta de incertidumbre: `UNKNOWN != 0`, `UNKNOWN != SUCCESS`, `duration_seconds=None`, `progress_pct=None` en misiones no finalizadas o datos faltantes.
  - Exclusión recursiva de credenciales y Anti-Chain-of-Thought (Anti-CoT) privado (Hito N.9).
  - Integración transparente con RBAC (Hitos O.4 y N.4) y Admin Console (Hito O.10).
- **Límites de Dominio y Alcance:**
  - Implementado **exclusivamente** Q.4.
  - Tareas Q.5 (Agent Cost Dashboard), Q.6 (Business KPIs) y Gate P no fueron tocadas ni modificadas.

---

## 2. DISCOVERY & MATRIZ DE FUENTES DE DATOS DE MISIONES
Se realizó el relevamiento exhaustivo de componentes de ejecución y fuentes de verdad en el codebase:

| ENTITY / COMPONENT | SOURCE MODEL | LOCATION | PERSISTENCE | SCOPE / RETRIEVAL | Q.4 USE |
|---|---|---|---|---|---|
| Mission Specification | `Mission`, `MissionType`, `MissionStatus`, `MissionPriority` | `src/domain/mission/models.py` | JSON (`data/tenants/{tenant_id}/missions/`) / PostgreSQL | Multi-Tenant Partitioned | Estado, objetivo, target y parámetros base |
| Mission Result & Output | `MissionResult`, `MissionTraceEntry` | `src/domain/mission/models.py` | JSON / PostgreSQL | Vinculado por `mission_id` | Resultado, duración, errores, bloques y pasos internos |
| Immutable Audit Records | `AuditRecord`, `AuditRecordType`, `AuditActor` | `src/domain/audit/models.py` | Append-Only JSON (`data/audit/`) / PostgreSQL | Indexado por `mission_id` (K.1) | Cronología de eventos de ciclo de vida e integridad |
| Agent Execution Traces | `AgentTraceRecord`, `TraceStatus`, `StepType` | `src/domain/agent_trace/models.py` | Append-Only JSON (`data/traces/`) / PostgreSQL | Indexado por `mission_id` (K.2) | Trazas detalladas de operaciones de agentes |
| Market Opportunities | `OpportunityRecord` | `src/domain/opportunity_detection/models.py` | Multi-Tenant Partitioned | Cross-Domain Link (Q.1) | Enlace consultivo a oportunidades analizadas |
| Supplier Intelligence | `Supplier` | `src/domain/supplier_intelligence/models.py` | Multi-Tenant Partitioned | Cross-Domain Link (Q.2) | Enlace consultivo a proveedores evaluados |
| Profit & Unit Economics | `ProfitDashboardItem` | `src/domain/profit_dashboard/models.py` | Multi-Tenant Partitioned | Cross-Domain Link (Q.3) | Enlace consultivo a rentabilidad evaluada |

---

## 3. MISSION SOURCE OF TRUTH & PERSISTENCIA
- **SOURCE OF TRUTH:** Facts originados en `Mission` y `MissionResult` persistidos mediante repositorio multi-tenant.
- **Repository Pattern:** Puerto `TenantMissionRepositoryPort` en `src/domain/mission_dashboard/ports.py` e implementación `JsonTenantMissionRepository` en `src/infrastructure/persistence/data/json/tenant_mission_repository.py`.
- **Escritura Atómica & File Isolation:**
  - Validación estricta de identificadores seguros con `validate_safe_identifier` y custodia por `CrossTenantGuard`.
  - Escrituras atómicas con archivo temporal `.tmp` y reemplazo atómico `os.replace` + `fsync`.
  - Partición física en `data/tenants/{tenant_id}/missions/missions.json` y `data/tenants/{tenant_id}/missions/results.json`.
  - Cero filtraciones de registros entre tenants.

---

## 4. MISSION VIEW MODEL & DATA CONTRACT
Se implementaron DTOs y ViewModels inmutables en `src/domain/mission_dashboard/models.py`:

1. **`MissionDashboardItem`**:
   - `mission_id`, `mission_type`, `status`, `priority`, `created_at`, `updated_at`, `finished_at`
   - Métricas de ejecución: `duration_seconds`, `progress_pct`, `iteration_count`
   - Metadatos de contexto: `goal`, `target`, `opportunity_id`, `supplier_id`, `product_id`, `marketplace`, `category`
   - Contadores analíticos: `error_count`, `block_count`, `evidence_count`, `decision_count`, `has_errors`
   - Resúmenes: `outcome_summary`, `result_summary`, `parameters_summary` (sanitizado)
   - `unknown_fields`: Tupla explícita de campos desconocidos/no reportados.
2. **`MissionDashboardSummary`**:
   - `total_missions`, `pending_count`, `running_count`, `completed_count`, `failed_count`, `blocked_count`, `aborted_count`
   - Estadísticas agregadas de duración: `average_duration_seconds`, `shortest_duration_seconds`, `longest_duration_seconds`
   - Distribuciones categóricas: `missions_by_type`, `missions_by_status`, `missions_by_priority`
   - Métricas de calidad: `success_rate_pct`, `missions_with_errors`
3. **`MissionDashboardDetail`**:
   - Ítem principal proyectado `item: MissionDashboardItem`
   - `unified_timeline: Tuple[MissionTimelineEvent, ...]`: Reconstrucción cronológica determinista unificando trazas de bucle, auditoría K.1 y trazas de agentes K.2.
   - `cross_domain_links: Tuple[CrossDomainEntityLink, ...]`: Enlaces enriquecidos hacia Oportunidades (Q.1), Proveedores (Q.2) y Rentabilidad (Q.3).
   - `errors: Tuple[str, ...]`, `blocks: Tuple[str, ...]`, `evidences_summary: Mapping[str, Any]`

---

## 5. SEMÁNTICA DE INCERTIDUMBRE (UNKNOWN != 0) Y ANTI-COT DEFENSE
- **UNKNOWN Semantics:**
  - Si una misión se encuentra en estado `PENDING`, `RUNNING` o `BLOCKED`, `duration_seconds` y `finished_at` se reportan explícitamente como `None` (no $0.0$ segundos).
  - Si el progreso no ha sido computado o emitido, `progress_pct` se reporta como `None` y se añade a `unknown_fields`.
  - Estados fallidos o incompletos nunca se consideran exitosos.
- **Sensitive Data & Anti-CoT Defense (N.9):**
  - Sanitización y redacción recursiva de todas las llaves y valores que contengan credenciales (`api_key`, `token`, `secret`, `password`, `bearer_token`, etc.).
  - Exclusión estricta de cadenas de pensamiento privado (`private_thought`, `reasoning_chain`, `cot`, `thought_process`).

---

## 6. SERVICE LAYER, FILTROS Y ORDENACIÓN DETERMINISTA
En `src/application/mission_dashboard/mission_dashboard_service.py` se implementó `MissionDashboardService`:
- **Filtros Multi-Criterio:**
  - `status`, `mission_type`, `priority`, `opportunity_id`, `supplier_id`, `product_id`, `marketplace`, `category`
  - `has_errors` (booleano para detectar incidentes o excepciones en ejecución)
  - Rangos de fechas: `date_from`, `date_to`, `created_after`, `created_before`
  - Búsqueda textual amplia: `search_text` / `search` (búsqueda en ID, objetivo, target, tipo, status, resumen y parámetros)
- **Ordenación Determinista:**
  - Campos: `created_at`, `updated_at`, `duration`, `status`, `priority`, `mission_id`
  - Direcciones: `asc`, `desc`
  - Regla de Nulos: Valores `None` van al final independientemente de la dirección.
  - Desempate determinista secundario mandatario por `mission_id`.
- **Paginación Segura:** Parámetros `page >= 1` y `page_size` acotado en $[1, 100]$.

---

## 7. CRONOLOGÍA UNIFICADA (K.1 & K.2) & CROSS-DOMAIN LINKING
- **Unified Timeline:**
  - Fusión de 3 flujos de eventos:
    1. Trazas internas de la misión (`MissionTraceEntry` -> `source="MISSION_TRACE"`)
    2. Registros inmutables de auditoría K.1 (`AuditRecord` -> `source="AUDIT"`)
    3. Trazas operacionales de agentes K.2 (`AgentTraceRecord` -> `source="AGENT_TRACE"`)
  - Ordenación cronológica ascendente por `timestamp` con desempate determinista por `event_id` o paso.
- **Cross-Domain Linking:**
  - Resolución consultiva y explicable de entidades vinculadas:
    - Oportunidades (`OPPORTUNITY` -> `OpportunityRecord` vía Q.1)
    - Proveedores (`SUPPLIER` -> `Supplier` vía Q.2)
    - Rentabilidad (`PROFIT_ITEM` -> `ProfitDashboardItem` vía Q.3)

---

## 8. TENANT ISOLATION & AUTHORIZATION RBAC
- **Aislamiento Multi-Tenant (Hito O.1):** Validación de pertenencia al tenant en cada invocación (`list_missions`, `get_mission_summary`, `get_mission_detail`). Intentos de acceso cruzado entre tenants son denegados con código HTTP 403 / segregación de disco.
- **RBAC (Hitos O.4 y N.4):**
  - Permiso requerido: `MISSION_DASHBOARD_READ` (o `BUSINESS_INTELLIGENCE_READ` / `SYSTEM_ADMIN`).
  - Respuestas: 401 Unauthorized para peticiones sin sesión válida; 403 Forbidden para sesiones sin el permiso requerido.

---

## 9. API REST & ADMIN CONSOLE UI
En `src/infrastructure/web/admin_app.py` y `src/infrastructure/web/app.py` se montaron:
- **Endpoints REST API:**
  - `GET /api/bi/tenants/{tenant_id}/missions` — Lista paginada con filtros multicriterio y ordenación.
  - `GET /api/bi/tenants/{tenant_id}/missions/summary` — Resumen estadístico de misiones, tasas de éxito y duraciones.
  - `GET /api/bi/tenants/{tenant_id}/missions/{mission_id}` — Detalle de la misión con cronología unificada y enlaces cruzados.
- **Admin Console UI View:**
  - `GET /bi/missions` — Superficie web responsive con tarjetas de resumen de misiones (Total, Running, Success Rate, Avg Duration) y visualización del catálogo y cronología.

---

## 10. EVIDENCIA DE EJECUCIÓN Y PRUEBAS

### Pruebas Unitarias de Q.4:
```bash
python -m pytest tests/unit/test_q4_mission_dashboard_unit.py -v
```
**Resultado:** `9 passed in 2.52s`

### Pruebas de Integración de Q.4:
```bash
python -m pytest tests/integration/test_q4_mission_dashboard_integration.py -v
```
**Resultado:** `10 passed in 9.72s`

### Regresión Focalizada de Hito Q (Q.1, Q.2, Q.3, Q.4):
```bash
python -m pytest tests/unit/test_q1_opportunity_dashboard_unit.py tests/integration/test_q1_opportunity_dashboard_integration.py tests/unit/test_q2_supplier_dashboard_unit.py tests/integration/test_q2_supplier_dashboard_integration.py tests/unit/test_q3_profit_dashboard_unit.py tests/integration/test_q3_profit_dashboard_integration.py tests/unit/test_q4_mission_dashboard_unit.py tests/integration/test_q4_mission_dashboard_integration.py -v
```
**Resultado:** `81 passed in 21.83s` (Cero fallos)

### Regresión de Trazas y Auditoría (K.1 & K.2):
```bash
python -m pytest tests/unit/domain/mission/ tests/unit/test_k1_audit_trail_unit.py tests/integration/test_k1_audit_trail_integration.py tests/unit/test_k2_agent_trace_unit.py tests/integration/test_k2_agent_trace_integration.py -v
```
**Resultado:** `61 passed in 3.56s` (Cero fallos)

### Validación de Despliegue:
```bash
python scripts/deploy_validate.py
```
**Resultado:**
```
================================================================
      O.13 DEPLOYMENT AUTOMATION VALIDATION SUITE
================================================================
[1/5] Checking Dockerfile and .dockerignore...
[PASS] Dockerfile and .dockerignore are valid, deterministic and non-root.
[2/5] Checking Configuration Validator rules (Positive and Negative paths)...
[PASS] Configuration Validator enforces all security and domain constraints.
[3/5] Checking ASGI application creation and health/readiness endpoints...
[PASS] Liveness and Readiness probes operational.
[4/5] Checking scripts/entrypoint.py pre-flight execution...
[PASS] Entrypoint dry-run succeeded cleanly.
[5/5] Synthesizing results...

>>> ALL O.13 DEPLOYMENT AUTOMATION VALIDATIONS PASSED SUCCESSFULY. <<<
```

### Full Regression Suite:
```bash
python -m pytest -v
```
**Resultado:**
```
========================= 2536 passed, 18 skipped, 211 warnings in 213.28s (0:03:33) =========================
```
- Total de pruebas en suite global: 2554
- Pruebas exitosas: 2536 (Línea base previa 2517 + 19 nuevos tests de Q.4)
- Pruebas omitidas (skipped condicionales): 18
- Pruebas fallidas: 0 (Cero regresiones)

---

## 11. CONCLUSIÓN & NEXT STEPS
- **Hito Q.4 — Mission Dashboard:** 🟢 **IMPLEMENTADO Y VALIDADO AL 100%**.
- Cumple con todas las restricciones de aislamiento multi-tenant, seguridad RBAC, semántica `UNKNOWN`, exclusión Anti-CoT, cronología unificada K.1/K.2 y arquitectura consultiva de sólo lectura.
- **Git Hygiene:** No se realizaron commits ni pushes de acuerdo al protocolo.
- **Siguiente Paso en el Roadmap:** Proceder con **Q.5 — Agent Cost Dashboard** (análisis consultivo de costos de inferencia LLM y tokens por misión/agente/tenant).
