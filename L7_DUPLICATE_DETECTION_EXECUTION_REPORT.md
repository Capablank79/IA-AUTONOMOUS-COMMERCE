# L.7 Duplicate Detection — Execution Report

## 1. Resumen Ejecutivo

- **Capacidad / Tarea**: L.7 — Duplicate Detection (Data Quality y Governance / Transversal L)
- **Estado**: 🟢 VALIDADA
- **Fecha**: 2026-09-03
- **Test Suite Results**:
  - Tests Unitarios (`test_l7_duplicate_detection_unit.py`): **16 passed**
  - Tests Integración & E2E (`test_l7_duplicate_detection_integration.py`): **10 passed**
  - Regresión Suite L.6 (`test_l6_entity_resolution_unit.py` + `test_l6_entity_resolution_integration.py`): **27 passed**
  - Regresión Suite L.1–L.5: **140 passed**
  - Regresión Completa Pytest: **1372 passed, 1 skipped, 0 failures, 0 errors** (Baseline previo: 1321 passed).
- **Higiene Git**:
  - `.pytest_tmp`: Limpio / no rastreado
  - `git diff --check`: 0 errores de formato/whitespace
  - NO commits realizados
  - NO push realizado

---

## 2. Matriz de Reutilización / Discovery

| Capacidad Requerida | Localización en Base de Código | Estrategia (REUSE / EXTEND / CREATE) | Justificación |
|---|---|---|---|
| **Entity Resolution (`L.6`)** | `src/domain/entity_resolution/`, `src/application/entity_resolution/` | **REUSE** | L.7 reutiliza los `canonical_entity_id` y `MatchStatus` de L.6 para discriminar entidades distintas antes de evaluar si los registros son hechos duplicados. |
| **Reliability / Idempotency (`K.7`)** | `src/infrastructure/reliability/reliability_infrastructure.py` | **REUSE** | Reutilización de conceptos de idempotencia y contratos de reloj/infraestructura determinista (`VirtualClock`, `SystemClock`). |
| **Data Provenance & Lineage (`L.2`)** | `src/domain/data_provenance/` | **REUSE** | Trazabilidad causal y de procedencia sin pérdida de referencias ni colapso de linajes independientes. |
| **Security Sanitization (`K.8`)** | `src/domain/security/sanitization.py` | **REUSE** | Sanitización recursiva de secretos y exclusión de claves sensibles (`SENSITIVE_KEYS`) al generar fingerprints semánticos. |
| **Modelos de Dominio L.7** | `src/domain/duplicate_detection/models.py` | **CREATE** | Modelado inmutable (`DuplicateCandidate`, `DuplicateDetectionResult`, `DuplicateDetectionPolicy`, `DuplicateGroup`, `DuplicateStatus`, `DuplicateReasonCode`). |
| **Fingerprint Semántico SHA-256** | `src/domain/duplicate_detection/models.py` | **CREATE** | Algoritmo determinista SHA-256 con ordenamiento canónico, normalización NFKC, formateo canónico de números/fechas y exclusión de secretos y ruido técnico. |
| **Servicio de Deduplicación L.7** | `src/application/duplicate_detection/service.py` | **CREATE** | Motor determinista `DuplicateDetectionService` con evaluación en pares y por lote, respetando ventanas temporales y fuentes independientes. |
| **Persistencia JSON Durable L.7** | `src/infrastructure/persistence/data/json/duplicate_detection_repository.py` | **CREATE** | Persistencia atómica crash-safe (`.tmp` + `fsync` + `os.replace`), verificación criptográfica SHA-256 en lectura y locking multihilo (`threading.RLock`). |

---

## 3. Principio Rector: SAME ENTITY != DUPLICATE

L.6 y L.7 abordan preguntas ontológicamente distintas y complementarias:
- **L.6 (Entity Resolution)** responde: *“¿Estas referencias representan la misma entidad lógica canónica?”*
- **L.7 (Duplicate Detection)** responde: *“¿Estos registros concretos representan el mismo hecho lógico repetido?”*

### Demostración de Casos Críticos:
1. **Mismo producto, observaciones temporales distintas**:
   - Producto ID canónico `canon_prod_100`.
   - Registro A: Precio lunes $100 (Obs $T_0$).
   - Registro B: Precio lunes $100 (Obs $T_1$, $T_1 - T_0 > \text{ventana temporal}$).
   - **Resultado L.7**: `NOT_DUPLICATE` (`DuplicateReasonCode.SAME_ENTITY_DISTINCT_TEMPORAL_EVENT`). Se preserva la serie histórica válida.
2. **Mismo producto, fuentes independientes**:
   - Producto ID canónico `canon_prod_100`.
   - Registro A: Proveedor Alpha reporta stock = 50.
   - Registro B: Proveedor Beta reporta stock = 50.
   - **Resultado L.7**: `NOT_DUPLICATE` (`DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE`). Se preservan ambas fuentes como evidencia independiente.
3. **Replay de registro lógico idéntico**:
   - Mismo ID o clave de idempotencia + mismo payload semántico normalizado.
   - **Resultado L.7**: `REPLAY_DUPLICATE` (`DuplicateReasonCode.REPLAY_PAYLOAD_MATCH`). Se garantiza idempotencia total sin mutaciones secundarias.

---

## 4. Respuestas a la Auditoría Formal (Sección 15)

1. **¿Se reutilizó dedupe existente?**
   - **SÍ**. Se reutilizaron los identificadores y resoluciones de L.6 Entity Resolution y los principios de idempotencia de K.7 Reliability, evitando duplicar infraestructura base.
2. **¿Same entity se confunde con duplicate?**
   - **NO**. Registros de una misma entidad se evalúan rigurosamente contra sus condiciones semánticas, temporales y de procedencia antes de declararse duplicados.
3. **¿Historia legítima puede colapsarse?**
   - **NO**. La política de deduplicación soporta `temporal_window_seconds`, protegiendo la serie temporal de observaciones idénticas en instantes legítimos separados.
4. **¿Cross-source evidence se pierde?**
   - **NO**. Observaciones de múltiples fuentes no se marcan automáticamente como duplicadas salvo que la política explícitamente declare `allow_cross_source_duplicates=True`. Cada registro preserva su `source_id` y `provenance_id`.
5. **¿Replay es determinista?**
   - **SÍ**. El cálculo de fingerprint semántico SHA-256 es 100% determinista, canónico y reproducible entre ejecuciones y reinicios.
6. **¿UNKNOWN puede convertirse en duplicate?**
   - **NO**. La presencia de payloads vacíos, datos incompletos o esquemas no evaluables produce estrictamente `DuplicateStatus.UNKNOWN`, el cual permanece estrictamente diferenciado de `NOT_DUPLICATE` y `DUPLICATE`.
7. **¿Se borran originals?**
   - **NO**. Los registros originales nunca se eliminan ni se sobrescriben. Los duplicados se relacionan como tuplas inmutables en `DuplicateGroup`.
8. **¿Se eligió winner?**
   - **NO**. L.7 no realiza selección de ganadores ("winners"), no fusiona atributos ni resuelve discrepancias entre valores discrepantes.
9. **¿L.8 fue invadido?**
   - **NO**. Se respetó estrictamente la frontera con Conflict Resolution (L.8). Ante discrepancias de precios o stock entre fuentes sobre una misma entidad, L.7 emite `NOT_DUPLICATE` y preserva ambos registros intactos para su posterior resolución en L.8.
10. **¿Secrets entran al fingerprint?**
    - **NO**. La función `compute_semantic_fingerprint` filtra recursivamente cualquier clave sensible perteneciente al catálogo de seguridad (`token`, `secret`, `password`, `key`, `authorization`, etc.).

---

## 5. Resumen de Pruebas Ejecutadas

### Tests Unitarios (`tests/unit/test_l7_duplicate_detection_unit.py`) — 16/16 Passed:
1. `test_01_exact_duplicate`: Detección determinista de duplicado semántico exacto.
2. `test_02_replay_duplicate`: Identificación de replay por idempotencia con mismo payload.
3. `test_03_same_entity_different_event`: Validación de que misma entidad en distinto evento no es duplicado.
4. `test_04_different_entity`: Validación de entidades canónicas distintas como `NOT_DUPLICATE`.
5. `test_05_different_source`: Protección de evidencias independientes entre distintas fuentes.
6. `test_06_temporal_distinction`: Preservación de observaciones fuera de la ventana temporal.
7. `test_07_deterministic_fingerprint`: Verificación de consistencia del hash SHA-256 ante distinto orden de claves.
8. `test_08_unknown_status_preservation`: Preservación de `UNKNOWN` ante datos insuficientes (`UNKNOWN != NOT_DUPLICATE`).
9. `test_09_possible_duplicate_status_preservation`: Diferenciación estricta de `POSSIBLE_DUPLICATE != DUPLICATE`.
10. `test_10_l6_match_reused`: Integración con resolución de entidades canónicas de L.6.
11. `test_11_l6_no_match_propagation`: Manejo de entidades no coincidentes de L.6.
12. `test_12_policy_versioning`: Soporte de versionado SemVer en políticas de deduplicación.
13. `test_13_checksum_and_tampering`: Detección explícita de corrupción física mediante SHA-256.
14. `test_14_idempotency`: Idempotencia de persistencia y consistencia de checksums.
15. `test_15_no_destructive_merge`: Comprobación de que `DuplicateGroup` no muta ni borra candidatos.
16. `test_16_no_l8_conflict_resolution_logic`: Preservación de conflictos de valor sin elegir ganador.

### Tests de Integración y E2E (`tests/integration/test_l7_duplicate_detection_integration.py`) — 10/10 Passed:
- **Escenario A**: Registro de proveedor idéntico importado dos veces $\rightarrow$ `DUPLICATE`.
- **Escenario B**: Mismo producto, nuevo instante de observación $\rightarrow$ `NOT_DUPLICATE`.
- **Escenario C**: Replay de registro lógico con idéntica clave $\rightarrow$ `REPLAY_DUPLICATE`.
- **Escenario D**: Misma entidad, fuentes independientes $\rightarrow$ `NOT_DUPLICATE` (evidencias independientes).
- **Escenario E**: Entidades canónicas distintas L.6 $\rightarrow$ `NOT_DUPLICATE`.
- **Escenario F**: Incertidumbre L.6 `UNKNOWN`/`POSSIBLE` $\rightarrow$ No genera falsos duplicados.
- **Escenario G**: Persistencia y reinicio de repositorio $\rightarrow$ Mismos resultados intactos.
- **Escenario H**: Repositorio alterado/tampered $\rightarrow$ `CorruptedDuplicateDetectionRecordError` detectado.
- **Escenario I**: Replay concurrente con 10 hilos $\rightarrow$ Exactamente 1 resultado y grupo lógico persistido.
- **Escenario J (E2E Transversal)**: Flujo completo `Source Registry (L.1) -> Data Provenance (L.2) -> Schema Validation (L.5) -> Entity Resolution (L.6) -> Duplicate Detection (L.7)`.

---

## 6. Estado del Proyecto

- **L.1 Source Registry**: 🟢 VALIDADA
- **L.2 Data Provenance**: 🟢 VALIDADA
- **L.3 Freshness / TTL**: 🟢 VALIDADA
- **L.4 Confidence Model**: 🟢 VALIDADA
- **L.5 Schema Validation**: 🟢 VALIDADA
- **L.6 Entity Resolution**: 🟢 VALIDADA
- **L.7 Duplicate Detection**: 🟢 VALIDADA
- **L.8 Conflict Resolution**: ⚪ PENDIENTE
- **Gate K**: ⚪ PENDIENTE
- **Hito L**: 🟡 EN PROGRESO

**NEXT TASK**: `L.8 — Conflict Resolution`
