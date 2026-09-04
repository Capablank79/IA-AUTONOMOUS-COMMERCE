# REPORTE DE EJECUCIÓN: M.4 — CACHING

**Fecha de Ejecución:** 2026-09-03  
**Transversal:** M — Control de Coste e Inferencia  
**Hito:** M.4 — Caching  
**Estado:** 🟢 VALIDADA  
**Baseline previo:** 1468 passed, 1 skipped (0 failures, 0 errors)  
**Resultado actual:** 1491 passed, 1 skipped (0 failures, 0 errors)  
**Commits:** NO commit / NO push realizado (según instrucciones)  

---

## 1. RESUMEN EJECUTIVO

Se ha diseñado, implementado y validado exhaustivamente el hito **M.4 — Caching**, componente central del **Transversal M — Control de Coste e Inferencia**.

M.4 responde formalmente a la pregunta arquitectónica:
> *“¿Podemos reutilizar de forma segura un resultado previo para evitar una inferencia redundante?”*

La solución implementada proporciona un subsistema de caché de inferencia determinista, auditable, crash-safe y semánticamente seguro (`InferenceCacheService`, `InMemoryCacheRepository`, `JsonCacheRepository`), apoyado en modelos inmutables y congelados (`CacheKey`, `CacheEntry`, `CacheLookupRequest`, `CacheLookupResult`, `CacheStoreRequest`, `CachePolicy`), plenamente integrado con los hitos previos **M.1** (Model Routing), **M.2** (Context Budgeting), **M.3** (Prompt Compression) y **K.7** (Reliability / `ClockPort`).

---

## 2. MATRIZ DE REUTILIZACIÓN Y DESCUBRIMIENTO (Discovery Matrix)

| CAPABILITY | LOCATION | REUSE / EXTEND / CREATE | DESCRIPCIÓN |
|---|---|---|---|
| Routing & Route Models | `src/domain/model_routing/` | **REUSE** | Integración directa con `ModelRoute` y `RoutingDecision`. |
| Sanitization & Deep Freeze | `src/domain/model_routing/models.py` | **REUSE** | Sanitización de secretos (`SENSITIVE_KEYS`) y congelamiento recursivo de payloads. |
| Time / Deterministic Clocks | `src/domain/reliability/ports.py` | **REUSE** | Uso de `ClockPort` (K.7) para cálculo determinista de expiración y TTL sin acoplamiento a datetime de sistema. |
| Pipeline M.1 -> M.2 -> M.3 | `src/application/` | **REUSE** | Cadena canónica: Enrutamiento -> Presupuesto -> Compresión -> Inferencia / Caching. |
| Cache Domain Models & Keys | `src/domain/caching/models.py` | **CREATE** | `CacheKey`, `CacheEntry`, `CacheLookupResult`, `CacheLookupStatus`, `CacheEvictionReason`, `CacheIntegrityError`, hashing SHA-256 canónico. |
| Cache Ports & Interfaces | `src/domain/caching/ports.py` | **CREATE** | Puertos formales `CacheRepositoryPort` e `InferenceCacheServicePort`. |
| In-Memory Cache Store | `src/infrastructure/persistence/data/in_memory/cache_repository.py` | **CREATE** | Adaptador thread-safe en memoria con `threading.RLock`. |
| Crash-Safe JSON Cache Store | `src/infrastructure/persistence/data/json/cache_repository.py` | **CREATE** | Persistencia JSON atómica (`.tmp -> fsync -> os.replace`) con detección de manipulación física / checksum mismatch. |
| Inference Cache Service | `src/application/caching/inference_cache_service.py` | **CREATE** | Orquestador de caching: fingerprinting, lookup, validation, TTL check, store con validación estricta de cacheabilidad. |

---

## 3. ARQUITECTURA Y ESPECIFICACIÓN TÉCNICA

### 3.1 Modelos de Dominio Inmutables (`src/domain/caching/models.py`)
- **`CacheLookupStatus`**: Estados exhaustivos `HIT`, `MISS`, `EXPIRED`, `INVALID`, `UNKNOWN`, `ERROR`.
- **`CacheEvictionReason`**: Razones estructuradas de invalidación/descarte (`TTL_EXPIRED`, `CHECKSUM_MISMATCH`, `POLICY_VERSION_MISMATCH`, `MODEL_MISMATCH`, `UNKNOWN_STATUS`, `INVALID_ENTRY`, `MANUAL_INVALIDATION`, `SECURITY_CONTEXT_MISMATCH`).
- **`CacheIntegrityError`**: Excepción de dominio ante corrupción física o discrepancia en el checksum SHA-256.
- **`CachePolicy`**: Declaración de TTL por defecto, versionado, banderas `allow_caching` y parámetros de expiración.
- **`CacheKey` / `compute_cache_key`**: Generación canónica determinista:
  $$\text{CacheKey} = \text{SHA-256}(\text{normalized\_request} \parallel \text{route\_id} \parallel \text{model\_version} \parallel \text{inference\_params} \parallel \text{tool\_schemas} \parallel \text{policy\_version} \parallel \text{security\_context\_id})$$
- **`CacheEntry`**: Registro inmutable con checksum SHA-256 sobre contenido y metadatos sanitizados.

### 3.2 Seguridad Semántica y Prevención de False HITs
- **Inputs deterministas**: La clave de caché excluye timestamps de runtime, UUIDs aleatorios y memory addresses. Emplea serialización JSON canónica (`sort_keys=True`, `separators=(",", ":")`).
- **Aislamiento Multi-Tenant / Seguridad**: Soporte estricto para `security_context_id`. Contextos de seguridad diferentes para la misma consulta producen claves de caché distintas y deniegan acceso cross-tenant.
- **Sanitización de Secretos**: Los campos sensibles (`api_key`, `Authorization`, `token`, `password`, `secret`, `credentials`) son omitidos o redactados recursivamente antes de calcular firmas y almacenar metadatos.

### 3.3 Reglas Estrictas de Cacheabilidad
Se rechaza el almacenamiento (`store()`) de:
- Resultados con `status` `ERROR` o `UNKNOWN`.
- Operaciones con efectos secundarios comerciales o de ejecución (`has_side_effects=True`).
- Respuestas incompletas o denegadas por políticas.

### 3.4 Persistencia Crash-Safe y Resistencia a Corrupción
- El repositorio `JsonCacheRepository` implementa escritura atómica con descarga forzada a disco (`os.fsync`) y reemplazo atómico a nivel de sistema de archivos (`os.replace`).
- Cada archivo `.json` de caché contiene un campo `checksum` SHA-256. Si el archivo es modificado o manipulado externamente, el repositorio detecta la discrepancia, lanza `CacheIntegrityError`, desaloja la entrada corrupta y el servicio reporta `INVALID` con razón `CHECKSUM_MISMATCH`.

### 3.5 Seguridad en Concurrencia
- Ambas implementaciones de repositorio (`InMemoryCacheRepository` y `JsonCacheRepository`) y el servicio `InferenceCacheService` utilizan bloqueos reentrantes (`threading.RLock`) para garantizar que múltiples peticiones idénticas simultáneas no causen condiciones de carrera ni corrupción.

---

## 4. RESPUESTAS A LA AUDITORÍA OBLIGATORIA (Sección 20)

1. **¿Existía caché previa?**
   - No existía una capa de caché de inferencia tipada en el dominio de LLM. Se reutilizó la abstracción de confiabilidad `ClockPort` (K.7) y la lógica de sanitización de secretos de M.1.
2. **¿Se reutilizó?**
   - Sí, se reutilizaron las estructuras de M.1 (`ModelRoute`), M.2 (`ContextBudgetDecision`), M.3 (`CompressedContextPayload`), sanitización recursiva y `ClockPort`.
3. **¿La clave de caché es determinista?**
   - Sí, generada mediante serialización canónica ordenada y SHA-256 sin elementos no deterministas (no timestamps de runtime, no UUIDs aleatorios, no `hash()` nativo).
4. **¿Puede haber false HIT?**
   - No. Cualquier cambio en prompt, parámetros, esquemas de tools, modelo, versión de política o contexto de seguridad altera el SHA-256 y produce `MISS`.
5. **¿La caché puede saltarse Policy o Security?**
   - No. Los efectos secundarios están explícitamente bloqueados de ser cacheados (`has_side_effects`), y las políticas comerciales / K.7 mantienen autoridad exclusiva sobre la ejecución de acciones.
6. **¿UNKNOWN / ERROR se cachea?**
   - No. El método `store()` rechaza expresamente estados de error o incertidumbre.
7. **¿Una entrada expirada puede ser HIT?**
   - No. La evaluación contra el `ClockPort` retorna inmediatamente `EXPIRED` y desaloja la entrada.
8. **¿Se pueden persistir secretos?**
   - No. Todos los metadatos y payloads pasan por sanitización recursiva previa (`sanitize_routing_data`).
9. **¿Es posible la fuga cross-context / cross-tenant?**
   - No. El `security_context_id` forma parte integral del cómputo de la clave de caché.
10. **¿Se implementó M.5 / M.6 accidentalmente?**
    - No. M.5 (Model Selection by Task) y M.6 (Cost-aware Decision Policy) no fueron tocados y permanecen en estado `⚪ PENDIENTE`.

---

## 5. EVIDENCIA DE PRUEBAS Y VERIFICACIÓN

### 5.1 Pruebas Unitarias (`tests/unit/test_m4_caching_unit.py`) — 14/14 PASSED
- `test_deterministic_cache_key`: Verifica consistencia de clave ante permutaciones de diccionarios.
- `test_same_request_hit`: Verifica lookup HIT con payload idéntico.
- `test_changed_request_miss`: Verifica MISS ante cambios mínimos en el prompt.
- `test_changed_model_route_miss`: Verifica MISS ante cambio de modelo/proveedor.
- `test_changed_policy_version_miss`: Verifica invalidación por cambio de versión de política.
- `test_expiration_ttl_no_hit`: Verifica que entradas con TTL vencido retornan `EXPIRED`.
- `test_error_not_cached`: Verifica bloqueo de almacenamiento para respuestas de error.
- `test_unknown_not_cached`: Verifica bloqueo de almacenamiento para respuestas `UNKNOWN`.
- `test_secure_metadata_and_secret_sanitization`: Verifica sanitización de api_keys/tokens en metadata y entry.
- `test_checksum_integrity_validation`: Verifica cálculo y comprobación de checksum SHA-256.
- `test_corruption_handling_json_repo`: Verifica desalojo automático y reporte de `INVALID` ante manipulación de disco.
- `test_concurrency_safety`: Verifica concurrencia con 20 hilos simultáneos sin colisiones ni corrupción.
- `test_no_side_effect_bypass`: Verifica rechazo de caché en operaciones con efectos secundarios.
- `test_tenant_security_context_isolation`: Verifica aislamiento multi-tenant por `security_context_id`.

### 5.2 Pruebas de Integración y E2E (`tests/integration/test_m4_caching_integration.py`) — 9/9 PASSED
- `test_scenario_a_pipeline_miss_store_hit`: Pipeline completo M.1 -> M.2 -> M.3 -> M.4 (1º MISS + inferencia, 2º HIT sin inferencia).
- `test_scenario_b_same_prompt_different_route_miss`: Mismo prompt pero diferente ruta produce MISS y claves distintas.
- `test_scenario_c_compressed_context_changes_cache_key`: El contexto comprimido por M.3 genera una clave canónica representativa del input real.
- `test_scenario_d_expired_entry_recompute`: Expiración temporal vía `ClockPort` gatilla recomputación de inferencia.
- `test_scenario_e_unknown_and_error_not_reusable`: Respuestas `UNKNOWN`/`ERROR` no se almacenan y exigen inferencia en reintentos.
- `test_scenario_f_durable_json_store_restart`: Preservación durable y recarga de índice en disco post-reinicio.
- `test_scenario_g_tampered_entry_eviction`: Detección de corrupción física en repositorio JSON, desalojo seguro y nuevo cálculo.
- `test_scenario_h_concurrent_identical_requests`: Múltiples hilos concurrentes compartiendo caché de forma atómica.
- `test_e2e_mission_decision_inference_caching`: Flujo E2E completo demostrando reducción de llamadas al mock a exactamente 1.

### 5.3 Regresión Total del Sistema
```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\JLLV\Desktop\IA-AUTONOMOUS-COMMERCE
configfile: pyproject.toml
plugins: anyio-4.14.2
collected 1492 items

=============== 1491 passed, 1 skipped, 211 warnings in 46.30s ================
```

---

## 6. HIGIENE Y ESTADO DE REPOSITORIO

- `git ls-files .pytest_tmp`: Limpio.
- `git diff --check`: Sin errores ni espacios residuales.
- `git status --short`: No se realizaron commits ni pushes.

---

## 7. ESTADO DE GANTT Y PRÓXIMO PASO

- **M.1 Model Routing Strategy**: 🟢 VALIDADA
- **M.2 Context Budgeting**: 🟢 VALIDADA
- **M.3 Prompt Compression**: 🟢 VALIDADA
- **M.4 Caching**: 🟢 VALIDADA
- **M.5 Model Selection by Task**: ⚪ PENDIENTE (Próxima tarea a ejecutar)
- **M.6 Cost-aware Decision Policy**: ⚪ PENDIENTE
- **Transversal M — Control de Coste e Inferencia**: 🟡 EN PROGRESO
- **Gate L**: ⚪ PENDIENTE

**Próxima tarea:** `M.5 — Model Selection by Task` (esperando instrucción explícita sin implementar anticipadamente).
