# L.3 Freshness / TTL — Execution Report

## STATUS
- **Sub-slice L.3 — Freshness / TTL**: 🟢 VALIDADA
- **Transversal L — Data Quality / Governance**: 🟡 EN PROGRESO
- **Gate K**: ⚪ PENDIENTE (esperando L.4–L.8)
- **Hito L**: 🟡 EN PROGRESO

---

## ROADMAP / GANTT
- **Definition of Done L.3**: Implementación y validación de políticas y evaluaciones temporales TTL de datos con integración a L.1 Source Registry y L.2 Data Provenance.
- **Semántica Freshness**: Responde exclusivamente "¿Este dato sigue siendo suficientemente reciente para ser utilizado bajo una política TTL explícita y reproducible?".
- **TTL Requerido**: Configurable por política determinista; no hardcodeado globalmente.
- **Relación con Source Registry (L.1)**: Resolución jerárquica de políticas por `source_id` o `source_type` sin duplicar la taxonomía ni entidades de L.1.
- **Relación con Data Provenance (L.2)**: Capacidad de evaluar frescura sobre registros de procedencia (`provenance_id`) y DAGs de linaje derivado (`parent_provenance_ids`).
- **Requisito Gate K**: Mantener ⚪ PENDIENTE. No invadir L.4–L.8.

---

## DISCOVERY & REUSE MATRIX

| CAPABILITY | EXISTING LOCATION | CURRENT PURPOSE | REUSE / EXTEND / CREATE |
|---|---|---|---|
| Inmutable Data Structures & Checksums | `src/domain/source_registry/models.py`, `src/domain/data_provenance/models.py` | Hash SHA-256 canónico, dataclasses frozen y MappingProxy | **REUSE** pattern |
| Source Taxonomy & Types | `src/domain/source_registry/models.py` | Identificación y registro de fuentes | **REUSE** `RegisteredSource`, `SourceType` |
| Lineage & Provenance Tracking | `src/domain/data_provenance/models.py` | Rastreo causal de hechos y padres | **REUSE** `ProvenanceRecord`, `SubjectType` |
| Virtual Clock & Clock Port | `src/infrastructure/reliability/reliability_infrastructure.py`, `src/domain/reliability/ports.py` | Control determinista del tiempo en tests | **REUSE** `ClockPort`, `VirtualClock`, `SystemClock` |
| Crash-Safe Atomic Persistence | `src/infrastructure/persistence/data/json/` | Escritura atómica `.tmp` + `fsync` + `os.replace` y detección de corrupción | **REUSE** pattern |
| Path Traversal & Secret Sanitization | `src/domain/security/models.py` | Validación de identificadores y sanitización recursiva | **REUSE** `validate_safe_identifier`, `sanitize_security_data` |
| Audit Trail Logging | `src/domain/audit/` | Registro de eventos operacionales | **REUSE** `AuditRecord`, `AuditActor` |
| Freshness Domain & Policies | `src/domain/freshness/` | Definición de `FreshnessPolicy`, `FreshnessAssessment`, `FreshnessStatus` | **CREATE** |
| Freshness JSON Repositories | `src/infrastructure/persistence/data/json/freshness_repository.py` | Repositorios durables para políticas y assessments | **CREATE** |
| Freshness Evaluation Service | `src/application/freshness/service.py` | Orquestación, resolución en cascada y evaluación | **CREATE** |

---

## BOUNDARIES
- **L.3 Responde**: Frescura y caducidad temporal según TTL explícito.
- **L.3 NO Responde**:
  - Confianza / Score de credibilidad (responsabilidad de L.4 Confidence Model).
  - Validez de estructura/tipos (responsabilidad de L.5 Schema Validation).
  - Unificación o coincidencia de entidades (responsabilidad de L.6 Entity Resolution).
  - Detección de duplicados (responsabilidad de L.7 Duplicate Detection).
  - Resolución de conflictos entre fuentes divergentes (responsabilidad de L.8 Conflict Resolution).

---

## FRESHNESS MODEL
Entidades inmutables implementadas en `src/domain/freshness/models.py`:
- `FreshnessStatus`: `FRESH`, `STALE`, `EXPIRED`, `UNKNOWN`, `ERROR`.
- `FreshnessPolicy`:
  - `policy_id` (str, safe identifier)
  - `name` (str)
  - `version` (str, SemVer)
  - `ttl_seconds` (float, > 0)
  - `stale_threshold_seconds` (Optional[float])
  - `future_tolerance_seconds` (float, default 5.0s)
  - `source_type` (Optional[SourceType])
  - `source_id` (Optional[str])
  - `subject_type` (Optional[SubjectType])
  - `field_path` (Optional[str])
  - `description` (Optional[str])
  - `checksum` (SHA-256 canónico)
  - `metadata` (MappingProxyType)
- `FreshnessAssessment`:
  - `assessment_id` (str, determinista)
  - `subject_type` (str)
  - `subject_id` (str)
  - `field_path` (Optional[str])
  - `source_id` (Optional[str])
  - `provenance_id` (Optional[str])
  - `observed_at` (Optional[datetime UTC])
  - `evaluated_at` (datetime UTC)
  - `ttl_seconds` (float)
  - `age_seconds` (Optional[float])
  - `status` (FreshnessStatus)
  - `reason` (str)
  - `policy_id` (str)
  - `policy_version` (str)
  - `correlation_id` (str)
  - `checksum` (SHA-256 canónico)
  - `metadata` (MappingProxyType)
  - Propiedad de conveniencia: `is_usable` (`True` ssi `status == FreshnessStatus.FRESH`).

---

## TTL POLICY & PRECEDENCE
Cascada de resolución determinista (`FreshnessService.resolve_policy`):
1. Match exacto por `field_path` + `subject_type`
2. Match por `field_path` solo
3. Match por `source_id` + `subject_type`
4. Match por `source_type` + `subject_type`
5. Match por `subject_type` solo
6. Match por `source_id` solo
7. Match por `source_type` solo
8. Política catch-all persistida en repositorio
9. Default policy inyectada en el servicio

---

## CLOCK / TIME SEMANTICS
- Normalización obligatoria a UTC timezone-aware.
- Rechazo o normalización explícita de timestamps naive.
- Inyección estricta de `ClockPort` (`VirtualClock` en tests, `SystemClock` en producción). Cero llamadas a `time.sleep()`.

---

## TTL BOUNDARY & TIMESTAMPS
- **Boundary exacto**:
  - `age_seconds < ttl_seconds` $\rightarrow$ `FRESH`
  - `age_seconds >= ttl_seconds` $\rightarrow$ `STALE` (o `EXPIRED` si `age_seconds >= stale_threshold_seconds`)
- **Missing Timestamp**:
  - `observed_at is None` $\rightarrow$ `FreshnessStatus.UNKNOWN` (`UNKNOWN != FRESH`, `is_usable = False`).
- **Future Timestamp**:
  - `(observed_at - now) > future_tolerance_seconds` $\rightarrow$ `FreshnessStatus.ERROR` (`is_usable = False`, no produce age negativo ni falso FRESH).

---

## SOURCE REGISTRY & PROVENANCE LINK
- **L.1 Link**: Integración con `SourceRegistryRepositoryPort` para resolver el `source_type` asociado a un `source_id`.
- **L.2 Link**: Integración con `ProvenanceRepositoryPort` para evaluar hechos a partir de su `provenance_id`.

---

## DERIVED DATA
- Regla del padre más degradado (*oldest/most degraded parent rule*):
  - La frescura de un dato derivado se restringe por el estado de sus ancestros en el DAG de procedencia.
  - Jerarquía de severidad: `ERROR > UNKNOWN > EXPIRED > STALE > FRESH`.
  - Un dato derivado generado en $T_{now}$ cuyos inputs son STALE se evalúa como `STALE` con razón explícita (`Derived data degraded by parent provenance...`).

---

## PERSISTENCE DECISION & IDEMPOTENCY
- Se crearon puertos `FreshnessPolicyRepositoryPort` y `FreshnessAssessmentRepositoryPort` e implementaciones físicas JSON durables: `JsonFreshnessPolicyRepository` y `JsonFreshnessAssessmentRepository`.
- Persistencia crash-safe mediante atomic write (`.tmp` + `fsync` + `os.replace`), verificación de checksum SHA-256 y detección de colisiones (`FreshnessConflictError`).
- Replays con idéntico ID y checksum devuelven la instancia existente de forma determinista.

---

## BUSINESS CONSUMER BOUNDARY
- Se validó el patrón donde un consumidor de pricing comercial (`CommercialPricingConsumer`) verifica `is_quote_temporally_acceptable(provenance_id)` antes de consumir una cotización de proveedor, demostrando rechazo temporal explícito tras expirar su TTL sin alterar datos ni calcular confianza.

---

## TEST SUITES & VERIFICATION

### 1. Unit Tests (`tests/unit/test_l3_freshness_ttl_unit.py`)
16/16 tests passing:
- `test_fresh_value_when_age_less_than_ttl`
- `test_stale_and_expired_value_when_age_exceeds_ttl`
- `test_exact_ttl_boundary_semantics`
- `test_zero_ttl_handling`
- `test_timezone_aware_utc_handling`
- `test_naive_timestamp_normalized_to_utc`
- `test_missing_timestamp_produces_unknown`
- `test_future_timestamp_beyond_tolerance_produces_error`
- `test_deterministic_clock_advance`
- `test_policy_precedence_deterministic_resolution`
- `test_derived_data_cannot_be_fresher_than_stale_parent`
- `test_derived_data_with_missing_parent_timestamp`
- `test_policy_semver_validation`
- `test_path_traversal_rejected_in_identifiers`
- `test_repository_idempotency_and_conflict_detection`
- `test_freshness_does_not_mutate_provenance_or_calculate_confidence`

### 2. Integration & E2E Tests (`tests/integration/test_l3_freshness_ttl_integration.py`)
8/8 tests passing:
- `test_scenario_a_and_b_registered_source_provenance_fresh_then_stale`
- `test_scenario_c_missing_provenance_timestamp_unknown`
- `test_scenario_d_supplier_quote_ttl`
- `test_scenario_e_marketplace_price_field_level_override`
- `test_scenario_f_derived_fact_with_stale_parent`
- `test_scenario_g_crash_safe_persistence_and_restart`
- `test_scenario_h_policy_version_change_deterministic_reevaluation`
- `test_scenario_i_business_consumer_temporal_precheck_boundary`

### 3. L.1 & L.2 Regression
- `test_l1_source_registry_unit.py` + `test_l1_source_registry_integration.py`: 33 passed.
- `test_l2_data_provenance_unit.py` + `test_l2_data_provenance_integration.py`: 28 passed.

### 4. Full Regression
- **Baseline previo**: 1219 passed, 1 skipped, 0 failures.
- **Nuevo resultado global**: **1243 passed, 1 skipped, 0 failures** (en 45.27s).

---

## ARCHITECTURE AUDIT
1. *¿TTL está hardcodeado?* **No**. Es 100% configurable por entidad `FreshnessPolicy`.
2. *¿Freshness duplica Provenance?* **No**. Referencia `provenance_id` y `parent_provenance_ids` de L.2.
3. *¿Freshness duplica Source Registry?* **No**. Consume `SourceType` y `source_id` de L.1.
4. *¿Se calcula Confidence?* **No**. L.4 permanece intocada.
5. *¿Timestamp missing produce FRESH?* **No**. Produce `FreshnessStatus.UNKNOWN`.
6. *¿Timestamp futuro produce FRESH?* **No**. Produce `FreshnessStatus.ERROR`.
7. *¿age == ttl está definido?* **Sí**. `age >= ttl` produce `STALE`.
8. *¿Clock es testeable?* **Sí**. Se utiliza `VirtualClock` / `ClockPort`.
9. *¿Derived data puede ocultar parent stale?* **No**. Se impone la regla del padre más degradado.
10. *¿Policy precedence es determinista?* **Sí**. Cascada de 9 niveles exhaustiva y ordenada.
11. *¿Timezone está controlada?* **Sí**. Normalización a UTC timezone-aware.
12. *¿Freshness cambia datos originales?* **No**. Todas las evaluaciones son inmutables y de solo lectura sobre los datos de origen.
13. *¿L.4–L.8 fueron invadidas?* **No**. Permanecen desacopladas y no implementadas.

---

## FILES CREATED / MODIFIED

### Created:
- `src/domain/freshness/__init__.py`
- `src/domain/freshness/models.py`
- `src/domain/freshness/ports.py`
- `src/application/freshness/__init__.py`
- `src/application/freshness/service.py`
- `src/infrastructure/persistence/data/json/freshness_repository.py`
- `tests/unit/test_l3_freshness_ttl_unit.py`
- `tests/integration/test_l3_freshness_ttl_integration.py`
- `L3_FRESHNESS_TTL_EXECUTION_REPORT.md`

### Modified:
- `docs/market-intelligence/AI_AUTONOMOUS_COMMERCE_GANTT_MAESTRA.md`

---

## GIT & HYGIENE
- `git diff --check`: 0 issues.
- `git ls-files .pytest_tmp`: 0 archivos.
- `git commit` / `git push`: NO ejecutados (política estricta).

---

## FINAL DECISION & NEXT TASK
- **L.3 Freshness / TTL** queda formalmente **🟢 VALIDADA**.
- **Next Task**: L.4 — Confidence Model (Transversal L — Data Quality y Governance).
