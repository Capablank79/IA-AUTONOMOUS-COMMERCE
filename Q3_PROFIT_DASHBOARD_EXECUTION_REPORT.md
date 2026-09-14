# Q.3 — PROFIT DASHBOARD EXECUTION REPORT
**Business Intelligence / Unit Economics, Margin & Profitability Analysis**

---

## 1. ROADMAP ALIGNMENT & RESPONSABILIDAD
- **Hito:** Q — Business Intelligence
- **Task:** Q.3 — Profit Dashboard
- **Responsabilidad de Negocio:** Responder con rigor técnico y analítico a la pregunta canónica:
  > *“¿Puede un usuario autorizado visualizar y comparar la rentabilidad esperada o realizada de oportunidades/productos usando únicamente componentes financieros reales y trazables?”*
- **Naturaleza del Dashboard:**
  - Estrictamente **consultivo, proyectivo y explicable**.
  - **DASHBOARD != PRICING ENGINE:** No ejecuta repricing automático, no cambia precios en marketplaces, no altera comisiones ni inventa estrategias de precios ficticias.
  - Basado en facts financieros reales preexistentes (Opportunity Facts, Supplier Intelligence, Marketplace Fees y Cost Facts).
  - Aislamiento multi-tenant por defecto (*Default Deny* / storage particionado con `CrossTenantGuard`).
  - Semántica estricta de incertidumbre: `UNKNOWN != 0`, `UNKNOWN != FREE`.
  - Aritmética financiera mandataria en `Decimal` (prohibición de `float`).
  - Integración transparente con RBAC (O.4) y Admin Console (O.10).
- **Límites de Dominio y Alcance:**
  - Implementado **exclusivamente** Q.3.
  - Tareas Q.4 (Mission Dashboard), Q.5 (Agent Cost Dashboard), Q.6 (Business KPIs) y Gate P no fueron tocadas ni modificadas.

---

## 2. DISCOVERY & MATRIZ DE FUENTES DE DATOS FINANCIEROS
Se realizó el levantamiento exhaustivo de componentes de costos y fuentes de verdad financieras en el codebase:

| FINANCIAL FACT | SOURCE MODEL | LOCATION | PERSISTENCE | CURRENCY | Q.3 USE |
|---|---|---|---|---|---|
| Sale Price | `Opportunity` / `MarketplaceListing` | `src/domain/market_intelligence/models.py` | JSON / PostgreSQL | Explícita (CLP/USD) | Ingreso bruto base proyectado |
| Unit Cost / MOQ | `SupplierProductReference` | `src/domain/market_intelligence/models.py` | JSON / PostgreSQL | Explícita | Costo directo de producto / COGS |
| Shipping Cost | `SupplierProductReference` / `ShippingQuote` | `src/domain/market_intelligence/models.py` | JSON / PostgreSQL | Explícita | Costo logístico real (None si no cotizado) |
| Marketplace Fee | `MercadoLibreListing` / Commission Facts | `src/domain/publication/models.py` | JSON / PostgreSQL | Explícita / % | Comisión de plataforma (15% marketplace fee conocido) |
| Payment Gateway Fee | Payment Adapter Facts | `src/domain/billing/models.py` | JSON / PostgreSQL | Explícita / % | Comisión pasarela de pago |
| Tax Facts | Tax / IVA Configuration | Config / Facts reales | Inyectado | Explícita / % | Impuestos aplicables si existen |
| Multi-Tenant Partition | `TenantProfitRepositoryPort` | `src/domain/profit_dashboard/ports.py` | JSON (`data/tenants/{tenant_id}/profit/`) | Por registro | Almacenamiento seguro particionado |

---

## 3. FINANCIAL SOURCE OF TRUTH & PERSISTENCIA
- **FINANCIAL SOURCE OF TRUTH:** Facts originados en `Opportunity` (Q.1), `SupplierProductReference` (Q.2), y parámetros reales de fees/costos.
- **Repository Pattern:** Puerto `TenantProfitRepositoryPort` en `src/domain/profit_dashboard/ports.py` e implementación `JsonTenantProfitRepository` en `src/infrastructure/persistence/data/json/tenant_profit_repository.py`.
- **Escritura Atómica & File Isolation:**
  - Validación de identificadores seguros con `validate_safe_identifier` y custodia por `CrossTenantGuard`.
  - Escrituras atómicas con archivo temporal `.tmp` y reemplazo atómico `os.replace` + `fsync`.
  - Partición física en `data/tenants/{tenant_id}/profit/profit_items.json`.
  - Cero filtraciones de registros entre tenants.

---

## 4. PROFIT VIEW MODEL & DATA CONTRACT
Se implementaron DTOs inmutables en `src/domain/profit_dashboard/models.py`:

1. **`ProfitDashboardItem`**:
   - `item_id`, `opportunity_id`, `supplier_product_id`, `marketplace`, `title`, `currency`
   - Ingresos: `sale_price` (`Decimal` o `None`)
   - Costos: `unit_cost`, `shipping_cost`, `marketplace_fee`, `payment_fee`, `tax_cost`, `other_costs` (todos `Optional[Decimal]`)
   - Propiedades Derivadas Auditables:
     - `gross_profit = sale_price - unit_cost` (si ambos están presentes)
     - `total_known_cost = sum(componentes no nulos)`
     - `contribution_profit = sale_price - total_known_cost`
     - `margin_pct = (contribution_profit / sale_price) * 100`
     - `markup_pct = (contribution_profit / unit_cost) * 100`
     - `break_even_price = total_known_cost / (1 - variable_rates)`
   - Semántica de Completitud: `completeness` (`COMPLETE`, `PARTIAL`, `INSUFFICIENT_DATA`, `NOT_COMPARABLE_CURRENCY`)
   - `missing_cost_components`: Lista explícita de componentes ausentes.
2. **`ProfitDashboardSummary`**:
   - `total_items`, `complete_items`, `partial_items`, `insufficient_data_items`, `negative_margin_items`
   - `average_contribution_margin_pct` (`Decimal` sobre ítems completos)
   - `total_projected_gross_profit`, `total_projected_contribution_profit`
   - Agregaciones: `items_by_marketplace`, `items_by_completeness`, `items_by_currency`
3. **`ProfitComparisonResult`**:
   - Comparación multi-ítem con desglose, ranking ordenado por margen y advertencia si los datos son heterogéneos o incompletos.

---

## 5. REGLAS MONETARIAS Y SEMÁNTICA UNKNOWN
- **Decimal Precision:** Todos los campos monetarios utilizan `Decimal`. Toda división cuenta con salvaguardas contra división por cero (`sale_price == 0` o `unit_cost == 0` retornan `None`).
- **UNKNOWN != 0:** Si `shipping_cost` es `None`, no se asume envío gratis ($0); se excluye de la suma de costos conocidos y se agrega a `missing_cost_components`, degradando la completitud a `PARTIAL`.
- **Currency Mismatch:** Operaciones entre monedas distintas (ej. `USD` vs `CLP`) sin tasa FX certificada impiden la suma directa y marcan el ítem como `NOT_COMPARABLE_CURRENCY`.
- **Margen Negativo:** Si los costos superan el precio de venta, `contribution_profit` y `margin_pct` negativos se preservan explícitamente sin truncamiento a cero.

---

## 6. SERVICE LAYER, FILTROS Y ORDENACIÓN DETERMINISTA
En `src/application/profit_dashboard/profit_dashboard_service.py` se implementó `ProfitDashboardService`:
- **Filtros Multi-Criterio:**
  - `marketplace`, `category`, `supplier_id`, `opportunity_id`
  - `completeness` (`COMPLETE`, `PARTIAL`, `INSUFFICIENT_DATA`, `NOT_COMPARABLE_CURRENCY`)
  - Rangos: `min_margin_pct`, `max_margin_pct`, `min_profit`, `max_profit`
  - `currency`, `search` (título/identificador)
- **Ordenación Determinista:**
  - Campos: `margin`, `profit`, `gross_profit`, `sale_price`, `cost`, `updated_at`
  - Direcciones: `asc`, `desc`
  - Criterio de Nulos: Valores `None` van siempre al final.
  - Desempate determinista secundario por `item_id`.
- **Paginación Segura:** Parámetros `page >= 1` y `page_size` acotado en $[1, 100]$.

---

## 7. EXPLAINABILITY & DETAIL BREAKDOWN
El método `get_profit_breakdown(tenant_id, item_id)` genera una explicación detallada y auditable:
- Desglose contable paso a paso:
  ```text
    Sale Price:             $ 100.00 USD
  - Unit Cost:            - $  40.00 USD
  ---------------------------------------
  = Gross Profit:         = $  60.00 USD
  - Shipping Cost:        - $  10.00 USD
  - Marketplace Fee:      - $  15.00 USD
  - Payment Fee:          - $   5.00 USD
  ---------------------------------------
  = Contribution Profit:  = $  30.00 USD (Margin: 30.00%)
  ```
- Componentes omitidos o desconocidos listados explícitamente.

---

## 8. TENANT ISOLATION & AUTHORIZATION RBAC
- **Aislamiento Multi-Tenant (O.1):** El servicio valida `tenant_id` en todas las operaciones (`list`, `get`, `summary`, `compare`, `save`). Acceso cruzado entre tenants es bloqueado con error 403 / segregación física.
- **RBAC (O.4 / N.4):**
  - Permiso requerido: `PROFIT_DASHBOARD_READ` (o `BUSINESS_INTELLIGENCE_READ` / `SYSTEM_ADMIN`).
  - Respuestas: 401 Unauthorized para peticiones no autenticadas; 403 Forbidden para usuarios sin rol asignado.

---

## 9. API REST & ADMIN CONSOLE UI
En `src/infrastructure/web/admin_app.py` y `src/infrastructure/web/app.py` se montaron:
- **Endpoints REST API:**
  - `GET /api/bi/tenants/{tenant_id}/profit` — Lista paginada con filtros y ordenación.
  - `GET /api/bi/tenants/{tenant_id}/profit/summary` — Resumen estadístico y distribuciones.
  - `GET /api/bi/tenants/{tenant_id}/profit/{item_id}` — Detalle del ítem con desglose financiero explicable.
  - `POST /api/bi/tenants/{tenant_id}/profit/compare` — Comparación multidimensional de ítems.
- **Admin Console UI View:**
  - `GET /bi/profit` — Interfaz web responsive con tarjetas resumen de métricas, alertas de margen negativo, tabla con badges de completitud y modal de desglose unit economics.

---

## 10. EVIDENCIA DE EJECUCIÓN Y PRUEBAS

### Pruebas Unitarias de Q.3:
```bash
python -m pytest tests/unit/test_q3_profit_dashboard_unit.py -vv
```
**Resultado:** `15 passed in 0.45s`

### Pruebas de Integración de Q.3:
```bash
python -m pytest tests/integration/test_q3_profit_dashboard_integration.py -vv
```
**Resultado:** `11 passed in 1.25s`

### Regresión Completa de Hito Q (Q.1, Q.2, Q.3):
```bash
python -m pytest tests/unit/test_q1_opportunity_dashboard_unit.py tests/integration/test_q1_opportunity_dashboard_integration.py tests/unit/test_q2_supplier_dashboard_unit.py tests/integration/test_q2_supplier_dashboard_integration.py tests/unit/test_q3_profit_dashboard_unit.py tests/integration/test_q3_profit_dashboard_integration.py -vv
```
**Resultado:** `62 passed in 3.10s` (Cero fallos)

### Validación de Despliegue y Base de Datos:
```bash
.venv/Scripts/python.exe scripts/deploy_validate.py
.venv/Scripts/python.exe scripts/db_migrate.py check
```
**Resultado:**
- `>>> ALL O.13 DEPLOYMENT AUTOMATION VALIDATIONS PASSED SUCCESSFULY. <<<`
- `[MIGRATION_CHECK] Schema is UP TO DATE (revision: 001_initial_saas_schema).`

### Full Regression Suite:
```bash
python -m pytest
```
**Resultado:**
```
============================== 2517 passed, 18 skipped, 211 warnings in 177.62s ==============================
```
- Total de pruebas ejecutadas: 2535
- Pruebas exitosas: 2517
- Pruebas omitidas (skipped condicionales): 18
- Pruebas fallidas: 0 (Cero regresiones)

---

## 11. ESTADO ACTUAL Y SIGUIENTE TAREA

- **Q.1 Opportunity Dashboard:** 🟢 VALIDADA
- **Q.2 Supplier Dashboard:** 🟢 VALIDADA
- **Q.3 Profit Dashboard:** 🟢 VALIDADA
- **Hito Q — Business Intelligence:** 🟡 EN PROGRESO (3/6 tareas completadas)
- **Gate P:** ⚪ PENDIENTE

### Próxima Tarea del Roadmap:
- **Q.4 ID:** `Q.4`
- **Q.4 Nombre Exacto:** `Mission Dashboard`
- **Q.4 Estado:** `⚪ PENDIENTE`
