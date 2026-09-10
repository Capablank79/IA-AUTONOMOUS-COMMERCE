# O.6 Usage Metering — Execution & Architecture Report

## 1. Discovery & Existing Capabilities Matrix

| Capability | Location | Current Purpose | O.6 Gap | Reuse / Extend / Create |
|---|---|---|---|---|
| Tenant Isolation | `src/domain/tenant/` | Aislamiento físico y lógico por tenant | No contenía agregado de métricas de uso | **REUSE** (`TenantContext`, `CrossTenantGuard`) |
| Identity & Org | `src/domain/organization/` | Dimensiones de usuario y organización | Enlace canónico de actor/membership | **REUSE** (`Organization`, `UserMembership`) |
| Model Gateway Facts | `src/domain/model_gateway/` | Emisión de facts estructurados de inferencia | Consumo desacoplado de hechos para agregación | **REUSE** (`ModelGatewayResponse`, `ProviderRequestReference`) |
| Cost Tracking | `src/domain/cost/` | Observación y ledger de costos de inferencia | Agregación multidimensional SaaS | **REUSE** (Aritmética Decimal, distinción estimated vs actual) |
| Usage Metering Domain | `src/domain/usage_metering/` | No existía modelo inmutable para eventos y agregados SaaS | Definición formal de `UsageEvent`, `UsageAggregate`, `UsageQuery` | **CREATE** (`models.py`, `ports.py`) |
| Bridge O.5 -> O.6 | `src/application/usage_metering/` | Desacoplamiento de hechos de gateway hacia eventos de uso | Mapeo determinista sin raw prompts | **CREATE** (`model_gateway_bridge.py`) |
| Metering Service & Repo | `src/infrastructure/persistence/data/json/` & `src/application/usage_metering/` | Persistencia append-only particionada y agregaciones | Almacenamiento tenant-scoped y cálculo determinista | **CREATE** (`usage_event_repository.py`, `usage_metering_service.py`) |

---

## 2. K.3 Reuse & Boundaries

- **Separación de Responsabilidades**: K.3 provee los patrones canónicos para el tracking y observación de costos por inferencia unitaria. O.6 se encarga de la **agregación SaaS multidimensional** (tenant, organización, usuario/identidad, modelo, proveedor, tipo de tarea y período de tiempo).
- **Aritmética Exacta**: Todo cálculo de costes utiliza `Decimal` de alta precisión, distinguiendo explícitamente entre `estimated_cost` y `actual_cost`.
- **UNKNOWN Semantics**: Los valores no reportados (`UNKNOWN` / `None`) se conservan sin mutarlos forzosamente a `0.00` o `0 tokens`.

---

## 3. Usage Event Model & Fact vs Aggregate Pattern

- **`UsageEvent` (Hecho atómico inmutable)**:
  - `usage_event_id`: Identificador determinista/único con prefijo seguro `use_evt_...`.
  - `tenant_id`: Identificador del tenant propietario (obligatorio).
  - `identity_id` / `organization_id` / `session_id`: Referencias canónicas opcionales.
  - `provider` / `model` / `task_type`: Dimensiones canónicas de IA.
  - `request_status`: `SUCCESS`, `FAILED`, `CACHED`, `BLOCKED`.
  - `cache_status`: `HIT`, `MISS`, `BYPASS`.
  - `input_tokens` / `output_tokens` / `total_tokens`: Conteo exacto reportado.
  - `estimated_cost` / `actual_cost`: Registro monetario `Decimal`.
  - `occurred_at`: Marca temporal obligatoria con zona horaria UTC.
  - `checksum`: Digest SHA-256 criptográfico para detección de manipulación en reposo.
- **`UsageAggregate` (Proyección calculada)**:
  - Proyección derivada calculada bajo demanda mediante `aggregate_usage()`.
  - Los eventos históricos son estrictamente append-only y nunca se modifican para ajustar totales.

---

## 4. Tenant Isolation

- **Particionamiento Físico en Disco**: Almacenado en `tenants/{tenant_id}/usage/events/{usage_event_id}.json`.
- **CrossTenantGuard Enforcement**: Cada invocación a `record_usage_event`, `get_event` o `aggregate_usage` exige un `TenantContext` válido coincidente con el `tenant_id` objetivo.
- **Imposibilidad de Fugas Cross-Tenant**: Se bloquea todo intento de consulta cruzada (`CrossTenantAccessError`), y ningún tenant puede listar o agregar eventos de otro tenant.

---

## 5. Token & Cost Accounting

- **Preservación de Hechos Reales**: O.6 no infiere tokens ficticios ni recalcula inferencias.
- **UNKNOWN Token Handling**: Si los tokens no están presentes en el fact de origen, se registran como `None` y se computan métricas separadas en los agregados sin distorsionar el total conocido.
- **Precisión Monetaria**: Se evita por completo el uso de tipos de coma flotante (`float`), operando exclusivamente con `Decimal`.

---

## 6. Cache & Failed Requests Accounting

- **Cache HIT**: Registra `request_count = 1`, `cached_requests = 1`, `cache_status = HIT` y 0 tokens de proveedor adicionales sin inventar costes de inferencia.
- **Failed Requests**: Errores 429, timeouts, errores de autenticación con el proveedor o denegaciones de política registran `request_status = FAILED` y `failed_requests = 1`, preservando el estado real sin asumir consumos inexistentes.

---

## 7. Aggregation & Dimensions

- Dimensiones soportadas:
  - `tenant_id` (Obligatorio).
  - `identity_id` (Filtrado y desglose en `breakdown_by_identity`).
  - `organization_id` (Filtrado y desglose en `breakdown_by_organization`).
  - `provider` (Filtrado y desglose en `breakdown_by_provider`).
  - `model` (Filtrado y desglose en `breakdown_by_model`).
  - `task_type` (Filtrado y desglose en `breakdown_by_task_type`).
  - `start_time` / `end_time` (Límites temporales deterministas semiabiertos `[start, end)`).

---

## 8. Idempotency, Concurrency & Restart Safety

- **Deduplicación Criptográfica**: `append_event` verifica la presencia de `usage_event_id` o `source_reference`/`correlation_id`.
- **Manejo de Reintentos**:
  - Evento idéntico con mismo checksum -> Retorno idempotente sin duplicar conteos.
  - Evento en conflicto (mismo ID pero payload modificado) -> `UsageEventConflictError`.
- **Integridad y Detección de Manipulación**: Si un archivo JSON en disco es alterado sin recalcular el checksum, el repositorio lanza `UsageEventIntegrityError`.
- **Concurrencia Segura**: Bloqueo `threading.RLock()` en operaciones de lectura y escritura.
- **Persistencia y Reinicio**: Los totales agregados tras un reinicio de servicio (`JsonUsageEventRepository`) son exactamente reproducibles bit a bit.

---

## 9. Privacy & Minimization (N.9)

- **Cero Datos Sensibles**: `UsageEvent` y `UsageAggregate` no almacenan ni procesan prompts, completions, chain-of-thought, reasoning privado, credenciales ni PII.
- **Auditoría Segura**: Las entradas en `AuditTrail` (`RECORD_USAGE_EVENT`) contienen metadatos limpios y sanitizados.

---

## 10. Boundaries & O.7 Separation

- **O.6 NO decide si el consumo está permitido**: No implementa cuotas, no aplica throttling, no evalúa límites de saldo ni bloquea peticiones por sobreconsumo.
- **O.6 NO es Billing (O.8/O.9)**: No calcula precios de planes, facturas, impuestos, créditos ni balances de suscripción.

---

## 11. Test Results & Baseline Regression

- **Tests Unitarios O.6**: 16/16 PASSED (`tests/unit/test_o6_usage_metering_unit.py`).
- **Tests Integración & E2E O.6**: 11/11 PASSED (`tests/integration/test_o6_usage_metering_integration.py`).
- **Suite Completa de Regresión del Sistema**:
  - **Baseline inicial**: 1972 passed, 1 skipped, 0 failures.
  - **Resultado actual**: **1999 passed, 1 skipped, 0 failures, 0 errors** (100.13s).
- **Higiene de Repositorio**:
  - `git diff --check`: Limpio (0 errores de espacios o formato).
  - `git ls-files .pytest_tmp`: Limpio.
  - NO commits creados, NO push efectuado.

---

## 12. Estado del Roadmap

- O.1 Tenant Isolation → 🟢 VALIDADA
- O.2 Organizations / Users → 🟢 VALIDADA
- O.3 SaaS Authentication → 🟢 VALIDADA
- O.4 SaaS Authorization → 🟢 VALIDADA
- O.5 Model Gateway SaaS → 🟢 VALIDADA
- **O.6 Usage Metering → 🟢 VALIDADA**
- O.7 Quota Management → ⚪ PENDIENTE
- Hito O → 🟡 EN PROGRESO
- Gate N → ⚪ PENDIENTE
