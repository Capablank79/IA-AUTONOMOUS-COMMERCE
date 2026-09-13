# P.9 — LOG RETENTION EXECUTION REPORT
## RETENTION POLICIES, ROTATION, PURGE & SAFE OPERATIONAL LOG LIFECYCLE

**Hito:** Hito P — Production / Operations  
**Tarea:** P.9 — Log Retention  
**Estado:** 🟢 VALIDADA  
**Fecha:** 2026-09-12  
**Baseline Inicial:** 2387 passed, 2 skipped, 0 failures  
**Resultado Final Suite Global:** 2403 passed, 2 skipped, 0 failures  
**Operaciones Git:** NO commit, NO push  

---

### 1. ROADMAP ALIGNMENT & RESPONSABILIDAD
P.9 responde formalmente a la pregunta canónica de arquitectura:
> *"¿Puede la plataforma conservar logs y evidencia operativa durante períodos definidos, rotarlos y purgarlos de forma segura sin eliminar información crítica ni retener datos sensibles indefinidamente?"*

Se implementó el ciclo de vida operacional completo gobernado por políticas inmutables y deterministas, distinguiendo explícitamente entre registros de auditoría K.1 (inmutables y protegidos) y logs operacionales depurables/rotables (P.7, P.8, K.2, Filesystem y PostgreSQL).

---

### 2. DISCOVERY & STORAGE MAPPING
Se identificaron y mapearon los almacenes de datos operacionales reales de la plataforma:

| DATA CLASS | STORAGE LOCATION / REPO | DEFAULT RETENTION | PROTECTED RULES / SAFETIES | REUSE / EXTEND / CREATE |
|---|---|---|---|---|
| **APPLICATION_LOG** | Filesystem `.runtime/logs/{env}/app.log*` | 7 días | Rotación por tamaño (10MB) o time, backup count configurable | EXTEND / CREATE |
| **ACCESS_LOG** | Filesystem `.runtime/logs/{env}/access.log*` | 7 días | Rotación por tamaño (10MB), aislamiento de tenant si aplica | EXTEND / CREATE |
| **ERROR_LOG** | Filesystem `.runtime/logs/{env}/error.log*` | 14 días | Preservación ampliada para depuración de fallos | EXTEND / CREATE |
| **TRACE_LOG** | `JsonAgentTraceRepository` (`traces/*.json`) | 7 días | `protect_latest=True`, correlación K.2 preservada | REUSE & EXTEND |
| **MONITORING_SAMPLE** | `JsonMetricRepository` (`monitoring/{env}/metrics_samples.json`) | 3 días | Preservación de ventanas operativas recientes (5m, 1h, 24h) | REUSE & EXTEND |
| **ALERT_HISTORY** | `JsonProductionAlertRepository` (`alerts/{env}/alerts.json`) | 14 días | **Alertas ACTIVE y ACKNOWLEDGED NUNCA se purgan por edad** (solo RESOLVED < cutoff) | REUSE & EXTEND |
| **AUDIT_RECORD** | `JsonAuditRepository` (`audit/{tenant}/audit.jsonl`) | 365 días / Permanente | **Protegido incondicionalmente (`RetentionAction.PROTECT`)** salvo regla legal explícita | REUSE & EXTEND |
| **POSTGRES_OPERATIONAL** | Tablas operacionales PostgreSQL (p.ej. `alert_history`) | Por política de entorno | Batching seguro en transacciones SQL parametrizadas | EXTEND |

---

### 3. DOMINIO & MODELO DE POLÍTICAS
- `RetentionClass`: Enum canónico para las 7 clases de datos operacionales.
- `RetentionAction`: Enum (`KEEP`, `PURGE`, `ROTATE`, `PROTECT`, `SKIP_CORRUPT`).
- `RetentionPolicy`: DTO inmutable con `environment`, `data_class`, `retention_days`, `purge_batch_size`, `protect_latest`, `dry_run` y método `calculate_cutoff(now: datetime) -> datetime` en UTC estricto.
- `DefaultRetentionPolicyRegistry`: Registro centralizado de políticas por entorno (`DEVELOPMENT`, `STAGING`, `PRODUCTION`, `TESTING`).
- `LogRetentionService`: Orquestador de aplicación para consultas de estado, simulaciones dry-run y purgas atómicas seguras.

---

### 4. SALVAGUARDAS ARQUITECTÓNICAS Y SEGURIDAD
1. **Audit Evidence Protection (`LOG != AUDIT`):** Los registros de `AuditRecord` (K.1) son preservados por defecto. `AuditLogRetentionStore` emite `RetentionAction.PROTECT` ante cualquier solicitud de purga rutinaria.
2. **Active Incident Protection:** Las alertas P.8 en estado `ACTIVE` o `ACKNOWLEDGED` son excluidas de cualquier purga sin importar su antigüedad. Solo alertas `RESOLVED` con timestamp anterior al corte UTC son elegibles.
3. **Environment Isolation (P.2):** Purgar logs en entorno `DEVELOPMENT` jamás afecta ni evalúa datos en `STAGING` o `PRODUCTION`.
4. **Tenant Scoping:** Purgas con scope de `tenant_id` filtran estrictamente por tenant; Tenant A no puede alterar ni purgar datos de Tenant B.
5. **Filesystem Safety (Path Traversal & Symlink Defense):**
   - Validación estricta con `validate_safe_identifier` (regex `^[a-zA-Z0-9_-]+$`).
   - Resolución canónica `path.resolve()` verificando que ningún archivo o symlink escape del directorio raíz aprobado `.runtime/logs/`.
6. **Time & Determinismo:** Todos los cálculos se realizan sobre objetos `datetime` con `timezone.utc`.
7. **Dry Run:** Subcomando determinista `--dry-run` que reporta elegibles y cortes sin modificar el sistema de archivos ni las bases de datos.
8. **Idempotencia:** Ejecutar una purga de forma consecutiva reporta 0 nuevos eliminados (`purged_count=0`) sin fallos.

---

### 5. CLI CANÓNICO `scripts/log_retention.py`
Proporciona los subcomandos:
- `status`: Inspecciona las clases de log y muestra volumen, fecha más antigua/reciente y elegibles para purga.
- `dry-run`: Evalúa políticas y simula el ciclo de retención sin alterar archivos ni registros.
- `purge`: Ejecuta la purga y rotación atómica y segura según las políticas de retención.

---

### 6. RESULTADOS DE VALIDACIÓN Y SUITES DE PRUEBAS
- **Unit Tests (`tests/unit/test_p9_log_retention_unit.py`):** 11 passed (100%).
- **Integration Tests (`tests/integration/test_p9_log_retention_integration.py`):** 5 passed (100%) cubriendo escenarios A a J.
- **Regresión P.7, P.8, K.1, K.2:** 110 passed (100%).
- **Validaciones de Despliegue y Migraciones:**
  - `python scripts/deploy_validate.py`: PASS (5/5 checks exitosos).
  - `python scripts/db_migrate.py check`: PASS (Schema UP TO DATE).
- **Regresión Global de la Plataforma:**
  - Baseline anterior: 2387 passed, 2 skipped, 0 failures.
  - Baseline actual: **2403 passed, 2 skipped, 0 failures** (+16 tests nuevos P.9).
- **Higiene de Repositorio:** `git diff --check` limpio, sin secrets ni artefactos temporales en control de versiones.

---

### 7. ACTUALIZACIÓN GANTT
- **P.9 Log Retention:** 🟢 VALIDADA
- **Hito P — Production / Operations:** 🟡 EN PROGRESO
- **Gate O:** ⚪ PENDIENTE
