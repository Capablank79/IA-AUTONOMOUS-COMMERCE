# Q.1 — OPPORTUNITY DASHBOARD EXECUTION REPORT
**Business Intelligence / Multi-Tenant Market Opportunity Analysis**

---

## 1. ROADMAP ALIGNMENT & RESPONSABILIDAD

- **Hito:** Q — Business Intelligence
- **Task:** Q.1 — Opportunity Dashboard
- **Responsabilidad de Negocio:** Responder con precisión demostrada a la pregunta canónica:
  > *“¿Puede un usuario autorizado visualizar, filtrar, comparar y priorizar las oportunidades de mercado detectadas por el agente para decidir dónde actuar?”*
- **Naturaleza del Dashboard:**
  - Estrictamente **consultivo, proyectivo y explicable**.
  - **DASHBOARD != ENGINE:** No recalcula inteligencia desde cero, no ejecuta crawling ni scraping en vivo, no invoca marketplaces externos directamente, no ejecuta compras ni publica listings.
  - Basado en datos reales existentes originados por el pipeline canónico de Market Intelligence y Business Memory.
  - Aislamiento multi-tenant por defecto (*Default Deny* / scoped access).
  - Semántica estricta de incertidumbre: `UNKNOWN != 0`.
  - Representación monetaria y márgenes en `Decimal` con currency explícita.
- **Límites de Alcance:**
  - Implementado **exclusivamente** Q.1.
  - Tareas Q.2 (Supplier Dashboard), Q.3 (Profit Dashboard), Q.4 (Mission Dashboard), Q.5 (Agent Cost Dashboard), Q.6 (Business KPIs) y Gate P no fueron tocadas.

---

## 2. DISCOVERY & MATRIZ DE FUENTES DE DATOS

Se realizó el levantamiento exhaustivo del modelo de oportunidades preexistente en el ecosistema (`src/domain/market_intelligence/models.py`), consolidando la siguiente matriz:

| DATA SOURCE | MODEL | LOCATION | PERSISTENCE | AVAILABLE FIELDS | Q.1 USE |
|---|---|---|---|---|---|
| Market Intelligence Pipeline | `OpportunityRecord` | `src/domain/market_intelligence/models.py` | JSON partitioned / PostgreSQL ready | `id`, `opportunity_id`, `product_candidate`, `market_score`, `demand_score`, `competition_score`, `margin_score`, `confidence`, `reasons`, `scoring_rationale`, `observed_metrics`, `derived_metrics`, `provenance`, `correlation_id`, `created_at` | Source of Truth para proyección y analytics |
| Product Candidate Context | `ProductCandidate` | `src/domain/market_intelligence/models.py` | Embedded en `OpportunityRecord` | `title`, `category`, `marketplace`, `suggested_price`, `estimated_cost`, `estimated_margin`, `source_count`, `currency`, `brand`, `supplier_count` | Metadatos de producto, precios y márgenes |
| Multi-Tenant Partition | `TenantOpportunityRepositoryPort` | `src/domain/opportunity_dashboard/ports.py` | JSON (`data/tenants/{tenant_id}/opportunities/`) | Particionado atómico por tenant | Lectura y almacenamiento persistente seguro por tenant |

---

## 3. SOURCE OF TRUTH & PERSISTENCIA

- **Source of Truth:** Modelos canónicos `OpportunityRecord` y `ProductCandidate` generados por el pipeline de inteligencia de mercado.
- **Repository Pattern:** Se definió el puerto de dominio `TenantOpportunityRepositoryPort` en [ports.py](file:///c:/Users/JLLV/Desktop/IA-AUTONOMOUS-COMMERCE/src/domain/opportunity_dashboard/ports.py) y su implementación `JsonTenantOpportunityRepository` en [tenant_opportunity_repository.py](file:///c:/Users/JLLV/Desktop/IA-AUTONOMOUS-COMMERCE/src/infrastructure/persistence/data/json/tenant_opportunity_repository.py).
- **Escritura Atómica & File Isolation:**
  - Rutas seguras validando identificadores con `validate_safe_identifier` y `CrossTenantGuard`.
  - Escritura atómica mediante archivo `.tmp` + `os.replace` + `fsync`.
  - Cero mezclas de datos entre tenants en disco.

---

## 4. OPPORTUNITY DATA CONTRACT & VIEWMODELS

Se diseñaron DTOs inmutables (`@dataclass(frozen=True)`) en [models.py](file:///c:/Users/JLLV/Desktop/IA-AUTONOMOUS-COMMERCE/src/domain/opportunity_dashboard/models.py):

1. **`OpportunityDashboardItem`**:
   - `opportunity_id`, `title`, `category`, `marketplace`, `currency`
   - Scores: `opportunity_score`, `demand_score`, `competition_score`, `margin_score`, `confidence`
   - Finanzas: `estimated_price`, `estimated_cost`, `estimated_margin`, `margin_pct` (todos en `Decimal` o `None`)
   - Operaciones: `supplier_count`, `source_count`, `seasonality`, `risk`, `status`, `detected_at`
2. **`OpportunityDashboardSummary`**:
   - `total_opportunities`, `high_potential_count` (score >= 70), `medium_potential_count` (50-69), `low_potential_count` (< 50)
   - `average_opportunity_score` (Decimal redondeado a 2 decimales)
   - `opportunities_by_category`, `opportunities_by_marketplace` (agregaciones canónicas)
   - `newest_detections` (Top 5 más recientes)
3. **`OpportunityDashboardDetail`**:
   - Ítem base + `reasons`, `scoring_rationale`, `observed_metrics_detail`, `derived_metrics_detail`, `explanation`, `provenance`, `correlation_id`, `metadata`.
4. **`OpportunityComparisonView`**:
   - Comparación tabular normalizada de 2 a 10 oportunidades autorizadas.
5. **`OpportunityDashboardPage`**:
   - Paginación canónica con `items`, `total_count`, `page`, `page_size`, `total_pages`.

---

## 5. SEMÁNTICA DE INCERTIDUMBRE (`UNKNOWN != 0`) & PRECISIÓN FINANCIERA

- **Unknown Preservation:** Todo valor ausente o desconocido (`cost`, `margin`, `supplier_count`, `seasonality`, `risk`) se preserva estrictamente como `None` (`null` en JSON) y jamás se mapea a `0`, `0.0` o cadenas sintéticas.
- **Decimal Money:** Todos los precios, costes y márgenes utilizan `Decimal`. Se serializan como strings en formato `to_dict()` para garantizar precisión arbitraria en APIs REST/Web sin pérdidas de redondeo de punto flotante IEEE 754.

---

## 6. TENANT ISOLATION & AUTORIZACIÓN (O.1, O.3, O.4, N.4)

- **Tenant Scoping:** Cada consulta requiere `TenantContext(tenant_id=...)`. Si un usuario intenta acceder a datos de otro tenant, la consulta se aísla automáticamente y/o se rechaza (`TenantAccessDeniedError` / 403 Forbidden).
- **RBAC & Permisos:** Se incorporó el permiso granular `OPPORTUNITY_DASHBOARD_READ` y `BUSINESS_INTELLIGENCE_READ` en el sistema RBAC SaaS.
- **Default Deny:** Sin sesión válida -> `401 Unauthorized`. Sin rol con permisos -> `403 Forbidden`.

---

## 7. FILTRADO, ORDENACIÓN Y PAGINACIÓN

- **Filtros Soportados:**
  - `category` (case-insensitive substring)
  - `marketplace` (Enum match)
  - `status` (Enum match)
  - `score_min` / `score_max` (Decimal range)
  - `margin_min` (Decimal comparison)
  - `confidence_min` (Decimal comparison)
  - `date_from` / `date_to` (UTC ISO format)
  - `search_text` (búsqueda en título e ID)
- **Ordenación Determinista:**
  - `opportunity_score` (default DESC), `detected_at`, `margin`, `confidence`, `demand_score`, `competition_score`.
  - **Tie-breaking Estable:** Ante igualdad de métrica primaria, se ordena secundariamente por `detected_at DESC` y finalmente por `opportunity_id ASC`.
- **Paginación Segura:**
  - `page >= 1`, `1 <= page_size <= 100` (acotado para evitar denegación de servicio por memoria).

---

## 8. INTEGRACIÓN REST & ADMIN CONSOLE (O.10)

Los endpoints de Business Intelligence fueron montados de forma limpia en el router de Admin Console ([admin_app.py](file:///c:/Users/JLLV/Desktop/IA-AUTONOMOUS-COMMERCE/src/infrastructure/web/admin_app.py)):

- `GET /api/bi/tenants/{tenant_id}/opportunities`: Listado paginado con filtros y ordenación.
- `GET /api/bi/tenants/{tenant_id}/opportunities/summary`: Resumen analítico agregado.
- `GET /api/bi/tenants/{tenant_id}/opportunities/{opportunity_id}`: Detalle de oportunidad con desglose métrico.
- `GET/POST /api/bi/tenants/{tenant_id}/opportunities/compare`: Comparación segura de 2–10 oportunidades.
- `GET /admin/tenants/{tenant_id}/bi/opportunities`: Render HTML interactivo dentro del layout de Admin Console O.10.

---

## 9. SEGURIDAD, PRIVACIDAD & OBSERVABILIDAD (N.9, P.7)

- **Sanitización:** Cero exposición de credenciales de marketplaces, tokens de API, prompts internos, cadenas CoT o PII.
- **Métricas:** Los endpoints se integran transparentemente en el middleware de observabilidad P.7 / O.12 registrando latencias, códigos HTTP y tenant tags.

---

## 10. RESULTADOS DE VALIDACIÓN Y SUITES DE TESTS

### 10.1. Tests Específicos Q.1 (Unitarios e Integración)
- **Unitarios (`tests/unit/test_q1_opportunity_dashboard_unit.py`):** 10/10 PASS
  1. `test_opportunity_dashboard_item_unknown_semantics`
  2. `test_opportunity_dashboard_item_decimal_money_serialization`
  3. `test_query_sort_and_pagination_defaults`
  4. `test_json_tenant_opportunity_repository_crud_and_isolation`
  5. `test_service_empty_state`
  6. `test_service_filtering_and_sorting`
  7. `test_service_pagination`
  8. `test_service_summary_calculation`
  9. `test_service_detail_and_comparison`
  10. `test_service_authorization_enforcement`
- **Integración HTTP/REST (`tests/integration/test_q1_opportunity_dashboard_integration.py`):** 5/5 PASS
  1. `test_q1_opportunity_dashboard_tenant_isolation_and_listing`
  2. `test_q1_opportunity_dashboard_filters_and_sorting`
  3. `test_q1_opportunity_dashboard_summary_and_detail`
  4. `test_q1_opportunity_dashboard_comparison`
  5. `test_q1_opportunity_dashboard_unauthenticated_and_unauthorized`

**Subtotal Q.1:** **15 passed, 0 failures.**

### 10.2. Regresión Completa de la Plataforma
- **Baseline inicial:** 2471 passed, 2 skipped, 0 failures.
- **Resultado actual:** **2486 passed, 2 skipped, 0 failures** (15 tests agregados correspondientes a Q.1).

### 10.3. Validaciones de Despliegue y Migraciones
- `python scripts/deploy_validate.py`: **5/5 CHECKS PASSED**
- `python scripts/db_migrate.py check`: **UP TO DATE (revision 001_initial_saas_schema)**

---

## 11. GIT HYGIENE & AUDITORÍA ARQUITECTÓNICA

- `git diff --check`: Limpio (sin trailing whitespaces ni conflictos).
- `git status --short`:
  - Nuevos módulos creados estrictamente en `src/domain/opportunity_dashboard/`, `src/application/opportunity_dashboard/`, `src/infrastructure/persistence/data/json/tenant_opportunity_repository.py`.
  - Integración mínima en `src/infrastructure/web/admin_app.py`, `src/infrastructure/web/app.py`, `src/domain/admin_console/models.py`.
  - Cero archivos de base de datos o logs transitorios en staging.
  - Ningún archivo correspondiente a Q.2+ tocado.
- **Estado de Control de Versiones:** NO commit, NO push.
