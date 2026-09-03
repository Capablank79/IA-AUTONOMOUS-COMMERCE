# L.2 DATA PROVENANCE — INFORME FORMAL DE EJECUCIÓN Y VALIDACIÓN

**Fecha de Ejecución:** 2026-09-02
**Hito:** Transversal L — Data Quality & Governance
**Capacidad:** L.2 — Data Provenance
**Estado:** 🟢 VALIDADA
**Gate K:** ⚪ PENDIENTE
**Hito L:** 🟡 EN PROGRESO

---

## 1. STATUS & OVERVIEW

Se ha implementado y validado de manera formal y desacoplada la capacidad **L.2 Data Provenance** dentro del framework de calidad de datos del sistema. La implementación responde con exactitud a la pregunta fundamental de gobierno:

> *"¿De qué fuente y evidencia concreta provino este dato, y cuál es su linaje hasta las fuentes raíz?"*

La capacidad se apoya contractualmente en **L.1 Source Registry** para la resolución de fuentes y garantiza que hechos atómicos o derivados puedan ser auditados y rastreados sin alucinaciones, sin crear bases de datos de grafos innecesarias, y sin invadir responsabilidades de frescura (L.3), confianza (L.4), validación de esquema (L.5), resolución de entidades (L.6), detección de duplicados (L.7) ni resolución de conflictos (L.8).

---

## 2. ROADMAP & GANTT RECONCILIATION

- **Definition of Done L.2:** Cumplida al 100%.
- **Relación con Gate K:** Provee el eslabón de linaje de datos (`Data Fact -> Evidence -> SourceRecord -> RegisteredSource`) exigido por Gate K para la trazabilidad de decisiones comerciales críticas hasta su fuente de origen.
- **Transversal L & Gate K Status:**
  - L.1 Source Registry: 🟢 VALIDADA
  - L.2 Data Provenance: 🟢 VALIDADA
  - L.3 Freshness / TTL: 🟡 PENDIENTE (Siguiente tarea)
  - L.4 Confidence Model: 🟡 PENDIENTE
  - L.5 Schema Validation: ⚪ PENDIENTE
  - L.6 Entity Resolution: ⚪ PENDIENTE
  - L.7 Duplicate Detection: 🟡 PENDIENTE
  - L.8 Conflict Resolution: ⚪ PENDIENTE
  - Gate K: ⚪ PENDIENTE

---

## 3. DISCOVERY & REUSE MATRIX

| Capability | Existing Location | Current Purpose | Action (Reuse / Extend / Create) |
|---|---|---|---|
| **RegisteredSource** | `src/domain/source_registry/models.py` | Modelo formal de fuentes de datos canónicas (L.1) | **REUSE** (Validación de orígenes y metadatos) |
| **Source Registry Persistence** | `src/infrastructure/persistence/data/json/source_registry_repository.py` | Almacenamiento JSON de fuentes de datos | **REUSE** (Verificación de existencia e integridad) |
| **MarketObservation / Evidence** | `src/domain/market_monitoring/models.py` | Hechos observados del mercado y evidencias asociadas | **REUSE** (Enlace mediante `evidence_id` y `source_record_id`) |
| **CommercialQuote / Supplier** | `src/domain/supplier_intelligence/models.py` | Cotizaciones y datos estructurados de proveedores | **REUSE** (Linaje de hechos de proveedor) |
| **Audit Trail (K.1)** | `src/domain/audit/models.py` | Auditoría inmutable de eventos operacionales | **REUSE** (Consumo de `correlation_id` / `causation_id`) |
| **Agent Trace (K.2)** | `src/domain/agent_trace/models.py` | Trazabilidad de pasos de ejecución | **REUSE** (Referenciable en metadatos contextuales) |
| **Security Scanner (K.8)** | `src/domain/security/` | Sanitización de secretos y path traversal | **REUSE** (Sanitización de credenciales y validación de IDs) |
| **Provenance Models** | `src/domain/data_provenance/models.py` | Modelado inmutable de linaje y grafo DAG | **CREATE** (Modelos canónicos L.2) |
| **Provenance Port** | `src/domain/data_provenance/ports.py` | Contrato de interfaz para repositorios | **CREATE** (Protocol desacoplado) |
| **JsonProvenanceRepository** | `src/infrastructure/persistence/data/json/data_provenance_repository.py` | Persistencia atómica crash-safe en JSON con SHA-256 | **CREATE** (Persistencia durable) |
| **DataProvenanceService** | `src/application/data_provenance/service.py` | Orquestación, validación, prevención de ciclos y trace | **CREATE** (Servicio de aplicación) |

---

## 4. ARCHITECTURAL BOUNDARIES & RESPONSIBILITIES

### Lo que L.2 HACE:
- Modela registros de procedencia inmutables (`ProvenanceRecord`) para entidades y campos específicos (`field_path`).
- Enlaza un hecho de datos con una fuente canónica registrada (`RegisteredSource` de L.1).
- Enlaza hechos con identificadores de evidencia (`evidence_id`, `source_record_id`).
- Gestiona linajes derivados mediante un grafo acíclico dirigido (DAG) a través de `parent_provenance_ids`.
- Reconstruye la ruta completa desde un hecho final hasta las fuentes raíz registradas (`trace_to_sources`).
- Previene y rechaza ciclos (auto-referencias y ciclos indirectos en el DAG).
- Aplica integridad criptográfica canónica mediante SHA-256 (`recompute -> compare` con detección de corrupción física).
- Garantiza idempotencia ante replays idénticos y detecta conflictos ante alteraciones semánticas.
- Sanitiza recursivamente secretos y valida seguridad de paths.

### Lo que L.2 NO HACE:
- NO calcula frescura ni TTL (responsabilidad exclusiva de L.3).
- NO calcula puntajes de confianza numérica ni pesos de fuente (responsabilidad de L.4).
- NO valida esquemas de payload contra schemas JSON o Pydantic (responsabilidad de L.5).
- NO realiza resolución de entidades entre catálogos dispares (responsabilidad de L.6).
- NO detecta duplicados entre registros de negocio (responsabilidad de L.7).
- NO ejecuta resolución de conflictos ni políticas de arbitraje (responsabilidad de L.8).
- NO duplica Audit Trail ni Agent Trace.

---

## 5. PROVENANCE MODEL & IDENTITY

### Modelo Inmutable (`ProvenanceRecord`):
```python
@dataclass(frozen=True)
class ProvenanceRecord:
    provenance_id: str
    source_id: str
    subject_type: SubjectType
    subject_id: str
    captured_at: datetime
    source_version: str = "1.0.0"
    source_record_id: Optional[str] = None
    evidence_id: Optional[str] = None
    field_path: Optional[str] = None
    parent_provenance_ids: Tuple[str, ...] = field(default_factory=tuple)
    transformation_id: Optional[str] = None
    correlation_id: str = "default-correlation"
    causation_id: Optional[str] = None
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

### Generación Determinista de Identidad:
El `provenance_id` se deriva del hash SHA-256 de las tuplas semánticas inmutables del linaje:
`(source_id, subject_type, subject_id, field_path, evidence_id, source_record_id, parent_provenance_ids, transformation_id)`.

---

## 6. FIELD-LEVEL PROVENANCE & DERIVED DATA DAG

1. **Field-Level Granularity:** Permite registrar procedencias disjuntas para diferentes campos dentro de un mismo objeto (por ejemplo, `product-123` donde `price.amount` proviene de una cotización de proveedor y `stock.available` proviene de una API de almacén).
2. **DAG de Datos Derivados:** Un hecho sintetizado (ej. `OPPORTUNITY_SCORE` o `PROFIT_MARGIN`) registra los `provenance_id` de los hechos atómicos que le dieron origen.
3. **Cycle Safety:** Se valida preventivamente que ningún registro sea padre de sí mismo (`self-parent`) y que ninguna cadena contenga ciclos indirectos (`A -> B -> A`), arrojando `ValueError` antes de persistir.
4. **Missing Lineage Safety:** Si se intenta reconstruir un linaje con padres faltantes, el sistema marca explícitamente `is_complete = False` y reporta la lista de `unresolved_parents` sin inventar orígenes falsos.

---

## 7. PERSISTENCIA, INTEGRIDAD Y SEGURIDAD

- **Crash-Safe Atomic Writes:** Escritura en archivo temporal `.tmp`, sincronización de buffers a disco (`fsync`) y reemplazo atómico (`os.replace`).
- **Thread Safety:** Bloqueo de concurrencia mediante `threading.RLock` en todas las operaciones de lectura, escritura e indexación.
- **Detección de Corrupción Física:** En cada lectura desde disco, el checksum SHA-256 se recalcula sobre el payload semántico ordenado. Si existe alteración física de bytes, se eleva `CorruptedProvenanceRecordError`.
- **Idempotencia y Detección de Conflictos:** Si se re-registra un linaje idéntico, la operación es idempotente; si los datos semánticos difieren para un mismo ID, se arroja `ProvenanceConflictError`.
- **Sanitización Recursiva de Secretos:** Llaves de autorización, tokens, API keys, credenciales y contraseñas son redactadas recursivamente (`[REDACTED]`) antes de la persistencia.
- **Path Traversal Protection:** Los identificadores se validan con regex estricto `^[a-zA-Z0-9_\-\.\:]+$` rechazando caracteres como `/`, `\` y `..`.

---

## 8. SUITE DE TESTS Y RESULTADOS DE REGRESIÓN

### Tests Unitarios L.2 (`tests/unit/test_l2_data_provenance_unit.py`): 20/20 PASSED
1. `test_01_immutable_provenance` (Inmutabilidad con dataclass frozen y MappingProxyType)
2. `test_02_direct_source_lineage` (Registro de linaje directo)
3. `test_03_source_reference_required` (Rechazo de campos obligatorios nulos)
4. `test_04_deterministic_id` (Determinismo en generación de IDs)
5. `test_05_checksum_verification` (Cálculo y verificación de checksum SHA-256)
6. `test_06_semantic_mutation_changes_checksum` (Sensibilidad ante mutaciones semánticas)
7. `test_07_same_replay_idempotent` (Idempotencia ante re-registro exacto)
8. `test_08_conflict_rejected` (Detección de conflicto ante mutación de payload)
9. `test_09_field_level_provenance` (Procedencia a nivel de campo)
10. `test_10_parent_provenance_and_derived_data` (Linaje de datos derivados)
11. `test_11_self_cycle_rejected` (Rechazo de auto-referencia en padres)
12. `test_12_cycle_rejected_in_dag` (Detección y rechazo de ciclos en grafo)
13. `test_13_duplicate_parents_normalized` (Deduplicación y normalización de padres)
14. `test_14_unknown_source_handling` (Manejo de fuentes no registradas)
15. `test_15_secret_sanitization` (Sanitización recursiva de secretos)
16. `test_16_path_safety` (Protección contra path traversal)
17. `test_17_no_freshness_logic` (Verificación de frontera: sin lógica de frescura)
18. `test_18_no_confidence_logic` (Verificación de frontera: sin lógica de confianza)
19. `test_19_no_conflict_resolution` (Verificación de frontera: sin resolución de conflictos)
20. `test_20_no_duplicate_detection_ownership` (Verificación de frontera: sin ownership de duplicados)

### Tests de Integración L.2 (`tests/integration/test_l2_data_provenance_integration.py`): 8/8 PASSED
- **Escenario A:** Ciclo de vida completo Source Registry -> Provenance -> Persistencia -> Recuperación.
- **Escenario B:** Trazabilidad de observación de Market Intelligence hasta fuente registrada.
- **Escenario C:** Trazabilidad de cotización de proveedor hasta fuente de datos de proveedor.
- **Escenario D:** Hecho derivado con múltiples padres y resolución recursiva de fuentes raíz.
- **Escenario E:** Durabilidad post-reinicio y recarga de repositorio desde disco.
- **Escenario F:** Detección de manipulación/corrupción física de registros persistidos.
- **Escenario G:** Replay de registro sin generar duplicados.
- **Escenario H:** Conflicto explícito al intentar registrar linaje inconsistente.

### Regresión L.1 Source Registry: 33/33 PASSED
- `tests/unit/test_l1_source_registry_unit.py` (24 passed)
- `tests/integration/test_l1_source_registry_integration.py` (9 passed)

### Regresión Transversal y Componentes Clave: 173/173 PASSED
- Supplier Intelligence (`tests/unit/domain/supplier_intelligence/`): 74 passed
- Audit Trail K.1 (`tests/unit/test_k1_audit_trail_unit.py` + integration): 36 passed
- Agent Trace K.2 (`tests/unit/test_k2_agent_trace_unit.py` + integration): 22 passed
- Security Checks K.8 (`tests/unit/test_k8_security_checks_unit.py` + integration): 31 passed

### Regresión Completa del Sistema (Full Pytest Suite):
- **Baseline inicial:** 1191 passed, 1 skipped, 0 failures.
- **Resultado actual:** **1219 passed, 1 skipped, 0 failures** (28 nuevos tests pasando al 100%).

---

## 9. VERIFICACIÓN DE ARTEFACTOS Y CONTROL DE VERSIONES

- **Pytest Hygiene:** `git ls-files .pytest_tmp` limpio (0 archivos rastreados).
- **Git Diff Hygiene:** `git diff --check` limpio (0 errores de formato/espaciado).
- **Git Policy:**
  - `git commit` ejecutado: **NO**
  - `git push` ejecutado: **NO**
  - Políticas de checkpoint respetadas estrictamente hasta el cierre formal del Hito L y Gate K.

---

## 10. DECISIÓN FINAL Y SIGUIENTE PASO

- **L.2 Data Provenance:** 🟢 **VALIDADA**
- **Estado de Hito L:** 🟡 **EN PROGRESO**
- **Estado de Gate K:** ⚪ **PENDIENTE**
- **Siguiente Tarea Planificada:** **L.3 — Freshness / TTL** (Mantenerse a la espera de la instrucción del usuario; no implementar proactivamente).
