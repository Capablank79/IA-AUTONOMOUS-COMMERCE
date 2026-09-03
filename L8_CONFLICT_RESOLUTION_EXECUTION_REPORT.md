# L.8 — Conflict Resolution: Execution and Validation Report

## 1. Identificación del Componente y Estado

- **Componente**: L.8 — Conflict Resolution
- **Hito**: Hito Transversal L — Data Quality y Governance
- **Fecha de Validación**: 2026-09-03
- **Estado de L.8**: 🟢 VALIDADA
- **Estado de Gate K**: ⚪ PENDIENTE
- **Estado de Hito L**: 🟡 EN PROGRESO
- **Baseline Previo de Regresión**: 1372 passed, 1 skipped, 0 failures
- **Resultado Actual de Regresión**: 1399 passed, 1 skipped, 0 failures (27 tests nuevos: 18 unitarios + 9 integración/E2E)

---

## 2. Responsabilidad Ontológica y Alcance de L.8

L.8 responde a la pregunta fundamental:
> *“Cuando dos o más datos válidos sobre la misma entidad/hecho se contradicen, ¿cómo resolvemos el conflicto de forma explícita y reproducible?”*

### Principios Contractuales Cumplidos:
1. **REUSE > EXTEND > CREATE**: Reutilización no intrusiva de los componentes precedentes L.1 (Source Registry), L.2 (Data Provenance), L.3 (Freshness / TTL), L.4 (Confidence Model), L.5 (Schema Validation), L.6 (Entity Resolution) y L.7 (Duplicate Detection).
2. **Preservación Total de Evidencia**: Prohibición de sobreescritura destructiva o borrado de candidatos. `ConflictResolutionResult` preserva referencias completas a todos los candidatos evaluados, sus fuentes y sus valores originales.
3. **Cero Ganadores Hardcodeados**: No existen reglas arbitrarias globales ("Mercado Libre siempre gana" o "Supplier siempre gana"). Toda resolución se ejecuta conforme a una `ConflictResolutionPolicy` inmutable y versionada.
4. **Manejo Seguro de Incertidumbre y Empates**:
   - `UNKNOWN` en frescura o confianza nunca asume ser fresco ni de alta confianza.
   - Datos expirados son descalificados frente a datos frescos bajo estrategias de frescura.
   - Replays o duplicados (L.7) no inflan el consenso (1 fuente independiente = 1 voto máximo).
   - Empates sin regla de desempate explícita convergen de forma segura en `ConflictStatus.UNRESOLVED` con `selected_value = None`.
5. **Persistencia Crash-Safe y Determinismo Criptográfico**:
   - Escritura atómica (`.tmp` + `fsync` + `os.replace`), locking thread-safe (`threading.RLock`) y verificación de integridad física SHA-256 en lectura con detección de corrupción sin autorreparación silenciosa.
   - IDs y checksums calculados de forma determinista mediante ordenamiento canónico JSON y normalización Unicode NFKC (cero uso de `random`, `time` no acotado o `hash()` nativo).

---

## 3. Matriz de Capacidades y Reutilización (Discovery)

| Capacidad | Ubicación | Estrategia (Reuse / Extend / Create) |
|---|---|---|
| Fuentes y Catálogo | `src/domain/source_registry/` (L.1) | **REUSE**: `RegisteredSource`, `SourceType` |
| Trazabilidad y Linaje | `src/domain/data_provenance/` (L.2) | **REUSE**: `ProvenanceRecord`, `SubjectType` |
| Frescura y Caducidad TTL | `src/domain/freshness/` (L.3) | **REUSE**: `FreshnessStatus`, `FreshnessAssessment` |
| Modelo de Confianza | `src/domain/confidence/` (L.4) | **REUSE**: `ConfidenceLevel`, `ConfidenceAssessment` |
| Validación de Esquemas | `src/domain/schema_validation/` (L.5) | **REUSE**: `SchemaDefinition`, `SchemaValidationResult` |
| Identidad Canónica | `src/domain/entity_resolution/` (L.6) | **REUSE**: `CanonicalEntity`, `EntityIdentifier` |
| Detección de Duplicados | `src/domain/duplicate_detection/` (L.7) | **REUSE**: `DuplicateCandidate`, `DuplicateFingerprint` para anti-inflación |
| Modelos de Resolución L.8 | `src/domain/conflict_resolution/models.py` | **CREATE**: `ConflictCandidate`, `ConflictResolutionPolicy`, `ConflictResolutionResult`, `ConflictStatus`, `ResolutionStrategy`, `ConflictReasonCode` |
| Puertos de Repositorio L.8 | `src/domain/conflict_resolution/ports.py` | **CREATE**: `ConflictResolutionPolicyRepositoryPort`, `ConflictResolutionRepositoryPort` |
| Repositorio JSON Crash-Safe | `src/infrastructure/persistence/data/json/conflict_resolution_repository.py` | **CREATE**: `JsonConflictResolutionPolicyRepository`, `JsonConflictResolutionRepository` |
| Servicio de Resolución L.8 | `src/application/conflict_resolution/service.py` | **CREATE**: `ConflictResolutionService` |

---

## 4. Auditoría Contractual (Respuestas a Criterios del Item 22)

1. **¿L.8 duplica L.3/L.4/L.6/L.7?**
   - **NO**. L.8 no recalcula frescura ni confianza, ni re-resuelve identidades ni genera fingerprints de duplicados; consume los contratos de L.3, L.4, L.6 y L.7 respectivamente como entradas inmutables.
2. **¿Se preservan valores originales y evidencia?**
   - **SÍ**. `ConflictResolutionResult` almacena la lista inmutable `candidate_ids` y un diccionario de `evidence_details` con el estado completo de cada candidato evaluado, sin mutar registros fuente.
3. **¿Hay source winner hardcodeado?**
   - **NO**. La prioridad de fuentes se rige exclusivamente por `policy.source_precedence` definido en la política versionada inyectada.
4. **¿Duplicados y replays inflan el consenso?**
   - **NO**. `ConflictResolutionService` agrupa los votos por `(source_id, deduplication_fingerprint)`. Múltiples replays de la misma fuente o fingerprint cuentan como 1 único voto.
5. **¿UNKNOWN puede ganar sobre datos válidos?**
   - **NO**. Evaluaciones con `freshness_status = UNKNOWN` o `confidence_level = UNKNOWN` son penalizadas/descalificadas en las estrategias correspondientes (`FRESHEST` y `HIGHEST_CONFIDENCE`).
6. **¿Los empates quedan unresolved de forma segura?**
   - **SÍ**. Cuando dos candidatos tienen el mismo score/prioridad y no hay desempate aplicable, el estado resultante es `ConflictStatus.UNRESOLVED` con `reason_code = ConflictReasonCode.TIE_UNRESOLVED` y `selected_value = None`.
7. **¿Los resultados son deterministas?**
   - **SÍ**. Todas las operaciones de hashing, generación de IDs y evaluación de candidatos se basan en ordenamiento canónico, serialización JSON canónica y SHA-256.
8. **¿La corrupción física puede generar una resolución falsa?**
   - **NO**. Tanto el repositorio de políticas como el de resultados verifican el hash SHA-256 al leer el disco (`recompute -> compare`) y lanzan excepciones de corrupción explícitas ante manipulaciones de bytes.

---

## 5. Resumen de Ejecución de Pruebas

### 5.1. Pruebas Unitarias (`tests/unit/test_l8_conflict_resolution_unit.py`)
- `test_01_no_conflict_single_and_identical`: 🟢 PASSED (Caso candidato único o valores idénticos -> `NO_CONFLICT`)
- `test_02_conflicting_values_detected`: 🟢 PASSED (Detección de valores incompatibles en mismo campo)
- `test_03_source_priority`: 🟢 PASSED (Resolución determinista por precedencia de fuente configurada)
- `test_04_freshest_wins`: 🟢 PASSED (Candidato con menor `freshness_age_seconds` prevalece)
- `test_05_highest_confidence_wins`: 🟢 PASSED (Candidato con mayor `confidence_score` / nivel prevalece)
- `test_06_tie_leads_to_unresolved`: 🟢 PASSED (Empates sin desempate -> `UNRESOLVED` seguro)
- `test_07_missing_policy_unresolved`: 🟢 PASSED (Falta de política aplicable -> `UNKNOWN` / `UNRESOLVED`)
- `test_08_duplicate_votes_not_counted_twice`: 🟢 PASSED (Replays de fuente A no superan a fuente B)
- `test_09_consensus_resolves_when_supported`: 🟢 PASSED (Mayoría de fuentes independientes resuelve conflicto)
- `test_10_unknown_freshness_safe`: 🟢 PASSED (`UNKNOWN` en frescura no prevalece)
- `test_11_unknown_confidence_safe`: 🟢 PASSED (`UNKNOWN` en confianza no prevalece)
- `test_12_expired_vs_fresh`: 🟢 PASSED (Dato expirado es descartado frente a dato fresco)
- `test_13_deterministic_result`: 🟢 PASSED (Mismos candidatos y política producen idéntico checksum y resultado)
- `test_14_policy_versioning`: 🟢 PASSED (Políticas versionadas inmutables)
- `test_15_checksum_and_tampering`: 🟢 PASSED (Detección de manipulación de checksum)
- `test_16_idempotency`: 🟢 PASSED (Replay de resolución produce idéntico estado)
- `test_17_no_evidence_deletion`: 🟢 PASSED (Evidencia de candidatos se conserva intacta)
- `test_18_no_hidden_winner`: 🟢 PASSED (Sin política que justifique ganador, no se inventa valor)

### 5.2. Pruebas de Integración y E2E (`tests/integration/test_l8_conflict_resolution_integration.py`)
- `test_scenario_a_source_priority_winner`: 🟢 PASSED (Precedencia de fuentes en persistencia JSON)
- `test_scenario_b_fresh_vs_stale_under_freshness_policy`: 🟢 PASSED (Frescura con persistencia)
- `test_scenario_c_higher_vs_lower_confidence`: 🟢 PASSED (Confianza con persistencia)
- `test_scenario_d_two_equally_valid_contradictory_unresolved`: 🟢 PASSED (Empate contradictorio seguro)
- `test_scenario_e_duplicate_replayed_evidence_not_double_counted`: 🟢 PASSED (Protección anti-inflación L.7 persistida)
- `test_scenario_f_different_entities_or_facts_safe`: 🟢 PASSED (Campos y entidades distintas no generan conflicto)
- `test_scenario_g_restart_durability_and_reload`: 🟢 PASSED (Durabilidad tras reinicio en frío)
- `test_scenario_h_tampered_persistence_corruption_detected`: 🟢 PASSED (Detección de corrupción física SHA-256)
- `test_scenario_i_e2e_data_quality_governance_flow`: 🟢 PASSED (Flujo E2E completo: L.1 -> L.2 -> L.5 -> L.6 -> L.7 -> L.8)

### 5.3. Suite Completa del Proyecto (Pytest)
```
1399 passed, 1 skipped, 211 warnings in 43.77s (0 failures, 0 errors)
```

---

## 6. Higiene del Repositorio

- `git diff --check`: 0 errores de whitespace.
- Sin artefactos temporales en `.pytest_tmp`.
- No commits / No pushes realizados.

---

## 7. Próxima Tarea

- **Siguiente Paso**: `GATE K — FORMAL HITO L VALIDATION` (Data Quality / Governance formal hito closure).
