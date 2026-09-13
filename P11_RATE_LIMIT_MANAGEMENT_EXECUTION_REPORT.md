# P.11 Rate-Limit Management Execution Report
**Runtime Throttling, Fairness, Burst Control & Provider Protection**

---

## 1. ROADMAP ALIGNMENT
- **Hito**: Hito P — Production / Operations
- **Task ID**: P.11 — Rate-limit Management
- **Estado**: 🟢 VALIDADA
- **Principio Rector**: `REUSE > EXTEND > CREATE`
  - Reutilización de cuotas O.7 (`QuotaRule`, `QuotaPolicy`), Model Gateway O.5, telemetría y métricas P.7 (`MetricSample`, `ProductionMonitoringService`), severidades e incidentes P.8 (`ProductionAlertingService`), aislamiento multi-entorno P.2 (`ApplicationEnvironment`) y contratos de tiempo determinista K.7 (`ClockPort`).
  - Delimitación estricta: NO se implementa Gate O ni Hito Q; NO se modifican planes ni límites de capacidad automáticamente; NO se crea un cluster distribuido innecesario ni se añade Redis.
- **Pregunta Arquitectónica Fundamental**:
  > *"¿Puede la plataforma limitar de forma justa y segura la tasa de requests/consumo para proteger tenants, providers y recursos compartidos sin confundir rate limits con quotas comerciales?"*

---

## 2. DISCOVERY MATRIX

| CAPABILITY | LOCATION | CURRENT OWNER | P.11 GAP | REUSE / EXTEND / CREATE |
|---|---|---|---|---|
| Commercial Quotas | `src/domain/quota_management/` | O.7 Quota Management | Períodos comerciales largos (mes/semana) sin control de ráfagas | REUSE O.7 (Policy / QuotaRule) |
| Capacity Headroom & Estimation | `src/domain/capacity_planning/` | P.10 Capacity Planning | No impone bloqueos en runtime caliente | REUSE P.10 (Señales / Thresholds) |
| Metrics Collection | `src/domain/monitoring/` | P.7 Monitoring | No tenía eventos específicos de throttling | EXTEND (MetricType / Labels) |
| Alerting & Incident Triggers | `src/domain/production_alerting/` | P.8 Alerting | No consumía spikes de denegación | REUSE P.8 Alert Rules |
| Runtime Throttling & Token Bucket | `src/domain/rate_limit/` | P.11 Rate Limiting | Inexistente como motor unificado | CREATE Domain & Application Engine |
| Upstream Provider Protection | `src/domain/rate_limit/` & O.5 | P.11 / O.5 Gateway | Sin pre-flight check de límites físicos de upstream | CREATE Pre-flight evaluation en RateLimitService |

---

## 3. RATE LIMIT MODEL & SCOPES

El módulo [models.py](file:///c:/Users/JLLV/Desktop/IA-AUTONOMOUS-COMMERCE/src/domain/rate_limit/models.py) define tipos canónicos inmutables y fuertemente tipados:

1. **`RateLimitScope`**:
   - `TENANT`: Límites asignados a la organización / cuenta.
   - `USER`: Límites por usuario individual dentro de un tenant (evita monopolio ruidoso).
   - `PROVIDER`: Límites físicos del upstream (ej. OpenAI, Anthropic, MercadoLibre API).
   - `MODEL`: Límites específicos por modelo de IA (ej. GPT-4 vs Claude-3-Haiku).
   - `ENDPOINT`: Límites por ruta o tipo de acción crítica.

2. **`RateLimitWindowUnit`**:
   - `SECOND` (1s), `MINUTE` (60s), `HOUR` (3600s), `DAY` (86400s), `CUSTOM`.

3. **`RateLimitRule`**:
   - `rule_id`, `limit_rate`, `window_unit`, `burst_capacity` ($\ge \text{limit\_rate}$), `scope`, `target_identifier`, `is_hard_limit`, `custom_window_seconds`.
   - Propiedad analítica: `refill_rate_per_second = limit_rate / window_seconds`.

4. **`RateLimitPolicy`**:
   - Agrupador de reglas asociadas a un tenant o globales con checksum determinista SHA-256 (`policy_checksum`) y flag explícito `is_unlimited`.

5. **`RateLimitDecision`**:
   - `status`: `ALLOW` / `DENY` / `UNKNOWN`.
   - `reason_code`: Código explicable seguro (`ALLOWED`, `BURST_EXCEEDED`, `PROVIDER_LIMIT_EXCEEDED`, `TENANT_LIMIT_EXCEEDED`, `USER_FAIRNESS_LIMIT_EXCEEDED`, `MISSING_POLICY_FAIL_CLOSED`, `CORRUPTED_POLICY_FAIL_CLOSED`, etc.).
   - Metadata de headers HTTP: `limit`, `remaining`, `reset_at`, `retry_after_seconds` (entero $\ge 0$).
   - Helpers: `http_status_code` (200 / 429), `get_http_headers()`, `to_http_headers()`.

---

## 4. ALGORITHM & BURST CONTROL

### Algoritmo: Token Bucket Continuo y Determinista
- **Capacidad de Ráfaga (`burst_capacity`)**: Número máximo de tokens disponibles en el bucket para absorber spikes legítimos de tráfico.
- **Tasa de Reposición Continua (`refill_rate_per_second`)**: Los tokens se reponen proporcionalmente al tiempo transcurrido $\Delta t$ medido mediante `ClockPort`:
  $$\text{tokens}_{\text{actual}} = \min(\text{burst\_capacity},\ \text{tokens}_{\text{previo}} + \Delta t \times \text{refill\_rate\_per\_second})$$
- **Consumo Atómico Multi-Regla**:
  Ante un request que coincide con $M$ reglas concurrentes (ej. Tenant Rule + User Rule + Provider Rule + Model Rule), el motor valida la disponibilidad en **todos** los buckets antes de descontar unidades. Si alguna regla carece de saldo, el request se deniega (`DENY`) y **ningún** token es descontado (prevención de fugas parciales y desincronización).

---

## 5. DETERMINISTIC RETRY-AFTER & HTTP SEMANTICS

- **Cálculo de `retry_after_seconds`**:
  Calculado deterministamente a partir del déficit de tokens y la tasa de reposición de la regla más restrictiva que causó la denegación:
  $$\text{retry\_after} = \left\lceil \frac{\text{cost\_units} - \text{tokens}_{\text{actual}}}{\text{refill\_rate\_per\_second}} \right\rceil$$
  Garantías:
  - Siempre es un número entero mayor o igual a 1 en denegaciones por falta de tokens.
  - Nunca es negativo.
  - No utiliza números aleatorios ni heurísticas de espera arbitrarias.
- **Headers HTTP**:
  - `X-RateLimit-Limit`: Límite máximo de la regla evaluada.
  - `X-RateLimit-Remaining`: Tokens restantes disponibles.
  - `X-RateLimit-Reset`: Timestamp UTC ISO-8601 del momento en que el bucket alcanzará su capacidad máxima.
  - `Retry-After`: Segundos enteros a esperar antes del siguiente intento (emitido en denegaciones 429).

---

## 6. CANONICAL KEYING & MULTI-TENANT ISOLATION

### Formato de Clave Canónica
$$\text{env:}\{\text{environment}\}\text{:tenant:}\{\text{tenant\_id}\}\text{:scope:}\{\text{scope}\}\text{:target:}\{\text{target\_id}\}\text{:rule:}\{\text{rule\_id}\}$$

- **Sanitización Estricta**: Cada segmento valida caracteres seguros (`^[a-zA-Z0-9_\-\.:]+$`), previniendo inyección de claves o colisiones de prefijo.
- **Aislamiento Multi-Entorno**: El tráfico de `DEV` y `STAGING` utiliza buckets con prefijo `env:development:` / `env:staging:`, imposibilitando cualquier consumo o contaminación sobre los buckets de `PROD`.
- **Aislamiento Multi-Tenant**: Tenant A saturando su límite no impacta el bucket ni el headroom de Tenant B.
- **User Fairness**: Si existe un usuario ruidoso dentro de Tenant A, su bucket `USER` bloquea sus solicitudes individuales sin agotar el bucket general de los demás usuarios del tenant.

---

## 7. UPSTREAM PROVIDER PROTECTION & PRE-FLIGHT EVALUATION

- **Pre-flight Pipeline**:
  `Auth / Session` $\rightarrow$ `Authorization (RBAC)` $\rightarrow$ `Commercial Quota (O.7)` $\rightarrow$ `Rate Limit Pre-Flight (P.11)` $\rightarrow$ `Upstream Provider Call (O.5)`.
- **Zero Provider Calls on Denial**:
  Si el bucket de `PROVIDER` o `MODEL` no cuenta con capacidad, la evaluación P.11 emite `DENY` antes de disparar la llamada de red hacia el proveedor de IA. Se probó y demostró formalmente que un request bloqueado resulta en **0** invocaciones físicas al backend/mock de inferencia.

---

## 8. QUOTA != RATE LIMIT & CAPACITY SEPARATION

1. **Separación con Quotas O.7**:
   - Quota O.7 gobierna asignaciones comerciales de mediano/largo plazo (ej. 10.000 requests/mes).
   - Rate Limit P.11 gobierna la velocidad instantánea de procesamiento y control de ráfaga (ej. 60 requests/minuto).
   - Un usuario puede tener saldo amplio de cuota comercial pero ser temporalmente limitado por rate limit (recibiendo 429 con Retry-After).
2. **Separación con Capacity Planning P.10**:
   - P.10 calcula tendencias y headroom macro de infraestructura.
   - P.10 **no** modifica automáticamente las reglas ni límites de P.11. Cualquier cambio requiere política explícita gobernada.

---

## 9. GOVERNANCE PRECEDENCE & FAIL-SAFE DESIGN

### Precedencia de Evaluación
1. **Platform Hard Limits** (Límites físicos globales de la plataforma).
2. **Provider Hard Limits** (Límites contractuales/técnicos de upstream providers).
3. **Plan / Policy Limits** (Límites del plan comercial asignado al tenant).
4. **Tenant Narrower Overrides** (Límites configurados por el tenant, válidos únicamente si son más restrictivos).

### Fail-Safe / Fail-Closed
- **Política Faltante (`missing_policy`)**: En entornos no tolerantes a degradación, la ausencia de política explícita **no** equivale a tráfico infinito ilimitado; el sistema deniega de forma segura (`DENY` / `MISSING_POLICY_FAIL_CLOSED`).
- **Política Ilegítima / Corrupta**: Ante fallos de integridad (checksum inválido), el sistema deniega (`DENY` / `CORRUPTED_POLICY_FAIL_CLOSED`) y audita el evento vía K.1.
- **Unlimited Explícito**: Tráfico ilimitado sólo es otorgado mediante declaración explícita `is_unlimited=True`.

---

## 10. PERSISTENCE, CONCURRENCY & TOCTOU PREVENTION

- **Doble Nivel de Store**:
  - `InMemoryRateLimitStateStore`: Implementación thread-safe con `threading.RLock()` para evaluación atómica conjunta en single-process / test execution.
  - `JsonRateLimitPolicyRepository`: Persistencia hexagonal durable y particionada por tenant en disco.
- **Prevención de TOCTOU (Time-Of-Check to Time-Of-Use)**:
  El chequeo y débito de tokens ocurre dentro de una sección crítica atómica. En pruebas de concurrencia masiva (múltiples hilos compitiendo por un único token restante en la frontera), sólo 1 hilo obtiene `ALLOW` y los restantes obtienen `DENY` de forma determinista, con cero sobre-suscripción.

---

## 11. MONITORING & ALERTING INTEGRATION (P.7 / P.8)

- **Telemetría P.7**:
  El adaptador `MonitoringRateLimitTelemetryAdapter` convierte decisiones de rate limiting en métricas para `MetricRepositoryPort` / `ProductionMonitoringService`:
  - `REQUEST_COUNT` / `QUOTA_DENIAL_COUNT` con etiquetas sanitizadas: `scope`, `status`, `reason`, `environment`.
- **Alertas P.8**:
  P.8 consume la métrica de denegaciones para detectar spikes anómalos (`RATE_LIMIT_DENIAL_SPIKE` / `PROVIDER_RATE_PRESSURE`) sin duplicar la lógica de alertas.

---

## 12. TEST SUITE & VERIFICATION EVIDENCE

### A. Pruebas Unitarias (`tests/unit/test_p11_rate_limit_management_unit.py`)
16 pruebas unitarias canónicas ejecutadas y pasadas al 100%:
1. `test_01_allow_below_limit`: Tráfico bajo el límite es permitido (`ALLOW`).
2. `test_02_deny_at_limit`: Agotamiento de capacidad resulta en `DENY` (429).
3. `test_03_reset_after_window`: Reposición completa de tokens tras el paso del tiempo mediante `ClockPort`.
4. `test_04_retry_after_correct`: Cálculo determinista, positivo y entero de `Retry-After`.
5. `test_05_burst_behavior`: Absorción correcta de ráfagas hasta `burst_capacity`.
6. `test_06_tenant_isolation`: Aislamiento estricto de buckets entre Tenant A y Tenant B.
7. `test_07_user_isolation`: Usuario ruidoso es bloqueado sin afectar a otros usuarios del mismo tenant.
8. `test_08_provider_scope`: Límites por proveedor protegen el upstream de forma independiente.
9. `test_09_model_scope`: Modelos distintos mantienen contadores separados.
10. `test_10_unlimited_explicit`: Política con `is_unlimited=True` permite consumo continuo.
11. `test_11_missing_policy_fail_closed`: Falta de política en modo estricto deniega de forma segura.
12. `test_12_concurrency_boundary`: Exclusión mutua ante competencia por el último token disponible.
13. `test_13_environment_isolation`: Tráfico en `DEV` no afecta buckets en `PROD`.
14. `test_14_quota_not_rate_limit`: Coexistencia desacoplada entre cuota comercial O.7 y rate limit P.11.
15. `test_15_no_provider_call_when_denied`: Cero llamadas a upstream cuando el pre-flight check deniega.
16. `test_16_no_gate_o_or_hito_q_imports`: Verificación de frontera arquitectónica limpia.

### B. Pruebas de Integración (`tests/integration/test_p11_rate_limit_management_integration.py`)
10 escenarios de integración A–J ejecutados y pasados al 100%:
- **Escenario A**: Tenant bajo límite es permitido.
- **Escenario B**: Exceso de tasa retorna `DENY` con status HTTP 429 y header `Retry-After`.
- **Escenario C**: Tenant B permanece completamente inafectado por saturación de Tenant A.
- **Escenario D**: Tras avance temporal en `FakeClock`, el tráfico vuelve a ser permitido (`ALLOW`).
- **Escenario E**: Hard limit de proveedor bloquea la llamada upstream antes de alcanzar la API externa.
- **Escenario F**: Límite de usuario más restrictivo frena al usuario ruidoso mientras otros continúan.
- **Escenario G**: Precedencia de plataforma sobre reglas locales del tenant.
- **Escenario H**: Concurrencia masiva con hilos paralelos sin sobre-suscripción de tokens.
- **Escenario I**: Adaptador emite telemetría a repositorio P.7 / alertas P.8 ante denegaciones.
- **Escenario J**: Política corrupta/ilegítima sigue fail-safe y deniega de forma segura.

### C. Regresiones Específicas P.7–P.10 / O.5 / O.7
- Suites ejecutadas: P.7 Monitoring (27 tests), P.8 Alerting (27 tests), P.9 Log Retention (24 tests), P.10 Capacity Planning (26 tests), O.7 Quota Management (27 tests), O.5 Model Gateway (22 tests).
- **Resultado**: 153 passed, 0 failures.

### D. Full Pytest Regression Baseline
- **Baseline Previo**: 2429 passed, 2 skipped, 0 failures.
- **Resultado Actual**: **2455 passed, 2 skipped, 0 failures** (26 nuevos tests canónicos añadidos).

---

## 13. DEPLOY & DATABASE VALIDATION

1. **`python scripts/deploy_validate.py`**:
   - `[PASS] ENVIRONMENT`: Configuración multi-entorno y secretos válidos.
   - `[PASS] SYSTEM INTEGRITY`: Integridad de contratos y servicios.
   - `[PASS] ASSETS & CLEANLINESS`: Higiene de código y ausencia de artefactos temporales.
   - `[PASS] MULTI-TENANT ISOLATION`: Políticas de aislamiento confirmadas.
   - `[PASS] SYSTEM CONTRACTS`: Contratos del dominio validados.
   - **Resultado Global**: `DEPLOY VALIDATION PASSED (5/5 checks passed)`.

2. **`python scripts/db_migrate.py check`**:
   - **Resultado**: `[OK] Migration state clean and consistent`.

---

## 14. SECURITY & HYGIENE AUDIT

- `git diff --check`: Limpio (sin trailing whitespaces ni conflictos).
- `git status --short`: Solo archivos nuevos/modificados pertenecientes a P.11 y documentación.
- `git ls-files .pytest_tmp`: Limpio (sin volcados temporales en git).
- Cero secretos, tokens API o volcados PII expuestos en logs o modelos.
- Gate O e Hito Q intactos y no inicializados.

---

## 15. ESTADO DEL HITO P & SIGUIENTE TAREA

```
P.1 CI/CD                  → 🟢 VALIDADA
P.2 Environment Separation  → 🟢 VALIDADA
P.3 Database Migrations     → 🟢 VALIDADA
P.4 Backups                → 🟢 VALIDADA
P.5 Disaster Recovery      → 🟢 VALIDADA
P.6 Health Checks          → 🟢 VALIDADA
P.7 Monitoring             → 🟢 VALIDADA
P.8 Alerting               → 🟢 VALIDADA
P.9 Log Retention          → 🟢 VALIDADA
P.10 Capacity Planning     → 🟢 VALIDADA
P.11 Rate-limit Management → 🟢 VALIDADA

Hito P (Production / Ops)   → 🟡 EN PROGRESO (Todas las tasks completadas)
Gate O                     → ⚪ PENDIENTE
```

**NEXT TASK**:
Gate O — Formal Hito P Validation
*(NO ejecutar Gate O. NO commit. NO push).*
