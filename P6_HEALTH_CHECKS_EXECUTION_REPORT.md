# P.6 HEALTH CHECKS — EXECUTION & VALIDATION REPORT
**Production Health, Liveness, Readiness & Dependency Status**
**Hito P — Production / Operations**
**Date:** 2026-09-12
**Status:** 🟢 VALIDATED

---

## 1. ROADMAP ALIGNMENT & SCOPE
P.6 implementa la arquitectura y los contratos técnicos para responder con precisión y determinismo:
> *"¿Puede un orquestador u operador determinar de forma segura si el proceso está vivo, si la aplicación puede atender tráfico y qué dependencia crítica impide readiness?"*

- **Scope acotado**: Exclusivamente P.6. Cero implementación de P.7 (Monitoring), P.8 (Alerting), uptime services externos, paging ni Gate O.
- **Principio de Reutilización (REUSE > EXTEND > CREATE)**:
  - Reutilización y extensión de probes de O.13 (`/health`, `/healthz`, `/ready`, `/readyz`).
  - Reutilización de validación de esquema Alembic de P.3 (`check_schema_compatibility`, `DatabaseConfig`, `DatabaseConnectionFactory`).
  - Reutilización del aislamiento de perfiles de entorno P.2 (`ApplicationEnvironment`, `DeploymentConfig`).
  - Respeto a las políticas de sanitización y minimización de datos (N.9 / N.5).

---

## 2. ARCHITECTURAL SEPARATION: LIVENESS VS READINESS VS DEGRADED

| Probe / Concepto | Endpoint(s) | Semántica Operacional | Dependencias Evaluadas | HTTP Status |
|---|---|---|---|---|
| **Liveness** | `/health`, `/healthz` | *"¿El proceso está vivo?"* | Ninguna externa (independiente de PostgreSQL, LLM, Mercado Libre, redes). | **200 OK** (mientras el proceso no esté corrupto) |
| **Readiness** | `/ready`, `/readyz` | *"¿La aplicación está en condiciones de atender tráfico?"* | Dependencias críticas reales: Almacenamiento persistente escribible, PostgreSQL accesible, esquema Alembic sincronizado con HEAD, startup completado. | **200 OK** (si critical dependencies están saludables/degraded opcional) / **503 Service Unavailable** (si alguna crítica falla o está en UNKNOWN) |
| **Degraded** | Proyectado en `/ready` | *"Servicio operativo con proveedores secundarios afectados."* | Proveedores LLM, Mercado Libre API sandbox, facturación externa. | **200 OK** (con status `degraded` y lista de checks detallada) |

---

## 3. DEPENDENCY CLASSIFICATION & HEALTH MATRIX

| Check Name | Classification | Failure Impact on Readiness | Failure Impact on Liveness | Fallback / Policy |
|---|---|---|---|---|
| **Startup** | `CRITICAL` | HTTP 503 | HTTP 200 | Bloquea tráfico hasta finalizar inicialización completa |
| **Shutdown** | `CRITICAL` | HTTP 503 | HTTP 200 | Bloquea tráfico durante drenado y apagado ordenado |
| **Storage** | `CRITICAL` | HTTP 503 | HTTP 200 | Verificación write/read en `DATA_DIR` con centinela efímero y cleanup inmediato |
| **Database** | `CRITICAL` | HTTP 503 | HTTP 200 | `SELECT 1` + `check_schema_compatibility()`. Obligatorio en Production/Staging. |
| **LLM Provider** | `OPTIONAL` | HTTP 200 (`degraded`) | HTTP 200 | Fallo externo no derriba tráfico core de la plataforma |
| **Marketplace API** | `OPTIONAL` | HTTP 200 (`degraded`) | HTTP 200 | Aislado de readiness general |

---

## 4. INVARIANTS & SECURITY ASSURANCES
1. **UNKNOWN != HEALTHY**: La falta de evidencia o un fallo en la recolección de métricas de una dependencia crítica evalúa inmediatamente como `UNHEALTHY`/`UNKNOWN`, retornando HTTP 503.
2. **No Auto-Migrations**: Los probes de readiness únicamente leen el estado del catálogo (`alembic_version` vs HEAD); jamás invocan `upgrade` o `downgrade` de esquema.
3. **Strict Credential Sanitization**:
   - Sanitización automática de URLs y DSNs (`sanitize_dsn`, `sanitize_error_message`).
   - Cero exposición de contraseñas, hostnames internos o stack traces en respuestas públicas o logs.
4. **Environment Awareness**: Estricto en entornos `production` y `staging`, tolerante en `development` y `testing`.

---

## 5. TEST SUITE EXECUTION & EVIDENCE

### Unit Tests (`tests/unit/test_p6_health_checks_unit.py`)
16/16 tests PASSED:
- `test_1_liveness_healthy`: Liveness determinista HTTP 200.
- `test_2_readiness_healthy`: Readiness HTTP 200 con storage y DB listos.
- `test_3_db_failure_does_not_kill_liveness`: Caída de DB no afecta liveness (200).
- `test_4_db_failure_fails_readiness`: Caída de DB retorna 503.
- `test_5_schema_mismatch_fails_readiness`: Esquema desactualizado retorna 503.
- `test_6_storage_failure_fails_readiness`: Storage no escribible retorna 503.
- `test_7_optional_dependency_degraded`: Falla en LLM/opcional retorna `degraded` con 200.
- `test_8_unknown_critical_fails_readiness`: UNKNOWN en dependencia crítica retorna 503.
- `test_9_sanitized_response`: Cero credenciales ni secretos en payload.
- `test_10_environment_included_safely`: Entorno reportado de forma limpia.
- `test_11_timeout_handled`: Latencias registradas con precisión milimétrica.
- `test_12_startup_initializing_not_ready`: Retorna 503 durante inicialización.
- `test_13_no_auto_migration_invoked`: Comprueba que `run_upgrade` jamás es llamado.
- `test_14_correct_http_semantics`: Semántica HTTP 200 vs 503 validada.
- `test_15_no_secret_leakage_in_models`: Modelos inmutables libres de secretos.
- `test_16_no_p7_plus_dependencies`: Cero importaciones de módulos futuros P.7+.

### Integration Tests (`tests/integration/test_p6_health_checks_integration.py`)
10/10 tests PASSED contra PostgreSQL local real y endpoints ASGI:
- `test_scenario_a_real_postgres_available`: PostgreSQL real activo → `/health` 200, `/ready` 200.
- `test_scenario_b_db_unavailable_fails_readiness_only`: Simulación controlada sin tocar DB real → `/health` 200, `/ready` 503.
- `test_scenario_c_schema_current`: Esquema actual compatible → 200.
- `test_scenario_d_schema_incompatible_fails_readiness`: Esquema desincronizado → 503.
- `test_scenario_e_storage_unavailable`: Storage inaccesible → 503.
- `test_scenario_f_restart_startup_transition`: Transición startup → ready.
- `test_scenario_g_dr_recovered_db`: Validación post-recuperación DR P.5.
- `test_scenario_h_response_contains_no_credentials`: Sanitización integral en endpoints HTTP.
- `test_scenario_i_production_policy_strict`: Política estricta en producción.
- `test_scenario_j_optional_provider_failure_does_not_kill_core_readiness`: Degradación limpia.

### Regression Suites
- **P.3 Migrations, P.4 Backups, P.5 Disaster Recovery**: 49 passed in 9.89s.
- **Deploy Validation (`scripts/deploy_validate.py`)**: 5/5 validation checks PASSED.
- **Full Test Suite**: **2335 passed, 2 skipped, 0 failures** in 150.73s.

---

## 6. HYGIENE & STATUS SUMMARY
- Git status: Limpio, sin artefactos temporales ni credenciales rastreadas.
- NO commit.
- NO push.
- **P.6 Health Checks**: 🟢 VALIDADA.
