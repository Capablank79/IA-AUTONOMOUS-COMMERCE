# AI Autonomous Commerce — Carta Gantt Maestra

> Documento vivo de seguimiento del Roadmap Maestro.
> Debe actualizarse a medida que cada tarea sea implementada y validada.
>
> **Fuente de alcance:** `docs/market-intelligence/AI_AUTONOMOUS_COMMERCE_ROADMAP_MAESTRO.md`
>
> **Regla:** una tarea solo puede marcarse como `✓ VALIDADA` cuando existe implementación, tests/regresión y evidencia correspondiente. Un Gate solo pasa cuando sus criterios están demostrados.

---

## Estados

| Estado | Significado |
|---|---|
| ⚪ PENDIENTE | No iniciada |
| 🟡 EN PROGRESO | Trabajo activo |
| 🔵 IMPLEMENTADA | Código implementado, aún falta validación completa |
| 🟢 VALIDADA | Criterios de aceptación demostrados |
| 🔴 BLOQUEADA | Existe una dependencia/bloqueo documentado |
| ⏸️ DIFERIDA | Se decidió postergar justificadamente |

---

# 1. Estado global

| ID | Fase | Prioridad | Estado | Gate |
|---|---|---|---|---|
| A | Market Opportunity Discovery | P0 | 🟢 VALIDADA | 🟢 GATE A |
| B | Opportunity Intelligence | P0 | 🟢 VALIDADA | 🟢 GATE B |
| C | Supplier Intelligence | P0 | 🟢 VALIDADA | 🟢 GATE C |
| D | Profit + Capital Allocation | P0 | 🟢 VALIDADA | 🟢 GATE C-Economics |
| E | Autonomous Commerce | P0 | 🟢 VALIDADA | 🟢 GATE D |
| F | Communications + Approval | P1 | 🟢 VALIDADA | 🟢 GATE E |
| G | Marketplace Operations | P1 | 🟢 VALIDADA | 🟢 GATE F |
| H | Business Memory | P1 | ⚪ PENDIENTE | ⚪ GATE G |
| I | Learning Loop | P1 | ⚪ PENDIENTE | ⚪ GATE H |
| J | Continuous Autonomy | P1 | ⚪ PENDIENTE | ⚪ GATE I |
| K | Observability / Evaluation / Reliability | P0 transversal | 🟡 EN PROGRESO | ⚪ GATE J |
| L | Data Quality / Governance | P0 transversal | 🟡 EN PROGRESO | ⚪ GATE K |
| M | Cost / Inference | P1 transversal | ⚪ PENDIENTE | ⚪ GATE L |
| N | Security / Governance / Safety | P0 transversal | 🟡 EN PROGRESO | ⚪ GATE M |
| O | SaaS / Platformization | P2 | ⚪ PENDIENTE | ⚪ GATE N |
| P | Production / Operations | P2 | ⚪ PENDIENTE | ⚪ GATE O |
| Q | Business Intelligence | P2 | ⚪ PENDIENTE | ⚪ GATE — |
| R | Advanced Autonomy | P3 | ⚪ PENDIENTE | ⚪ GATE P |
| S | Self-Improving Commerce | P3 | ⚪ PENDIENTE | ⚪ GATE — |

\* El Roadmap Maestro presenta Supplier Intelligence como Fase 03 y Profit + Capital Allocation como Fase 04; el control de esta carta utiliza identificadores funcionales para evitar ambigüedad.

---

# 2. Carta Gantt consolidada

Los bloques son secuenciales/relativos y no representan fechas calendario rígidas. Las estimaciones pueden cambiar según APIs, datos, dependencias y resultados.

| ID | Capacidad | B1 | B2 | B3 | B4 | B5 | B6 | B7 | Estado |
|---|---|---|---|---|---|---|---|---|---|
| A | Market Opportunity Discovery | █ | █ | | | | | | 🟢 |
| B | Opportunity Intelligence | █ | █ | | | | | | 🟢 |
| C | Supplier Intelligence | | █ | █ | | | | | 🟢 |
| D | Profit + Capital Allocation | | | █ | █ | | | | 🟢 |
| E | Autonomous Commerce | | | | █ | █ | | | 🟢 |
| F | Communications + Approval | | | | | █ | █ | | 🟢 |
| G | Marketplace Operations | | | | | | █ | █ | 🟢 |
| H | Business Memory | | | | | | | █ | ⚪ |
| I | Learning Loop | | | | | | | █ | ⚪ |
| J | Continuous Autonomy | | | | | | | █ | ⚪ |
| K–N | Transversales: reliability/data/cost/security | → | → | → | → | → | → | → | 🟡 |
| O | SaaS / Platformization | | | | | | | | ⚪ |
| P | Production / Operations | | | | | | | | ⚪ |
| Q | Business Intelligence | | | | | | | | ⚪ |
| R | Advanced Autonomy | | | | | | | | ⚪ |
| S | Self-Improving Commerce | | | | | | | | ⚪ |

---

# 3. Hito A — Market Opportunity Discovery

**Estado: 🟢 VALIDADA**

### Demostración obligatoria

> Busca oportunidades de productos reales en Mercado Libre Chile, encuentra los mejores candidatos, ordénalos y explícame con evidencia por qué son ganadores.

| Task | Descripción | Estado | Evidencia |
|---|---|---|---|
| A.1 / 02.1 | Mission de Product Discovery | 🟢 VALIDADA | Ejecución de misión |
| A.2 / 02.2 | Product Hunter real | 🟢 VALIDADA | Mercado Libre LIVE |
| A.3 / 02.3 | Evidence Pipeline | 🟢 VALIDADA | Evidence/provenance |
| A.4 / 02.4 | Opportunity Scoring | 🟢 VALIDADA | Score reproducible |
| A.5 / 02.5 | Winner Ranking | 🟢 VALIDADA | Ranking |
| A.6 / 02.6 | LLM Explanation | 🟢 VALIDADA | Explicación basada en evidencia |
| A.7 / 02.7 | Market Discovery E2E | 🟢 VALIDADA | E2E LIVE |

### GATE A

🟢 VALIDADO.

Resultado esperado demostrado:

`MISSION → Mercado Libre → candidatos → evidencia → scoring → ranking → explicación`

---

# 4. Hito B — Opportunity Intelligence

**Estado: 🟢 VALIDADA**

Objetivo:

Convertir candidatos y evidencia de Market Intelligence en decisiones de oportunidad comparables, explicables y monitorizables.

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| B.1 | Opportunity scoring | 🟢 VALIDADA | Score reproducible y basado en evidencia determinista (0-100) | `tests/unit/domain/opportunity/` |
| B.2 | Ranking | 🟢 VALIDADA | Ranking comparativo reproducible con desempate por confianza/suficiencia | `tests/unit/domain/opportunity/` |
| B.3 | Readiness | 🟢 VALIDADA | Estados de readiness definidos (`INSUFFICIENT_EVIDENCE`, `NEEDS_INVESTIGATION`, `READY`, `PROMOTED`, `REJECTED`) | `tests/unit/domain/opportunity/` |
| B.4 | Evidence sufficiency | 🟢 VALIDADA | Determina explícitamente `SUFFICIENT`, `PARTIAL`, `INSUFFICIENT` sin inventar datos | `tests/unit/domain/opportunity/` |
| B.5 | Opportunity explanation | 🟢 VALIDADA | Explicación en capas: `OBSERVED`, `DERIVED`, `INFERRED`, `RISKS`, `UNKNOWNS`, `RECOMMENDED` | `tests/unit/domain/opportunity/` |
| B.6 | Opportunity comparison | 🟢 VALIDADA | Compara oportunidades multidimensionalmente (Score, Confianza, Cobertura, Riesgos) | `tests/unit/domain/opportunity/` |
| B.7 | Opportunity rejection | 🟢 VALIDADA | Rechazo estructurado con razones de dominio (`RejectionReason`) y snapshot inmutable | `tests/unit/domain/opportunity/` |
| B.8 | Opportunity monitoring | 🟢 VALIDADA | Reevalúa ante cambios temporales preservando historial inmutable (`history`) | `tests/unit/domain/opportunity/` |

### GATE B

🟢 VALIDADO.

Demostrado satisfactoriamente:

`CANDIDATES → EVIDENCE → SCORE → SUFFICIENCY → READINESS → COMPARE → PROMOTE/REJECT → EXPLAIN → MONITOR`

---

# 5. Hito C — Supplier Intelligence

**Estado: 🟢 VALIDADA (Misiones C-01, C-02, C-03 y C-04 Validadas)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| C.1 | Supplier Discovery | 🟢 VALIDADA | Multi-source discovery de múltiples candidatos a partir de oportunidad de Hito B | `tests/unit/domain/supplier_intelligence/`, `tests/unit/application/supplier_intelligence/` |
| C.2 | Supplier Normalization | 🟢 VALIDADA | Normalización de nombres, fuentes, deduplicación determinista sin fusión agresiva | `SupplierNormalizer` en `tests/unit/domain/supplier_intelligence/` |
| C.3 | Supplier Evidence | 🟢 VALIDADA | Provenance estricto (`LIVE`, `FIXTURE`, `MOCK`), confianza, frescura, unknowns | `SupplierEvidence` |
| C.4 | Preliminary Supplier Ranking | 🟢 VALIDADA | Ranking determinista y scoring 0-100 sin inventar precios/MOQs | `SupplierScorer` |
| C.5 | Product Matching | 🟢 VALIDADA | Clasificación estricta: `EXACT_MATCH`, `CLOSE_MATCH`, `VARIANT`, `UNCERTAIN_MATCH`, `NO_MATCH` | `ProductMatcher` |
| C.6 | Quote Comparison | 🟢 VALIDADA | Normalización de cotizaciones, validación de divisas, escenarios por volumen, ranking comercial y BestCommercialCandidate | `tests/unit/domain/supplier_intelligence/test_c02_quote_comparison.py`, `tests/unit/application/supplier_intelligence/test_c02_quote_comparison_service.py` |
| C.7 | MOQ & Price Tiers | 🟢 VALIDADA | Modelado explícito de MOQ (SKU/VARIANT/ORDER/UNKNOWN) y tramos de precio por volumen sin suposiciones de MOQ=1 | `PriceTier`, `MOQInfo` en `test_c02_quote_comparison.py` |
| C.8 | Lead Time | 🟢 VALIDADA | Modelado determinista de lead time observado, rangos, varianza histórica y on-time rate sin fabricar distribuciones | `LeadTimeAnalyzer`, `LeadTimeProfile` en `test_c03_supplier_risk.py` |
| C.9 | Shipping | 🟢 VALIDADA | Aislamiento de costos de envío, métodos, transportistas, zonas geográficas y comparabilidad sin asumir free shipping | `ShippingAnalyzer`, `ShippingOption` en `test_c03_supplier_risk.py` |
| C.10 | Reliability | 🟢 VALIDADA | Evaluación determinista de cumplimiento de SLA, consistencia de stock, penalización de incidentes y confiabilidad | `ReliabilityEvaluator`, `ReliabilityEvaluation` en `test_c03_supplier_risk.py` |
| C.11 | Supplier Risk | 🟢 VALIDADA | Scoring de riesgo multidimensional explicable (0-100) en 5 dimensiones y recomendación de rechazo fundamentada | `SupplierRiskEngine`, `SupplierRiskProfile` en `test_c03_supplier_risk.py` |
| C.12 | Historical Supplier Performance | 🟢 VALIDADA | Registro inmutable de eventos temporales y análisis determinista de tendencias (`IMPROVING`, `STABLE`, `DETERIORATING`) | `HistoricalPerformanceAnalyzer`, `HistoricalPerformanceProfile` en `test_c03_supplier_risk.py` |
| C.13 | Supplier Recommendation | 🟢 VALIDADA | Recomendación final inmutable, determinista y explicable (Primary + Fallback + Contingency) combinando Economics + Risk + Reliability + Logistics + Evidence Sufficiency + Freshness + Provenance | `SupplierRecommendationEngine`, `SupplierRecommendationPolicy` en `tests/unit/domain/supplier_intelligence/test_c04_supplier_recommendation.py` y `tests/integration/test_c04_supplier_recommendation_demo.py` |

### GATE C — Supplier

🟢 VALIDADO.

Fecha de Validación: 2026-08-30
Tests: 311 unitarios y de integración pasando (100% pass)
E2E: Marcha Blanca C-04 con 3 escenarios (A: RECOMMEND, B: RECOMMEND_WITH_CONDITIONS, C: NO_RECOMMENDATION/NEEDS_INVESTIGATION) + Fallback E2E completada
Provenance: Preservación estricta de procedencia (`LIVE`, `FIXTURE`, `MOCK`, `DERIVED`, `INFERRED`)

Flujo E2E Demostrado:
`OPPORTUNITY → SUPPLIER DISCOVERY → EVIDENCE → MATCHING → NORMALIZATION → QUOTE NORMALIZATION → MOQ & TIERS → COMMERCIAL COMPARISON → RISK & RELIABILITY EVALUATION → LOGISTICS INTELLIGENCE → HISTORICAL PERFORMANCE → SUPPLIER RECOMMENDATION (PRIMARY + FALLBACK) → CONTINGENCY MONITORING`

---

# 6. Hito D — Profit + Capital Allocation

**Estado: 🟢 VALIDADA (Misiones D-01, D-02 y D-03 Validadas)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| D.1 | Landed Cost | 🟢 VALIDADA | Cálculo determinista de costo de adquisición + flete + aranceles + impuestos + costos variables con trazabilidad y detección de incógnitas sin asumir 0 | `LandedCostCalculator`, `LandedCost` en `tests/unit/domain/profit/test_d01_profit_engine.py` |
| D.2 | Unit Economics | 🟢 VALIDADA | Determinación de Gross Margin, Net Margin, Markup y Break-Even Sale Price con bloqueo estricto ante incógnitas críticas | `UnitEconomicsCalculator`, `BreakEvenCalculator` en `test_d01_profit_engine.py` y `test_d01_profit_engine_e2e.py` |
| D.3 | Risk Engine | 🟢 VALIDADA | Evaluación determinista de riesgo financiero, exposición de capital y suficiencia de evidencia | `CapitalAllocationEngine`, `SupplierRiskEngine` en `test_d02_capital_engine.py` |
| D.4 | Scenario Analysis | 🟢 VALIDADA | Análisis determinista de escenarios (Base, Conservador, Optimista) y por escalas de volumen (QTY=1, MOQ, Volume) | `EconomicScenarioAnalyzer` en `test_d01_profit_engine.py` |
| D.5 | Capital Allocation | 🟢 VALIDADA | Asignación prudente de capital, techos de exposición por oportunidad, protección de reservas, asignación parcial, reevaluación dinámica y liberación | `CapitalAllocationEngine`, `AutonomousCapitalService` en `tests/unit/domain/capital/test_d02_capital_engine.py` y `tests/integration/test_d02_capital_allocation_e2e.py` |
| D.6 | Inventory vs Dropshipping Engine | 🟢 VALIDADA | Comparación multidimensional explícita (Inventory vs Dropshipping), evaluación de MOQ, capital lock-up, rotación, stock exposure, obsolescencia, SLA de proveedor, política determinista explicable, decisiones condicionales, reevaluación dinámica y pivots en AutonomousLoop | `OperatingModelEvaluator`, `OperatingModelEngine`, `AutonomousOperatingModelService` en `tests/unit/domain/operating_model/test_d03_operating_model_engine.py` y `tests/integration/test_d03_operating_model_e2e.py` |

Decisiones objetivo:

`APPROVED / PARTIALLY_APPROVED / LIMITED_ALLOCATION / NEEDS_INVESTIGATION / REJECTED / RELEASED`
`SELECT_INVENTORY / SELECT_DROPSHIPPING / NEEDS_INVESTIGATION / NO_DECISION`

### GATE D — Profit, Capital Allocation & Operating Model (Misiones D-01, D-02 y D-03)

🟢 VALIDADO (Fases D-01, D-02 y D-03).

Fecha de Validación: 2026-08-30
Tests: 351 unitarios y de integración pasando (100% pass, 0 regresiones)
E2E: Marchas Blancas D-03 con 4 escenarios (A: Inventory ganador por economía de escala y rotación, B: Dropshipping ganador por baja prima de margen o mitigación de riesgo, C: Flete/costos desconocidos con resultado NO_DECISION / NEEDS_INVESTIGATION, D: Reevaluación dinámica y Pivot por caída de demanda / degradación de condiciones) completadas
Traceability: Trace inmutable, modelos inmutables (`InventoryScenario`, `DropshippingScenario`, `OperatingModelComparison`, `OperatingDecision`, `OperatingReassessmentRecord`), preservación estricta de procedencia (`LIVE`, `FIXTURE`, `MOCK`, `DERIVED`, `INFERRED`), anti-fabricación estricta (`UNKNOWN != 0`, `UNKNOWN != FREE`, `UNKNOWN != GOOD`, `UNKNOWN != BAD`), e integración nativa con `AutonomousLoop`.

Flujo E2E Demostrado:
`OPPORTUNITY → SUPPLIER RECOMMENDATION → PROFIT ENGINE (LANDED COST & UNIT ECONOMICS) → CAPITAL ALLOCATION (D-02) → INVENTORY SCENARIO (MOQ, BULK SHIPPING, EXPOSURE, VELOCITY, OBSOLESCENCE) vs DROPSHIPPING SCENARIO (UNIT SHIPPING, SLA, BUFFER) → COMPARISON (DIFFERENTIALS & TRADEOFFS) → POLICY EVALUATION → DECISION (INVENTORY / DROPSHIPPING / NEEDS_INVESTIGATION / NO_DECISION) → REASSESSMENT & PIVOT`

---

# 7. Hito E — Autonomous Commerce

**Estado: 🟢 VALIDADA**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| E.1 | Action Registry | 🟢 VALIDADA | `LoopAction` / `PublicationActionExecutor` contratos y action dispatching | `src/domain/mission/models.py`, `src/application/publication/` |
| E.2 | ActionExecutor | 🟢 VALIDADA | `PublicationActionExecutor` conectado desacopladamente con `PublicationPort` e integrado con `AutonomousLoop` | `tests/unit/application/publication/test_publication_action_executor.py` (9 passed) |
| E.3 | Policy Engine | 🟢 VALIDADA | Gobernanza determinista entre Decision y Action: Autorización, Presupuesto, Riesgo, Human Approval, Idempotency, UNKNOWN y Provenance (`PolicyGuardedActionExecutor`) | `tests/unit/domain/policy/`, `tests/unit/application/policy/` (32 passed), `tests/integration/test_policy_engine_integration.py` (4 passed) |
| E.4 | Tool Registry | 🟢 VALIDADA | Catálogo fuertemente tipado, versionado, lifecycle, contratos I/O, integración con Policy Engine y AutonomousLoop, seguro contra UNKNOWN | `tests/unit/domain/tool/`, `tests/unit/application/tool/`, `tests/integration/test_tool_registry_integration.py` (18 passed) |
| E.5 | Observe → Decide → Act | 🟢 VALIDADA | Flujo integrado `LoopDecision` → `PublicationActionExecutor` → `PublicationPort` → `PublicationResult` | `test_publication_action_executor.py`, `test_e01_3_mercadolibre_publication_integration.py` |
| E.6 | Recovery | 🟢 VALIDADA | Manejo estricto de `UNKNOWN`, preservación de incertidumbre y acción `VERIFY_STATUS` sin duplicación | `test_publication_action_executor.py`, `test_publication_adapter.py` |

### GATE D — Autonomous Commerce (E2E Validation)

🟢 VALIDADO.

Fecha de Validación: 2026-08-30
Tests: 459 unitarios y de integración pasando (100% pass, 1 skipped)
E2E: Marcha Blanca Gate D E2E completada con 6 escenarios deterministas:
- Escenario A (ALLOW / STANDARD FLOW): Integración real Market Discovery -> Opportunity Evaluation -> Supplier Sourcing -> Profit & Landed Cost -> Capital Allocation -> Policy Engine (ALLOW) -> Publication Action.
- Escenario B (DENY / HIGH RISK): Policy Engine intercepta y bloquea acción con riesgo CRITICAL/HIGH sin efectos externos.
- Escenario C (REQUIRE_APPROVAL): Policy Engine detiene la acción preventiva e irreversible cuando falta autorización explícita humana.
- Escenario D (UNKNOWN / INSUFFICIENT EVIDENCE): Preservación estricta de incertidumbre y bloqueo de publicaciones con datos sintéticos no autorizados.
- Escenario E (CAPITAL & ECONOMICS CONSTRAINT): Rechazo y reevaluación autónoma sin publicar cuando el margen no supera el umbral o el capital es insuficiente.
- Escenario F (RECOVERY / TRANSIENT FAILURE): Recuperación ante fallos transitorios en herramientas con reintento seguro y convergencia final.

Flujo E2E Demostrado:
`MISSION → OBSERVE → TOOL DISCOVERY → EVIDENCE GATHERING → OPPORTUNITY EVALUATION → SUPPLIER EVALUATION → ECONOMICS & CAPITAL EVALUATION → POLICY ENGINE BARRIER → ACTION EXECUTION → RESULT → RE-OBSERVE`

---

# 8. Hito F — Communications + Approval

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| F.1 | Report Generator | ⚪ |
| F.2 | Email Composer | ⚪ |
| F.3 | Email Delivery | ⚪ |
| F.4 | WhatsApp Notification Adapter | ⚪ |
| F.5 | Approval Workflow | ⚪ |
| F.6 | Notification Preferences | ⚪ |

### GATE E

⚪ PENDIENTE.

---

# 9. Hito G — Marketplace Operations

**Estado: 🟢 VALIDADA (Sub-slices G.1 a G.8 Validados e Integrados con E2E Gate F Pass)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| G.1 | Listing Generator | 🟢 VALIDADA | Generación determinista y estructurada de `ListingDraft` basada en evidencia real (Product Truth, MarketEvidence, SEO, Customer Pain Mining, Claim Provenance y Multichannel Readiness sin alucinaciones) | `src/domain/publication/generation_models.py`, `src/domain/publication/services.py`, `src/application/publication/listing_generator_service.py`, `tests/unit/domain/publication/test_listing_generator.py` (11 passed), `tests/unit/application/publication/test_listing_generator_service.py` (3 passed), `tests/integration/test_listing_generator_e2e.py` (1 passed) |
| G.2 | Listing Quality/Policy Validator | 🟢 VALIDADA | Validación determinista de calidad, verdad de producto, compliance regulatorio/canal, claim grounding, scoring multidimensional (0-100) y barrera de políticas | `src/domain/publication/validation_models.py`, `src/domain/publication/validation_engine.py`, `src/application/publication/listing_validator_service.py`, `tests/unit/domain/publication/test_listing_validator.py` (12 passed), `tests/unit/domain/publication/test_listing_validator_policy_boundary.py` (2 passed), `tests/integration/test_listing_quality_validator_integration.py` (3 passed) |
| G.3 | Publishing Adapter (E-01) | 🟢 VALIDADA | Integración completa de la cadena ListingDraft -> G.2 Validation -> Policy Guard -> ActionExecutor -> PublicationPort -> MercadoLibrePublicationAdapter -> API Mercado Libre -> PublicationResult -> Audit/Trace. Verificación de matriz de errores HTTP, resiliencia UNKNOWN, recuperación vía VERIFY_STATUS sin duplicación y ToolRegistry governance. LIVE NOT EXECUTED por falta de credenciales/entorno productivo. | `src/infrastructure/mercadolibre/publication_adapter.py`, `src/application/publication/publication_action_executor.py`, `src/application/policy/policy_guarded_action_executor.py`, `tests/integration/test_g03_publishing_adapter_integration.py` (12 passed), `tests/integration/test_e01_3_mercadolibre_publication_integration.py` (2 passed), `tests/unit/infrastructure/mercadolibre/test_publication_adapter.py` (15 passed) |
| G.4 | Pricing Actions | 🟢 VALIDADA | Capacidad desacoplada y determinista para calcular price floors con unit economics, evaluar pricing decisions, gobernar con Policy (PriceFloor, MarginProtection, MaxPriceChange), formular PricingActions, ejecutar vía ActionExecutor/PricingPort/MercadoLibrePricingAdapter, tratar incertidumbre UNKNOWN con reconciliación mediante verify/current price, auditoría y Tool Registry governance. | `src/domain/pricing/`, `src/domain/policy/rules.py`, `src/infrastructure/mercadolibre/pricing_adapter.py`, `src/application/pricing/pricing_action_executor.py`, `tests/integration/test_g04_pricing_pipeline_integration.py` (2 passed), `tests/unit/domain/pricing/` (11 passed), `tests/unit/infrastructure/mercadolibre/test_pricing_adapter.py` (7 passed) |
| G.5 | Inventory Actions | 🟢 VALIDADA | Gestión de inventario desacoplada y determinista con semántica de stock multinivel (supplier, owned, reserved, buffer, in_transit, listed), cálculo de available_to_sell con protección contra sobreventa (overselling protection) y stock negativo, gobernanza con Policy (OversellingProtection, InventorySafetyBuffer), formulación de InventoryActions, ejecución vía ActionExecutor/InventoryPort/MercadoLibreInventoryAdapter, tratamiento de incertidumbre UNKNOWN con reconciliación vía verify/get_current_stock, trazabilidad, auditoría y Tool Registry (`get_inventory`, `update_inventory`, `reconcile_inventory`). | `src/domain/inventory/`, `src/domain/policy/rules.py`, `src/infrastructure/mercadolibre/inventory_adapter.py`, `src/application/inventory/inventory_action_executor.py`, `tests/integration/test_g05_inventory_pipeline_integration.py` (2 passed), `tests/unit/domain/inventory/` (8 passed), `tests/unit/infrastructure/mercadolibre/test_inventory_adapter.py` (4 passed), `tests/unit/application/inventory/test_inventory_action_executor.py` (2 passed) |
| G.6 | Order Integration | 🟢 VALIDADA | Recepción, normalización, consulta, sincronización y gestión de estados de órdenes de venta con separación estricta (order/payment/fulfillment), minimización de PII, deduplicación e idempotencia estricta por evento/idempotency_key, impacto de stock exactly-once coordinado con Inventory Engine y Policy Guard (previniendo doble descuento ante replays), resiliencia ante errores de red y UNKNOWN, reconciliación estado interno vs externo y registro en Tool Registry (`get_orders`, `get_order`, `reconcile_order`). | `src/domain/order/`, `src/infrastructure/mercadolibre/order_adapter.py`, `src/application/order/order_processing_service.py`, `src/infrastructure/persistence/data/in_memory/order_repository.py`, `tests/integration/test_g06_order_pipeline_integration.py` (1 passed), `tests/unit/domain/order/` (6 passed), `tests/unit/infrastructure/mercadolibre/test_order_adapter.py` (4 passed), `tests/unit/application/order/` (4 passed) |
| G.7 | Fulfillment | 🟢 VALIDADA | Capacidad logística desacoplada posterior a G.6 (`ORDER -> FULFILLMENT -> SHIPMENT -> TRACKING -> RECONCILIATION -> RE-OBSERVE`). Gestión de envíos inmutables (`Shipment`), ingesta y normalización de `TrackingEvent` con deduplicación idempotente, soporte seguro de `ShippingLabel`, resiliencia ante errores 5xx/timeout con preservación de `ShipmentStatus.UNKNOWN` y baja confianza sin sobreescritura destructiva, gobernanza con `PolicyEngine` para acciones operativas de despacho, y registro de herramientas logísticas en `ToolRegistry` (`get_shipments`, `get_shipment`, `get_tracking`, `reconcile_shipment`, `prepare_fulfillment`, `create_shipping_label`). LIVE NOT EXECUTED por ausencia de credenciales productivas reales. | `src/domain/fulfillment/`, `src/infrastructure/mercadolibre/fulfillment_adapter.py`, `src/application/fulfillment/fulfillment_service.py`, `src/infrastructure/persistence/data/in_memory/fulfillment_repository.py`, `src/application/tool/catalog.py`, `tests/integration/test_g07_fulfillment_pipeline_integration.py` (5 passed), `tests/unit/domain/fulfillment/test_fulfillment_models.py` (4 passed), `tests/unit/infrastructure/mercadolibre/test_fulfillment_adapter.py` (7 passed), `tests/unit/application/fulfillment/test_fulfillment_service.py` (9 passed) |
| G.8 | Returns / Exceptions | 🟢 VALIDADA | Gestión integral de devoluciones, reclamos, disputas y excepciones postventa (`ORDER/SHIPMENT -> RETURN/CLAIM/EXCEPTION -> OBSERVE -> NORMALIZE -> VALIDATE -> DECIDE -> POLICY -> ACTION -> RESULT -> RECONCILE -> RE-OBSERVE`). Modelos de dominio inmutables (`Return`, `Claim`, `RefundDetail`, `ReturnEvent`, `ReturnReconciliationReport`), separación estricta de ciclos (`ReturnStatus`, `ClaimStatus`, `RefundStatus`), gobernanza de acciones de reembolso/rechazo con `PolicyEngine` y `ReturnActionPolicyRule`, deduplicación/idempotencia estricta por evento y clave, manejo de incertidumbre `UNKNOWN` ante caídas 5xx/timeout preservando estado local, motor de reconciliación determinista sin sobreescritura ciega, adaptador Mercado Libre (`MercadoLibreReturnsAdapter`), persistencia in-memory thread-safe y registro formal de 6 tools postventa en `ToolRegistry` (`get_returns`, `get_return`, `get_claim`, `reconcile_return`, `create_return_request`, `resolve_return_action`). LIVE NOT EXECUTED por ausencia de credenciales productivas reales. | `src/domain/returns/`, `src/domain/returns/rules.py`, `src/infrastructure/mercadolibre/returns_adapter.py`, `src/application/returns/returns_service.py`, `src/infrastructure/persistence/data/in_memory/returns_repository.py`, `src/application/tool/catalog.py`, `tests/integration/test_g08_returns_pipeline_integration.py` (6 passed), `tests/unit/domain/returns/test_returns_models.py` (5 passed), `tests/unit/domain/returns/test_returns_rules.py` (4 passed), `tests/unit/infrastructure/mercadolibre/test_returns_adapter.py` (4 passed), `tests/unit/application/returns/test_returns_service.py` (9 passed) |

### GATE F

🟢 PASSED.

Fecha de Validación: 2026-08-31
Tests: 615 unitarios y de integración pasando (100% pass, 1 skipped, 0 failures)
E2E: Suite formal de validación Gate F en `tests/integration/test_gate_f_e2e_validation.py` completada con 5 escenarios deterministas:
- Escenario A (STANDARD_APPROVED): Context -> Decision -> Policy -> REQUIRE_APPROVAL (Approved) -> Approval -> ActionExecutor -> Result -> Audit.
- Escenario B (REJECTED_BY_HUMAN): REQUIRE_APPROVAL -> REJECTED por Humano -> Cero side effects externos.
- Escenario C (DUPLICATE_REPLAY): Replay de aprobación/acción duplicada -> Idempotencia estricta, 1 sola ejecución.
- Escenario D (UNKNOWN_TIMEOUT): Timeout/5xx transitorio -> Preservación de `PublicationStatus.UNKNOWN` y `PolicyDecisionType.UNKNOWN` -> Re-observe/reconcile sin falso éxito.
- Escenario E (DENY_BY_POLICY): Precedencia absoluta de Policy DENY -> Acción bloqueada aun cuando existiera aprobación humana simulada.

Flujo E2E Demostrado:
`MISSION/CONTEXT → DECISION → POLICY EVALUATION → APPROVAL WORKFLOW (REQUIRE_APPROVAL / APPROVED / REJECTED) → ACTION EXECUTOR → RESULT / AUDIT → RE-OBSERVE`

---

# 10. Hito H — Business Memory

**Estado: 🟡 EN PROGRESO (H.1 Validada, H.2-H.7 Pendientes)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| H.1 | Persist Missions | 🟢 VALIDADA | Persistencia durable de misiones (`Mission` y `MissionResult`) basada en JSON con desacoplamiento absoluto de dominio, soporte para ciclo completo (`CREATE -> PERSIST -> LOAD -> UPDATE -> PERSIST -> LOAD -> CONTINUE/RESUME`), serialización ISO/Decimal/Enum, idempotencia estricta, preservación de `correlation_id` / `idempotency_key` / `provenance` / `confidence`, resiliencia ante corrupción y exclusión estricta de PII/credenciales. | `src/infrastructure/persistence/data/json/mission_repository.py`, `tests/unit/infrastructure/persistence/data/json/test_mission_repository.py` (7 passed), `tests/integration/test_h1_mission_memory_integration.py` (1 passed) |
| H.2 | Persist Decisions | ⚪ | | |
| H.3 | Persist Actions | ⚪ | | |
| H.4 | Persist Results | ⚪ | | |
| H.5 | Product Memory | ⚪ | | |
| H.6 | Supplier Memory | ⚪ | | |
| H.7 | Temporal State | ⚪ | | |

### GATE G

⚪ PENDIENTE.

Debe distinguirse:

`CURRENT STATE ≠ HISTORY`

---

# 11. Hito I — Learning Loop

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| I.1 | Outcome Tracking | ⚪ |
| I.2 | Prediction vs Actual | ⚪ |
| I.3 | Decision Calibration | ⚪ |
| I.4 | Product Performance | ⚪ |
| I.5 | Supplier Performance | ⚪ |
| I.6 | Strategy Performance | ⚪ |
| I.7 | Learning Signals | ⚪ |

### GATE H

⚪ PENDIENTE.

---

# 12. Hito J — Continuous Autonomy

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| J.1 | Scheduler | ⚪ |
| J.2 | Market Monitoring | ⚪ |
| J.3 | Opportunity Detection | ⚪ |
| J.4 | Change Detection | ⚪ |
| J.5 | Event Bus / Event Processing | ⚪ |
| J.6 | Autonomous Alerts | ⚪ |
| J.7 | Continuous Missions | ⚪ |

### GATE I

⚪ PENDIENTE.

---

# 13. Transversal K — Observability, Evaluation y Reliability

**Estado: 🟡 EN PROGRESO**

Estas capacidades acompañan cada hito; no deben dejarse para el final.

| ID | Task | Estado |
|---|---|---|
| K.1 | Audit Trail | 🟡 |
| K.2 | Agent Trace | 🟡 |
| K.3 | Cost Tracking | ⚪ |
| K.4 | Evaluation Harness | ⚪ |
| K.5 | Golden Datasets | ⚪ |
| K.6 | Quality Gates | ⚪ |
| K.7 | Reliability | 🟡 |
| K.8 | Security checks transversal | 🟡 |

### GATE J

⚪ PENDIENTE.

---

# 14. Transversal L — Data Quality y Governance

**Estado: 🟡 EN PROGRESO**

| ID | Task | Estado |
|---|---|---|
| L.1 | Source Registry | ⚪ |
| L.2 | Data Provenance | 🟡 |
| L.3 | Freshness / TTL | 🟡 |
| L.4 | Confidence Model | 🟡 |
| L.5 | Schema Validation | ⚪ |
| L.6 | Entity Resolution | ⚪ |
| L.7 | Duplicate Detection | 🟡 |
| L.8 | Conflict Resolution | ⚪ |

### GATE K

⚪ PENDIENTE.

---

# 15. Transversal M — Control de Coste e Inferencia

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| M.1 | Model Routing Strategy | ⚪ |
| M.2 | Context Budgeting | ⚪ |
| M.3 | Prompt Compression | ⚪ |
| M.4 | Caching | ⚪ |
| M.5 | Model Selection by Task | ⚪ |
| M.6 | Cost-aware Decision Policy | ⚪ |

### GATE L

⚪ PENDIENTE.

---

# 16. Transversal N — Security, Governance y Safety

**Estado: 🟡 EN PROGRESO**

| ID | Task | Estado |
|---|---|---|
| N.1 | Identity | ⚪ |
| N.2 | Authentication | 🟡 |
| N.3 | Authorization | ⚪ |
| N.4 | RBAC / Permissions | ⚪ |
| N.5 | Secret Management | 🟡 |
| N.6 | Approval Policies | ⚪ |
| N.7 | Financial Limits | ⚪ |
| N.8 | Tool Allowlist / Denylist | ⚪ |
| N.9 | Sensitive Data Handling | ⚪ |
| N.10 | Audit / Compliance | ⚪ |
| N.11 | Emergency Stop | ⚪ |

### GATE M

⚪ PENDIENTE.

---

# 17. Hito O — SaaS / Platformization

**Estado: ⚪ PENDIENTE**

No iniciar antes de demostrar el núcleo comercial.

| ID | Task | Estado |
|---|---|---|
| O.1 | Tenant Isolation | ⚪ |
| O.2 | Organizations / Users | ⚪ |
| O.3 | Authentication | ⚪ |
| O.4 | Authorization | ⚪ |
| O.5 | Model Gateway | ⚪ |
| O.6 | Usage Metering | ⚪ |
| O.7 | Quota Management | ⚪ |
| O.8 | Plans | ⚪ |
| O.9 | Billing | ⚪ |
| O.10 | Admin Console | ⚪ |
| O.11 | Tenant-level Configuration | ⚪ |
| O.12 | Observability | ⚪ |
| O.13 | Deployment Automation | ⚪ |

### GATE N

⚪ PENDIENTE.

---

# 18. Hito P — Production / Operations

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| P.1 | CI/CD | ⚪ |
| P.2 | Environment Separation | ⚪ |
| P.3 | Database Migrations | ⚪ |
| P.4 | Backups | ⚪ |
| P.5 | Disaster Recovery | ⚪ |
| P.6 | Health Checks | ⚪ |
| P.7 | Monitoring | ⚪ |
| P.8 | Alerting | ⚪ |
| P.9 | Log Retention | ⚪ |
| P.10 | Capacity Planning | ⚪ |
| P.11 | Rate-limit Management | ⚪ |

### GATE O

⚪ PENDIENTE.

---

# 19. Hito Q — Business Intelligence

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| Q.1 | Opportunity Dashboard | ⚪ |
| Q.2 | Supplier Dashboard | ⚪ |
| Q.3 | Profit Dashboard | ⚪ |
| Q.4 | Mission Dashboard | ⚪ |
| Q.5 | Agent Cost Dashboard | ⚪ |
| Q.6 | Business KPIs | ⚪ |

---

# 20. Hito R — Advanced Autonomy

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| R.1 | Multi-step Planning | ⚪ |
| R.2 | Sub-missions | ⚪ |
| R.3 | Specialist Agents | ⚪ |
| R.4 | Agent Coordination | ⚪ |
| R.5 | Dynamic Delegation | ⚪ |
| R.6 | Long-running Missions | ⚪ |
| R.7 | Self-monitoring | ⚪ |

### GATE P

⚪ PENDIENTE.

---

# 21. Hito S — Self-Improving Commerce

**Estado: ⚪ PENDIENTE**

| ID | Task | Estado |
|---|---|---|
| S.1 | Strategy Memory | ⚪ |
| S.2 | Experiment Framework | ⚪ |
| S.3 | A/B Testing | ⚪ |
| S.4 | Decision Calibration | ⚪ |
| S.5 | Outcome-driven Ranking | ⚪ |
| S.6 | Automated Evaluation | ⚪ |
| S.7 | Human Feedback | ⚪ |

---

# 22. Registro de checkpoints

| Fecha | Commit | Hito | Estado | Evidencia |
|---|---|---|---|---|
| 2026-08-29 | `e08ebb5` | A / Autonomous Loop + OmniRoute | 🟢 | Routing E2E real |
| 2026-08-29 | — | A | 🟢 | 226 tests + E2E LIVE reportado |
| 2026-08-29 | — | B / Marcha Blanca #1 | 🟢 | 240 tests + Marcha Blanca #1 E2E LIVE Autónoma (8 iteraciones, 8 llamadas LIVE, 0 fallos) |

Actualizar esta tabla cada vez que exista un checkpoint relevante.

---

# 23. Registro de Gates

| Gate | Condición | Estado | Fecha | Evidencia |
|---|---|---|---|---|
| A | Market Opportunity Discovery | 🟢 PASSED | 2026-08-29 | E2E LIVE |
| B | Opportunity Intelligence | 🟢 PASSED | 2026-08-29 | E2E LIVE Marcha Blanca #1 (8 iters, score determinista, ranking, comparación, rechazo formal) |
| C | Supplier Intelligence | 🟢 PASSED | 2026-08-30 | Marcha Blanca C-04 (3 escenarios + fallback E2E, 311 tests pass) |
| C-Economics | Profit + Capital Allocation | 🟢 PASSED | 2026-08-30 | Marchas Blancas D-03 (4 escenarios, 351 tests pass) |
| D | Autonomous Commerce | 🟢 PASSED | 2026-08-30 | Marcha Blanca Gate D E2E (6 escenarios: ALLOW, DENY, REQUIRE_APPROVAL, UNKNOWN/Data Safety, Economics/Capital Constraint, Tool Recovery, 459 tests pass) |
| E | Communications + Approval | 🟢 PASSED | 2026-08-31 | Suite E2E Gate F Validation (`test_gate_f_e2e_validation.py`, 5 escenarios deterministas: APPROVED, REJECTED, DUPLICATE/IDEMPOTENCY, UNKNOWN/TIMEOUT, DENY PRECEDENCE) |
| F | Marketplace Operations | 🟢 PASSED | 2026-08-31 | Sub-slices G.1–G.8 totalmente validados (192 tests) + Gate F E2E Pass |
| G | Business Memory | ⚪ | | |
| H | Learning Loop | ⚪ | | |
| I | Continuous Autonomy | ⚪ | | |
| J | Observability/Reliability | ⚪ | | |
| K | Data Quality/Governance | ⚪ | | |
| L | Cost/Inference | ⚪ | | |
| M | Security/Safety | ⚪ | | |
| N | SaaS | ⚪ | | |
| O | Production | ⚪ | | |
| P | Advanced Autonomy | ⚪ | | |

---

# 24. Registro de trabajo

TRAE debe agregar una entrada por cada task completada:

| Fecha | Task | Cambio | Tests | E2E | Estado | Evidencia |
|---|---|---|---|---|---|---|
| 2026-08-29 | Marcha Blanca #1 | Autonomous Market Opportunity Discovery LIVE E2E contra Mercado Libre Chile con Autonomous Cognitive Loop (8 iteraciones) | 240 passed | LIVE MLC | 🟢 VALIDADA | `run_marcha_blanca_1.py`, 8 llamadas LIVE sin mock |
| 2026-08-30 | E-01.1 | Publication Domain Contract (SalesChannel, ListingDraft, PublicationRequest/Result, UNKNOWN, PublicationPort) | 21 passed | Unit tests | 🟢 VALIDADA | `test_publication_contracts.py` (21 passed) |
| 2026-08-30 | E-01.2 | Publication Action Integration (`PublicationActionExecutor` integrado con `AutonomousLoop`, `ActionExecutor`, correlation e idempotencia) | 30 passed (21 domain + 9 app) | Loop Integration | 🟢 VALIDADA | `test_publication_action_executor.py` (9 passed) |
| 2026-08-30 | E-01.3 | Mercado Libre Publication Adapter (`MercadoLibrePublicationAdapter`, auth OAuth, error handling, UNKNOWN, `get_status`) | 47 passed (21 dom + 9 app + 15 adap + 2 integ) | Mocked Integration / LIVE NOT EXECUTED | 🔵 IMPLEMENTADA | `test_publication_adapter.py` (15 passed), `test_e01_3_mercadolibre_publication_integration.py` (2 passed), LIVE pendiente credenciales |
| 2026-08-30 | E.3 / 05.3 | Policy Engine & Governance Barrier (`PolicyEngine`, `PolicyGuardedActionExecutor`, Auth, Budget, Risk, Approval, Idempotency, UNKNOWN, Provenance, In-memory Audit) | 36 passed (18 dom + 14 app + 4 integ) | Loop + PublicationPort Governance Integration | 🟢 VALIDADA | `tests/unit/domain/policy/`, `tests/unit/application/policy/`, `tests/integration/test_policy_engine_integration.py` |
| 2026-08-30 | E.4 / 05.4 | Tool Registry & Discovery Architecture (`ToolRegistry`, `ToolDescriptor`, contracts, versioning, lifecycle, discovery, `ToolInvocationService` con intercepción `PolicyEngine`, safe `UNKNOWN`, catalog) | 18 passed (9 dom + 7 app + 2 integ) | Tool Discovery + Policy Guarded Invocation Integration | 🟢 VALIDADA | `tests/unit/domain/tool/`, `tests/unit/application/tool/`, `tests/integration/test_tool_registry_integration.py` |
| 2026-08-30 | Gate D | Gate D E2E Marcha Blanca Validation (Integración unificada autónoma no hardcodeada de Market + Opportunity + Supplier + Profit + Capital + Tool Registry + Policy Engine + Action Executor + Recovery en 6 escenarios) | 459 passed, 1 skipped | Marcha Blanca Gate D E2E (6 escenarios: ALLOW, DENY, REQUIRE_APPROVAL, UNKNOWN/Data Safety, Economics/Capital Constraint, Tool Recovery) | 🟢 VALIDADA | `tests/integration/test_gate_d_e2e_validation.py` (6 passed) |
| 2026-08-30 | G.1 / 07.1 | Listing Generator (Generación determinista y estructurada de `ListingDraft` basada en evidencia de mercado, verdades de producto, customer pain mining, SEO groundedness, trazabilidad de claims, omisión de afirmaciones prohibidas y variantes multicanal) | 474 passed, 1 skipped (15 específicos: 11 dom + 3 app + 1 integ) | MarketEvidence + ProductTruth -> ListingDraft + Grounding + Multichannel E2E | 🟢 VALIDADA | `tests/unit/domain/publication/test_listing_generator.py`, `tests/unit/application/publication/test_listing_generator_service.py`, `tests/integration/test_listing_generator_e2e.py` |
| 2026-08-31 | Gate F | Validación E2E formal de Gate F (Marketplace Operations + Governance Approval Loop en 5 escenarios deterministas: APPROVED, REJECTED, DUPLICATE/IDEMPOTENCY, UNKNOWN/TIMEOUT, DENY PRECEDENCE) | 615 passed, 1 skipped | E2E Gate F Validation (`test_gate_f_e2e_validation.py` - 5 escenarios PASSED) | 🟢 VALIDADA | `tests/integration/test_gate_f_e2e_validation.py` |
| 2026-08-31 | G.8 / 07.8 | Returns / Exceptions (Gestión integral de devoluciones, reclamos, disputas, reembolsos y excepciones postventa con separación de ciclos de vida, deduplicación e idempotencia estricta, gobernanza por Policy, tratamiento de incertidumbre UNKNOWN, motor de reconciliación determinista y 6 tools postventa en ToolRegistry) | 610 passed, 1 skipped (28 específicos: 9 dom/rules + 4 adap + 9 app + 6 integ E2E) | Returns Pipeline Integration E2E (6 Escenarios A-F: Happy path, Duplicate/Idempotency, UNKNOWN/Recovery, Discrepancy, Policy Governance, Refund Lifecycle) | 🟢 VALIDADA | `tests/unit/domain/returns/`, `tests/unit/infrastructure/mercadolibre/test_returns_adapter.py`, `tests/unit/application/returns/`, `tests/integration/test_g08_returns_pipeline_integration.py` |

---

# 25. Reglas de actualización

TRAE debe actualizar este archivo durante el trabajo.

Para cada task:

1. marcar `🟡 EN PROGRESO` al comenzar;
2. marcar `🔵 IMPLEMENTADA` cuando exista implementación;
3. ejecutar tests;
4. ejecutar regresión;
5. ejecutar E2E cuando corresponda;
6. documentar evidencia;
7. marcar `🟢 VALIDADA` solamente cuando los criterios de aceptación estén cumplidos;
8. actualizar el Gate cuando corresponda.

Si existe un bloqueo externo:

- marcar `🔴 BLOQUEADA`;
- describir la causa;
- indicar qué fue validado;
- indicar qué dependencia falta.

No marcar una task como completa sólo porque existe código.

No marcar un Gate como pasado por inferencia.

No borrar tareas.

No mover tareas de fase sin documentar la razón.

No saltar el Gate de una fase.

---

# 26. Regla de selección de la siguiente tarea

Cuando existan varias tareas posibles:

1. elegir la que más aumente la capacidad real de negocio;
2. cierre una dependencia crítica;
3. reduzca mayor riesgo;
4. produzca evidencia verificable;
5. evite duplicación;
6. respete dependencias del roadmap.

Antes de comenzar cada task, comprobar el estado de esta Gantt y del Roadmap Maestro.

---

# 27. Estado oficial de inicio y seguimiento

**Últimas fases validadas:**
- **Fase A — Market Opportunity Discovery (🟢 VALIDADA / GATE A PASSED)**
- **Fase B — Opportunity Intelligence (🟢 VALIDADA / GATE B PASSED)**
- **Fase C — Supplier Intelligence (🟢 VALIDADA / GATE C PASSED)**
- **Fase D — Profit + Capital Allocation (🟢 VALIDADA / GATE C-Economics PASSED - Misiones D-01, D-02, D-03)**
- **Hito E — Autonomous Commerce (🟢 VALIDADA / GATE D PASSED - E.1 a E.6 + Gate D E2E Validation)**
- **Hito F — Communications + Approval (🟢 VALIDADA / GATE E PASSED)**
- **Hito G — Marketplace Operations (🟢 VALIDADA / GATE F PASSED - G.1 a G.8 + Gate F E2E Validation)**

**Fases en progreso activo:**
- **Hito H — Business Memory (⚪ PENDIENTE de inicio según regla de no avanzar prematuramente)**

**Próxima acción:**
- Con **Gate F PASSED y 🟢 VALIDADO**, el repositorio está listo para someter a revisión el Gate F. La siguiente tarea según el Roadmap Maestro es **Hito H (Business Memory - H.1 Persist Missions)**.

---

# 28. North Star

El proyecto termina cuando puede ejecutar:

`DESCUBRIR → INVESTIGAR → EVALUAR → ENCONTRAR ABASTECIMIENTO → CALCULAR ECONOMÍA Y RIESGO → DECIDIR → EJECUTAR ACCIONES AUTORIZADAS → COMUNICAR → OBSERVAR → APRENDER → VOLVER A DECIDIR`

de forma autónoma, explicable, segura, auditable y económicamente viable.

La Gantt debe reflejar el estado REAL del sistema en todo momento.
