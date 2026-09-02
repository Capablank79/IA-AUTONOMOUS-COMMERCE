# HITO K.7 — RELIABILITY: REPORTE FORMAL DE EJECUCIÓN Y VALIDACIÓN

**Fecha:** 2026-09-02  
**Módulo:** Transversal K — Observability, Evaluation & Reliability (`K.7 Reliability`)  
**Estado:** `🟢 VALIDADA`  
**Autor:** Antigravity / TraeAI Pair Programmer  

---

## 1. STATUS
- **Capacidad K.7 Reliability:** `🟢 VALIDADA`
- **Capacidades K.1 a K.6:** `🟢 VALIDADA`
- **Capacidad K.8 Security Checks:** `🟡 EN PROGRESO` (Intacta / No tocada)
- **Gate J:** `⚪ PENDIENTE`
- **Regresión Completa:** `1122 passed, 1 skipped, 0 failures` (100% pass) en 49.06s.

---

## 2. ROADMAP & GANTT RECONCILIATION
- **Definition of Done satisfecha:**
  1. Taxonomía canónica explícita de fallos (11 categorías) y grados de recuperabilidad (4 categorías).
  2. Bounded Retry Policy determinista con backoff exponencial, límites máximos y respeto estricto a `Retry-After`.
  3. Retry Safety estricto: operaciones con efectos secundarios ante `TIMEOUT` o `UNKNOWN` prohíben reintentos a ciegas y fuerzan verificación/reconciliación previa contra el estado real para evitar duplicaciones.
  4. Deduplicación e Idempotencia fuerte por clave y hash SHA-256 de payload, con detección explícita de `CONFLICT` ante mutaciones en el payload sin sobreescritura silenciosa.
  5. Circuit Breaker (`CLOSED`, `OPEN`, `HALF_OPEN`) con bypass rápido de dependencias degradadas y soporte de reloj virtual (`VirtualClock`).
  6. Preservación estricta del valor epistemológico de `UNKNOWN` (`UNKNOWN != SUCCESS` y `UNKNOWN != FAILURE confirmado`).
  7. Aislamiento de fallos en dependencias no críticas (Audit y Trace) para no abortar resultados de negocio válidos.
  8. Recuperación y durabilidad ante caídas y reinicios (`crash/restart`) con persistencia atómica en disco (`.tmp` + `fsync` + `os.replace`).
  9. Respeto inviolable a la barrera de gobernanza de `PolicyEngine` (cero bypass en reintentos).
  10. Integración desacoplada y no intrusiva con K.1 Audit Trail (`AuditRecord`) y K.2 Agent Trace (`AgentTraceRecord`).
  11. Cero dependencias de caos externo, cero sleeps reales en tests y límites claros respecto a K.8 (Security Checks) y Hito P (Operaciones productivas).

---

## 3. DISCOVERY & REUSE MATRIX

| CAPABILITY | EXISTING LOCATION | COVERAGE | REUSE / EXTEND / CREATE |
|---|---|---|---|
| **Audit Trail** | `src/domain/audit/`, `src/application/audit/` | Registro append-only inmutable con sanitización de secretos | **REUSE** (Emisión desacoplada de `AuditRecord` en decisiones de confiabilidad) |
| **Agent Trace** | `src/domain/agent_trace/`, `src/application/agent_trace/` | Registro observable de pasos de ejecución | **REUSE** (Emisión desacoplada de `AgentTraceRecord` en pasos de reintento/reconciliación) |
| **Policy Engine** | `src/domain/policy/`, `src/application/policy/` | Evaluación de reglas de gobernanza y riesgos | **REUSE** (Inviolabilidad de políticas: DENY/UNKNOWN bloquea ejecución) |
| **Continuous Mission** | `src/application/continuous_mission/` | Ciclos autónomos periódicos con auto-stop | **REUSE / INTEGRATE** (Integración de orquestación resiliente) |
| **Event Bus** | `src/application/events/` | Despacho in-process idempotente | **REUSE** (Validación de entrega idempotente) |
| **Failure Taxonomy** | N/A | No unificada formalmente a nivel transversal | **CREATE** (`src/domain/reliability/models.py`) |
| **Retry & Recovery Models** | N/A | Parcialmente disperso en adaptadores ad-hoc | **CREATE** (`src/domain/reliability/models.py`) |
| **Reliability Engine & Ports**| N/A | Inexistente de forma centralizada y desacoplada | **CREATE** (`src/domain/reliability/ports.py`, `src/application/reliability/reliability_engine.py`) |
| **Circuit Breaker & Idempotency**| In-memory disperso | No estandarizado con reloj determinista ni storage durable | **CREATE** (`src/infrastructure/reliability/reliability_infrastructure.py`) |

---

## 4. FAILURE TAXONOMY
Clasificación formal en `src/domain/reliability/models.py`:

```python
class FailureCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHORIZATION = "AUTHORIZATION"
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    CORRUPTION = "CORRUPTION"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"

class FailureRecoverability(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    UNKNOWN = "UNKNOWN"
```

Mapeo determinista de recuperabilidad:
- `TRANSIENT`, `RATE_LIMIT`, `DEPENDENCY_UNAVAILABLE` $\rightarrow$ `RETRYABLE`
- `TIMEOUT`, `UNKNOWN` $\rightarrow$ `RECONCILIATION_REQUIRED` (en operaciones con efectos secundarios) / `RETRYABLE` (en lecturas puras)
- `PERMANENT`, `AUTHORIZATION`, `VALIDATION`, `CONFLICT`, `CORRUPTION`, `CANCELLED` $\rightarrow$ `NON_RETRYABLE`

---

## 5. RETRY POLICY & DETERMINISTIC BACKOFF
- Modelo inmutable `RetryPolicy`:
  - `max_attempts: int = 3`
  - `initial_delay_seconds: float = 0.5`
  - `max_delay_seconds: float = 10.0`
  - `backoff_multiplier: float = 2.0`
  - `timeout_seconds: Optional[float] = 30.0`
  - `retryable_categories: Tuple[FailureCategory, ...]`
  - `require_idempotency_for_side_effects: bool = True`
- Algoritmo de cálculo de delay acotado:
  $$\text{delay} = \min(\text{initial\_delay} \times \text{multiplier}^{\text{attempt}-1}, \text{max\_delay})$$
- Respeto prioritario a la cabecera / valor `Retry-After` de proveedores externos si excede el delay calculado.
- Cero `time.sleep()` real en pruebas gracias a la inyección de `ClockPort` / `VirtualClock`.

---

## 6. RETRY SAFETY & RECONCILIATION
- **Operaciones de sólo lectura (`is_side_effect=False`):** Fallos por timeout o transitorios pueden ser reintentados de forma segura según la política.
- **Operaciones con efectos secundarios (`is_side_effect=True`):**
  - Un `TIMEOUT` o error `UNKNOWN` **no** equivale a que la acción falló en el destino.
  - El sistema **prohíbe el reintento ciego** y exige invocar la función de reconciliación (`reconcile_func`) para inspeccionar el estado externo antes de decidir si reintenta o retorna el estado ya aplicado.
  - Ejemplo: ante timeout en publicación de producto, el motor reconcilia consultando si la publicación existe; si ya existe, recupera el ID generado y retorna `SUCCESS` evitando publicaciones duplicadas.

---

## 7. STRONG IDEMPOTENCY & CONFLICT DETECTION
- Implementación de `IdempotencyStorePort`:
  - `InMemoryIdempotencyStore`: Gestión thread-safe en memoria mediante bloqueos de exclusión mutua.
  - `JsonIdempotencyStore`: Almacenamiento persistente en disco con escrituras atómicas (`.tmp` $\rightarrow$ `fsync` $\rightarrow$ `os.replace`).
- Garantías contractualmente probadas:
  - **Misma clave + Mismo payload:** Retorna inmediatamente el resultado previamente computado (`ReliabilityStatus.SUCCESS` con `is_cached=True`).
  - **Misma clave + Payload diferente:** Arroja explícitamente error `IdempotencyConflictError` (`FailureCategory.CONFLICT`), impidiendo sobreescrituras silenciosas o corrupción de estado.

---

## 8. CIRCUIT BREAKER
- Modelo `InMemoryCircuitBreaker`:
  - Estados: `CircuitBreakerStatus.CLOSED`, `OPEN`, `HALF_OPEN`.
  - Configuración: `failure_threshold: int = 3`, `recovery_time_seconds: float = 30.0`, `half_open_success_threshold: int = 1`.
  - Transiciones deterministas:
    - `CLOSED` $\rightarrow$ Acumula 3 fallos consecutivos $\rightarrow$ Pasa a `OPEN`.
    - `OPEN` $\rightarrow$ Tras `recovery_time_seconds` en reloj virtual $\rightarrow$ Pasa a `HALF_OPEN`.
    - `HALF_OPEN` $\rightarrow$ Ejecución exitosa de prueba $\rightarrow$ Vuelve a `CLOSED` y resetea contadores.
    - `OPEN` $\rightarrow$ Rechaza llamadas entrantes inmediatamente con `FailureCategory.DEPENDENCY_UNAVAILABLE` sin saturar la dependencia caída.

---

## 9. FAILURE ISOLATION & DEGRADED MODE
- **Aislamiento de servicios periféricos:** El fallo en la persistencia de un registro de auditoría (`AuditTrailService`) o traza de agente (`AgentTraceService`) es capturado internamente y no aborta ni destruye una operación comercial que concluyó con éxito.
- **Modo Degradado:** El runtime decide de forma estructurada continuar, degradar o detener la ejecución basándose en la criticidad de la dependencia y la respuesta del Circuit Breaker.

---

## 10. CRASH / RESTART & DURABILITY
- Se demostró que las operaciones registradas en `JsonIdempotencyStore` persisten de manera durable en disco tras reiniciar el proceso de ejecución.
- Al reiniciar el motor de confiabilidad y recargar el almacén JSON desde disco, la deduplicación de operaciones previas se mantiene intacta con cero duplicación de efectos secundarios.

---

## 11. UNKNOWN SEMANTICS PRESERVATION
- Regla ontológica absoluta:
  - $\text{UNKNOWN} \neq \text{SUCCESS}$
  - $\text{UNKNOWN} \neq \text{FAILURE confirmado}$
- Si una operación falla de forma no recuperable bajo incertidumbre, el estado final del resultado preserva rigurosamente `ReliabilityStatus.UNKNOWN` o `FailureCategory.UNKNOWN`, alertando la necesidad de auditoría humana o reconciliación posterior.

---

## 12. AUDIT & AGENT TRACE INTEGRATION
- Toda ejecución gobernada por `ReliabilityEngine` genera:
  1. Pasos de traza en `AgentTraceService` (`AgentTraceStepType.SERVICE_CALL`, `RETRY`, `FAILURE`, etc.) correlacionando `execution_id`, `correlation_id` y `causation_id`.
  2. Registros inmutables en `AuditTrailService` (`AuditActionType.RESULT_RECORDED` o `FAILURE_RECORDED`) con sanitización recursiva de tokens y credenciales.
  3. Exclusión total de Chain-of-Thought (CoT) y prompts internos.

---

## 13. POLICY ENGINE BOUNDARY
- `ReliabilityEngine` respeta estrictamente los veredictos de `PolicyEngine`.
- Si una acción recibe una denegación de política (`POLICY_DENIED`) o incertidumbre de política (`POLICY_UNKNOWN`), el motor no realiza reintentos automáticos espurios, preservando la barrera de gobernanza inquebrantable.

---

## 14. FAULT INJECTION & TEST SUITE COVERAGE

### 14.1 Tests Unitarios (`tests/unit/test_k7_reliability_unit.py` - 10/10 Passed)
- `test_failure_taxonomy_classification`: Validación de categorías y recuperabilidad.
- `test_retry_policy_delay_computation`: Backoff exponencial determinista y respeto a `Retry-After`.
- `test_circuit_breaker_state_transitions`: Ciclo `CLOSED -> OPEN -> HALF_OPEN -> CLOSED` con reloj virtual.
- `test_reliability_transient_retry_success`: Reintento exitoso tras fallo transitorio.
- `test_reliability_permanent_non_retry`: Detención inmediata ante fallo permanente.
- `test_idempotency_caching_and_conflict_detection`: Cacheo de resultado y detección de conflicto ante payload divergente.
- `test_side_effect_timeout_reconciliation`: Reconciliación exitosa ante timeout en mutación externa.
- `test_unknown_semantics_preservation`: Preservación estricta de incertidumbre.
- `test_sanitization_of_secrets_in_reliability_metadata`: Enmascaramiento de secretos y tokens en metadatos y evidencia.
- `test_json_idempotency_store_durability_and_recovery`: Persistencia atómica crash-safe en disco y recuperación post-reinicio.

### 14.2 Tests de Integración (`tests/integration/test_k7_reliability_integration.py` - 9/9 Passed)
- `test_scenario_1_transient_failure_retry_success`: Fallo 503 transitorio recuperado en intento 2.
- `test_scenario_2_permanent_failure_no_useless_retry`: Fallo 400 permanente detenido sin reintentos inútiles.
- `test_scenario_3_timeout_side_effect_reconciliation_no_duplicate`: Timeout en publicación externa reconciliado sin duplicar publicación.
- `test_scenario_4_duplicate_event_one_effect`: Deduplicación de eventos duplicados en EventBus.
- `test_scenario_5_crash_restart_recovery`: Recuperación segura tras pérdida de memoria de proceso.
- `test_scenario_6_degraded_dependency_circuit_breaker`: Aislamiento de servicio externo caído vía Circuit Breaker.
- `test_scenario_7_retry_exhausted_explicit_status`: Estado explícito `RETRY_EXHAUSTED` tras agotar intentos máximos.
- `test_scenario_8_policy_engine_boundary_never_bypassed`: Bloqueo estricto ante rechazo de gobernanza de `PolicyEngine`.
- `test_scenario_9_concurrent_idempotency`: Ejecución concurrente multihilo de la misma operación con exactamente una ejecución real (`assert calls == 1`).

### 14.3 Test End-to-End (`tests/unit/test_k7_reliability_e2e.py` - 1/1 Passed)
- `test_e2e_resilient_mission_workflow`: Flujo E2E completo integrando Autonomous Mission $\rightarrow$ Policy Engine $\rightarrow$ Reliability Engine $\rightarrow$ External Marketplace Mock con Timeout $\rightarrow$ Reconciliación $\rightarrow$ Registro en Audit Trail y Agent Trace.

---

## 15. SECOND PASS AUDIT

| Pregunta de Auditoría | Respuesta | Evidencia / Justificación |
|---|:---:|---|
| 1. ¿Retry puede duplicar publicación/venta? | **NO** | Operaciones con efectos secundarios exigen idempotencia y reconciliación antes de reintentar ante timeouts. |
| 2. ¿Timeout puede convertirse falsamente en failure? | **NO** | Timeout en side-effect se trata como `UNKNOWN` y se verifica/reconcilia contra el estado real. |
| 3. ¿UNKNOWN puede convertirse en success? | **NO** | Semántica estricta `UNKNOWN != SUCCESS`; sólo transiciona a éxito si la reconciliación confirma la ejecución. |
| 4. ¿Evento duplicado puede ejecutar dos veces? | **NO** | Idempotencia por clave y validación de hash garantizan exactamente un efecto lógico. |
| 5. ¿Restart puede perder recovery? | **NO** | `JsonIdempotencyStore` persiste atómicamente en disco con `fsync` y recarga en frío. |
| 6. ¿Retry puede saltarse Policy? | **NO** | Reintentos operan dentro de la barrera y fallos de autorización/política son clasificados `NON_RETRYABLE`. |
| 7. ¿Auth failure puede generar retry storm? | **NO** | Clasificado categóricamente como `NON_RETRYABLE`. |
| 8. ¿Dependencia no crítica puede tumbar todo? | **NO** | Errores en Audit y Trace están aislados en bloques de captura no bloqueantes. |
| 9. ¿Corrupción puede cargarse como válida? | **NO** | Detección explícita de JSON corrupto y tipado estricto. |
| 10. ¿Misma idempotency key acepta payload distinto? | **NO** | Genera `IdempotencyConflictError` inmediato impidiendo colisiones y sobreescrituras silenciosas. |
| 11. ¿Hay loops infinitos? | **NO** | `max_attempts` acotado y Circuit Breaker con apertura determinista. |
| 12. ¿Retry puede continuar indefinidamente? | **NO** | Límite estricto de intentos que finaliza en `RETRY_EXHAUSTED`. |

---

## 16. REGRESIÓN COMPLETA Y ESTADO FINAL DE GIT

- **Resultado Pytest:** `1122 passed, 1 skipped, 211 warnings in 49.06s`
- **Delta vs Baseline:** $+20$ tests nuevos pasando ($1102 \rightarrow 1122$), $0$ fallos, $0$ errores.
- **Git Tracking Check:**
  - `git ls-files .pytest_tmp` $\rightarrow$ Vacío.
  - `git status --short | Select-String ".pytest_tmp|.pytest_cache|.runtime"` $\rightarrow$ Cero artefactos de runtime en Git.
  - `git diff --check` $\rightarrow$ Aprobado (sin conflictos de whitespace ni marcadores de fusión).

---

## 17. CONCLUSIÓN Y PRÓXIMOS PASOS

- **Hito K.7 Reliability:** Marcado como **`🟢 VALIDADA`** en la Carta Gantt Maestra.
- **Próxima Tarea:** **K.8 — Security Checks transversal** (Manteniendo Hito K en progreso y Gate J pendiente).
- **Control de Versiones:** Cero commits y cero pushes ejecutados conforme a las instrucciones del usuario.
