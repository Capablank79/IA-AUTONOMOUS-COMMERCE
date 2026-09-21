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
| H | Business Memory | P1 | 🟢 VALIDADA | 🟢 GATE G |
| I | Learning Loop | P1 | 🟢 VALIDADA | 🟢 GATE H |
| J | Continuous Autonomy | P1 | ⚪ PENDIENTE | ⚪ GATE I |
| K | Observability / Evaluation / Reliability | P0 transversal | 🟡 EN PROGRESO | ⚪ GATE J |
| L | Data Quality / Governance | P0 transversal | 🟡 EN PROGRESO | ⚪ GATE K |
| M | Cost / Inference | P1 transversal | 🟢 VALIDADA | 🟢 GATE L |
| N | Security / Governance / Safety | P0 transversal | 🟡 EN PROGRESO | ⚪ GATE M |
| O | SaaS / Platformization | P2 | 🟢 VALIDADA | 🟢 GATE N |
| P | Production / Operations | P2 | 🟢 VALIDADA | 🟢 GATE O |
| Q | Business Intelligence | P2 | 🟢 VALIDADA | 🟢 GATE P |
| R | Advanced Autonomy | P3 | 🟢 VALIDADA | 🟢 GATE P |
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
| H | Business Memory | | | | | | | █ | 🟢 |
| I | Learning Loop | | | | | | | █ | ⚪ |
| J | Continuous Autonomy | | | | | | | █ | ⚪ |
| K–N | Transversales: reliability/data/cost/security | → | → | → | → | → | → | → | 🟡 |
| O | SaaS / Platformization | | | | | | | | 🟢 |
| P | Production / Operations | | | | | | | | ⚪ |
| Q | Business Intelligence | | | | | | | | 🟢 |
| R | Advanced Autonomy | | | | | | | | 🟢 |
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

**Estado: 🟢 VALIDADA (H.1 a H.7 Validadas e Integradas E2E)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| H.1 | Persist Missions | 🟢 VALIDADA | Persistencia durable de misiones (`Mission` y `MissionResult`) basada en JSON con desacoplamiento absoluto de dominio, soporte para ciclo completo (`CREATE -> PERSIST -> LOAD -> UPDATE -> PERSIST -> LOAD -> CONTINUE/RESUME`), serialización ISO/Decimal/Enum, idempotencia estricta, preservación de `correlation_id` / `idempotency_key` / `provenance` / `confidence`, resiliencia ante corrupción y exclusión estricta de PII/credenciales. | `src/infrastructure/persistence/data/json/mission_repository.py`, `tests/unit/infrastructure/persistence/data/json/test_mission_repository.py` (7 passed), `tests/integration/test_h1_mission_memory_integration.py` (1 passed) |
| H.2 | Persist Decisions | 🟢 VALIDADA | Persistencia durable de decisiones (`DecisionRecord`) basada en JSON con desacoplamiento de dominio, soporte para ciclo de vida (`CREATE -> PERSIST -> LOAD -> UPDATE -> PERSIST -> LOAD -> RECOVERY`), vinculación formal con `Mission` (`mission_id`), preservación de `PolicyEvaluation` / `confidence` / `provenance` / `correlation_id` / `idempotency_key`, idempotencia estricta, resiliencia ante datos corruptos y exclusión automática de PII/secretos. | `src/domain/decision/`, `src/infrastructure/persistence/data/json/decision_repository.py`, `src/application/decision/decision_service.py`, `tests/unit/application/decision/test_decision_memory_service.py` (6 passed), `tests/integration/test_h2_decision_memory_integration.py` (1 passed) |
| H.3 | Persist Actions | 🟢 VALIDADA | Persistencia durable de acciones (`ActionRecord`) basada en JSON con desacoplamiento de dominio, inmutabilidad, idempotencia, sanitización de datos sensibles y vinculación formal a decisiones/misiones. | `src/domain/action/`, `src/infrastructure/persistence/data/json/action_repository.py`, `src/application/action/action_service.py`, `tests/unit/application/action/test_action_memory_service.py` (6 passed), `tests/integration/test_h3_action_memory_integration.py` (1 passed) |
| H.4 | Persist Results | 🟢 VALIDADA | Persistencia durable de resultados observados de acciones (`ActionResultRecord`) basada en JSON con desacoplamiento de dominio, soporte de UNKNOWN, confianza, procedencia e idempotencia. | `src/domain/result/`, `src/infrastructure/persistence/data/json/result_repository.py`, `src/application/result/result_service.py`, `tests/unit/application/result/test_result_memory_service.py` (7 passed), `tests/integration/test_h4_result_memory_integration.py` (1 passed) |
| H.5 | Product Memory | 🟢 VALIDADA | Memoria contextual de productos/listings (`ProductMemoryRecord`) basada en JSON, conservando SKUs, precios, observaciones, procedencia y evidencias. | `src/domain/product_memory/`, `src/infrastructure/persistence/data/json/product_memory_repository.py`, `src/application/product_memory/product_memory_service.py`, `tests/unit/application/product_memory/test_product_memory_service.py` (6 passed), `tests/integration/test_h5_product_memory_integration.py` (1 passed) |
| H.6 | Supplier Memory | 🟢 VALIDADA | Memoria contextual de proveedores/cotizaciones (`SupplierMemoryRecord`) basada en JSON, conservando identidades de proveedor, condiciones comerciales, nivel de riesgo, confianza y procedencia. | `src/domain/supplier_memory/`, `src/infrastructure/persistence/data/json/supplier_memory_repository.py`, `src/application/supplier_memory/supplier_memory_service.py`, `tests/unit/application/supplier_memory/test_supplier_memory_service.py` (6 passed), `tests/integration/test_h6_supplier_memory_integration.py` (1 passed) |
| H.7 | Temporal State | 🟢 VALIDADA | Captura y reconstrucción histórica temporal de snapshots (`TemporalSnapshot`) permitiendo consultar el estado exacto de cualquier entidad en $T_0, T_1, T_2$ con ordenamiento cronológico e inmutabilidad. | `src/domain/temporal_state/`, `src/infrastructure/persistence/data/json/temporal_state_repository.py`, `src/application/temporal_state/temporal_state_service.py`, `tests/unit/application/temporal_state/test_temporal_state_service.py` (6 passed), `tests/integration/test_h7_temporal_state_integration.py` (1 passed) |

### GATE G

🟢 PASSED.

Fecha de Validación: 2026-08-31
Tests: 650 unitarios y de integración pasando (100% pass, 1 skipped)
E2E: Suite E2E de Business Memory en `tests/integration/test_hito_h_business_memory_e2e.py` completada.

Debe distinguirse:

`CURRENT STATE ≠ HISTORY`

---

# 11. Hito I — Learning Loop

**Estado: 🟢 VALIDADA (I.1 a I.7 Validadas e Integradas E2E con Gate H PASS)**

| ID | Task | Estado |
|---|---|---|
| I.1 | Outcome Tracking | 🟢 VALIDADA | Captura y persistencia de outcomes observados post-acción con trazabilidad causal inmutable | `src/domain/outcome/`, `src/infrastructure/persistence/data/json/outcome_repository.py`, `src/application/outcome/outcome_service.py`, `tests/unit/application/outcome/test_outcome_tracking.py`, `tests/integration/test_i1_outcome_tracking_integration.py` |
| I.2 | Prediction vs Actual | 🟢 VALIDADA | Registro de predicciones y comparación determinista contra outcomes reales con trazabilidad causal, temporalidad e idempotencia | `src/domain/prediction/`, `src/infrastructure/persistence/data/json/prediction_repository.py`, `src/application/prediction/prediction_comparison_service.py`, `tests/unit/application/prediction/test_prediction_comparison.py`, `tests/integration/test_i2_prediction_vs_actual_integration.py` |
| I.3 | Decision Calibration | 🟢 VALIDADA | Transformación determinista del historial verificable de predicciones comparadas con outcomes reales en métricas/estado de calibración de decisiones (Brier score, error de calibración, bins de confianza, manejo seguro de UNKNOWN y suficiencia de datos) | `src/domain/calibration/`, `src/infrastructure/persistence/data/json/calibration_repository.py`, `src/application/calibration/decision_calibration_service.py`, `tests/unit/application/calibration/test_decision_calibration.py`, `tests/integration/test_i3_decision_calibration_integration.py` |
| I.4 | Product Performance | 🟢 VALIDADA | Medición determinista del desempeño comercial observable de productos usando memoria contextual existente (H.5) y outcomes reales (I.1), incorporando contexto de Prediction vs Actual (I.2) y Decision Calibration (I.3) sin duplicar ni recalibrar | `src/domain/product_performance/`, `src/application/product_performance/`, `src/infrastructure/persistence/data/json/product_performance_repository.py`, `tests/unit/application/product_performance/test_product_performance.py`, `tests/integration/test_i4_product_performance_integration.py` |
| I.5 | Supplier Performance | 🟢 VALIDADA | Medición determinista del desempeño observable y comercial de proveedores a partir de evidencia registrada en H.6 Supplier Memory y outcomes observados de I.1, reutilizando contratos y preservando la trazabilidad causal sin inventar métricas ni duplicar entidades | `src/domain/supplier_performance/`, `src/application/supplier_performance/`, `src/infrastructure/persistence/data/json/supplier_performance_repository.py`, `tests/unit/application/supplier_performance/test_supplier_performance.py`, `tests/integration/test_i5_supplier_performance_integration.py` |
| I.6 | Strategy Performance | 🟢 VALIDADA | Medición determinista del desempeño observable de estrategias comerciales a partir de decisiones, acciones, resultados y outcomes reales (I.1-I.5), preservando la trazabilidad causal completa y sanitizando credenciales | `src/domain/strategy_performance/`, `src/application/strategy_performance/`, `src/infrastructure/persistence/data/json/strategy_performance_repository.py`, `tests/unit/application/strategy_performance/test_strategy_performance.py`, `tests/integration/test_i6_strategy_performance_integration.py` |
| I.7 | Learning Signals | 🟢 VALIDADA | Transformación determinista de evidencia histórica validada (I.1-I.6) en señales estructuradas e inmutables para aprendizaje posterior, con separación estricta entre Signal y Recommendation, clasificación explícita de evidencia (OBSERVED, DERIVED, INFERRED), manejo seguro de UNKNOWN e INSUFFICIENT_DATA, deduplicación e idempotencia estricta en replay, y persistencia JSON durable. | `src/domain/learning_signals/`, `src/application/learning_signals/`, `src/infrastructure/persistence/data/json/learning_signal_repository.py`, `tests/unit/application/learning_signals/test_learning_signals.py`, `tests/integration/test_i7_learning_signals_integration.py` |

### GATE H

🟢 PASSED.

Fecha de Validación: 2026-08-31
Tests: 708 unitarios, de integración y E2E pasando (100% pass, 1 skipped, 0 failures)
E2E: Suite formal de validación Gate H en `tests/integration/test_gate_h_e2e_validation.py` completada demostrando todos los criterios A al P:
- A — Complete causal chain (`MISSION -> DECISION -> POLICY -> ACTION -> RESULT -> OUTCOME -> PREDICTION/COMPARISON -> CALIBRATION -> PRODUCT/SUPPLIER/STRATEGY PERFORMANCE -> LEARNING SIGNAL`)
- B — Durable memory
- C — Restart/reload
- D — UNKNOWN preservation
- E — Policy boundaries
- F — Approval boundaries
- G — Prediction vs actual
- H — Calibration
- I — Product performance
- J — Supplier performance
- K — Strategy performance
- L — Learning signals
- M — Signal does not modify policy
- N — Idempotent replay
- O — Sensitive-data exclusion
- P — No false success

---

# 12. Hito J — Continuous Autonomy

**Estado: 🟢 VALIDADA (Sub-slices J.1 a J.7 Validados e Integrados con E2E Gate I Pass)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| J.1 | Scheduler | 🟢 VALIDADA | Motor de planificación determinista, persistente e idempotente con triggers cron/interval/exact, serialización ISO/UTC, gestión de missed executions, thread-safety y cero dependencias de ejecución de mercado directa. | `src/domain/scheduler/`, `src/infrastructure/persistence/data/json/schedule_repository.py`, `src/application/scheduler/scheduler_service.py`, `tests/unit/test_scheduler_service.py` (22 passed), `tests/integration/test_j1_scheduler_integration.py` (1 passed) |
| J.2 | Market Monitoring | 🟢 VALIDADA | Monitorización continua y desacoplada del mercado consumiendo snapshots canónicos para producir observaciones inmutables (`MarketObservation`), con trazabilidad completa de procedencia, exclusión de secretos, preservación estricta de `UNKNOWN` ante fallos de fuente y persistencia JSON atómica. | `src/domain/market_monitoring/`, `src/infrastructure/persistence/data/json/market_observation_repository.py`, `src/application/market_monitoring/service.py`, `tests/unit/domain/market_monitoring/` (14 passed), `tests/unit/infrastructure/persistence/data/json/test_market_observation_repository.py` (6 passed), `tests/integration/test_j2_market_monitoring_integration.py` (1 passed) |
| J.3 | Opportunity Detection | 🟢 VALIDADA | Detección determinista, estructurada y explicable de oportunidades comerciales (`OpportunityRecord`) consumiendo `MarketObservation` de J.2, con separación ontológica rigurosa entre métricas observadas y derivadas, algoritmo de scoring sin ML/LLMs, preservación de `UNKNOWN` e `INSUFFICIENT_DATA`, deduplicación e idempotencia estricta por SHA-256 de observaciones fuente, sanitización recursiva de secretos y persistencia JSON atómica (`JsonOpportunityRepository`). Sin emitir decisiones, ejecutar acciones ni consultar marketplaces directamente. | `src/domain/opportunity_detection/`, `src/application/opportunity_detection/`, `src/infrastructure/persistence/data/json/opportunity_repository.py`, `tests/unit/domain/opportunity_detection/test_opportunity_detection_unit.py` (16 passed), `tests/unit/infrastructure/persistence/data/json/test_opportunity_repository_unit.py` (5 passed), `tests/integration/test_j3_opportunity_detection_integration.py` (10 passed) |
| J.4 | Change Detection | 🟢 VALIDADA | Detección determinista, inmutable y trazable de cambios temporales entre observaciones de mercado (`MarketObservation`, J.2) y entre oportunidades comerciales (`OpportunityRecord`, J.3), con validación temporal estricta ($T_0 < T_1$), separación ontológica entre hechos observados y deltas derivados, manejo seguro de `UNKNOWN`, preservación de `provenance` y referencias causales de evidencia, deduplicación e idempotencia por replay, sanitización recursiva de secretos y persistencia JSON atómica durable (`JsonChangeRecordRepository`). Sin emitir decisiones, ejecutar acciones, generar alertas ni implementar Event Bus. | `src/domain/change_detection/`, `src/application/change_detection/`, `src/infrastructure/persistence/data/json/change_repository.py`, `tests/unit/domain/change_detection/test_j4_change_detection_unit.py` (29 passed), `tests/integration/test_j4_change_detection_integration.py` (10 passed) |
| J.5 | Event Bus / Event Processing | 🟢 VALIDADA | Infraestructura interna de eventos desacoplada, durable y determinista (`EventRecord`, `EventType`), con persistencia JSON atómica (`JsonEventStore`), bus in-process con semántica at-least-once y despacho desacoplado (`EventBusService`), entrega idempotente por `(event_id, handler_id)`, aislamiento de fallos entre manejadores, replay determinista y seguro ante reinicios, trazabilidad causal completa (`correlation_id`, `causation_id`, `provenance`), preservación estricta de `UNKNOWN` y sanitización recursiva de secretos. Integración con `ChangeDetectedEvent` (J.4), `MarketObservationCreated` (J.2) y `OpportunityDetected` (J.3). Sin emitir decisiones, ejecutar acciones, generar alertas (J.6) ni iniciar misiones continuas (J.7). | `src/domain/events/`, `src/infrastructure/persistence/data/json/event_store.py`, `src/application/events/`, `tests/unit/domain/events/test_j5_event_bus_unit.py` (29 passed), `tests/integration/test_j5_event_bus_integration.py` (3 passed), `tests/e2e/test_j5_event_bus_e2e.py` (9 passed) |
| J.6 | Autonomous Alerts | 🟢 VALIDADA | Generación determinista, estructurada y desacoplada de alertas autónomas (`AlertRecord`, `AlertType`, `AlertSeverity`, `AlertDeliveryResult`) consumiendo eventos de J.5 (`CHANGE_DETECTED`, `OPPORTUNITY_DETECTED`, `MARKET_OBSERVATION_CREATED`) mediante `AutonomousAlertEventHandler` y `AlertService`, motor de reglas explícitas sin ML/LLMs (`DeterministicAlertRulesEngine`), preservación estricta de incertidumbre `UNKNOWN`, deduplicación e idempotencia por replay (`idempotency_key`), control de frecuencia/cooldown determinista, aislamiento de fallos en canales de entrega (`AlertDeliveryPort`), sanitización recursiva de secretos y persistencia JSON atómica durable (`JsonAlertRepository`). Sin emitir Decisiones, ejecutar Acciones, invocar marketplaces ni iniciar Misiones Continuas (J.7). | `src/domain/alerts/`, `src/application/alerts/`, `src/infrastructure/alerts/`, `src/infrastructure/persistence/data/json/alert_repository.py`, `tests/unit/domain/alerts/test_j6_autonomous_alerts_unit.py` (18 passed), `tests/integration/test_j6_autonomous_alerts_integration.py` (1 passed), `tests/e2e/test_j6_autonomous_alerts_e2e.py` (10 passed) |
| J.7 | Continuous Missions | 🟢 VALIDADA | Coordinación periódica, persistente, reiniciable, idempotente y gobernada de misiones continuas (`ContinuousMission`, `ContinuousMissionCycle`, `ContinuousMissionStatus`), integrando la cadena completa J.1 Scheduler (`MissionTriggerPort`), J.2 Market Monitoring, J.3 Opportunity Detection, J.4 Change Detection, J.5 Event Bus, J.6 Autonomous Alerts, orquestador de misiones existente (`BasicMissionOrchestrator`), Business Memory (Hito H) y Learning Loop (Hito I). Soporte de parada automática por límites de ciclos y fallos consecutivos, transiciones deterministas, manejo seguro de UNKNOWN, protección de concurrencia multihilo (`threading.RLock`), persistencia atómica JSON durable (`JsonContinuousMissionRepository`) y sanitización recursiva de secretos. Sin crear Scheduler paralelo, sin Event Bus paralelo, sin saltar PolicyEngine ni auto-aprobar acciones irreversibles. | `src/domain/continuous_mission/`, `src/application/continuous_mission/`, `src/infrastructure/persistence/data/json/continuous_mission_repository.py`, `tests/unit/domain/continuous_mission/test_continuous_mission_unit.py` (24 passed), `tests/integration/test_j7_continuous_missions_integration.py` (2 passed) |

### GATE I

🟢 PASSED.

Fecha de Validación: 2026-09-01
Tests: 934 unitarios, de integración y E2E pasando (100% pass, 1 skipped, 0 failures)
E2E: Suite formal de validación Gate I en `tests/integration/test_gate_i_continuous_autonomy_validation.py` completada demostrando todos los criterios A al J:
- A — Happy Path de dos ciclos continuos (`SCHEDULE -> CONTINUOUS MISSION -> MARKET MONITORING -> OPPORTUNITY DETECTION -> CHANGE DETECTION -> EVENT BUS -> AUTONOMOUS ALERTS -> MISSION/AUTONOMOUS LOOP -> DECISION -> POLICY -> ACTION -> RESULT -> BUSINESS MEMORY -> LEARNING SIGNALS -> NEXT CYCLE`)
- B — Restart / Recovery tras destrucción de memoria en proceso y recarga desde JSON stores
- C — Duplicate / Replay idempotente (mismo schedule occurrence / trigger)
- D — UNKNOWN preservation ante fallos o incertidumbre de fuente de mercado
- E — Policy governance / DENY enforcement (sin bypass ni auto-aprobación)
- F — Pause / Resume / Stop deterministic lifecycle control
- G — Failure isolation en handlers no críticos (Alert Delivery failure sin abortar ciclo de misión continua)
- H — Max Cycles termination determinista
- I — Security & recursive sensitive-data redaction
- J — Full causal trace reconstruction e integración con Hito H Business Memory y Hito I Learning Signals

---

# 13. Transversal K — Observability, Evaluation y Reliability

**Estado: 🟢 VALIDADA (Capacidades K.1 a K.8 Validadas e Integradas con E2E Gate J Pass)**

Estas capacidades acompañan cada hito; no deben dejarse para el final.

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| K.1 | Audit Trail | 🟢 VALIDADA | Registro histórico de auditoría inmutable, persistente, append-only, determinista y seguro, capaz de reconstruir cronológica y causalmente misiones completas (Mission -> Observation -> Evidence -> Decision -> Policy -> Action -> Result) con taxonomía canónica de actores, desempate determinista, deduplicación e idempotencia por replay, durabilidad post-reinicio, preservación estricta de UNKNOWN y sanitización recursiva de secretos. | `src/domain/audit/`, `src/application/audit/`, `src/infrastructure/persistence/data/json/audit_repository.py`, `tests/unit/test_k1_audit_trail_unit.py` (29 passed), `tests/integration/test_k1_audit_trail_integration.py` (7 passed) |
| K.2 | Agent Trace | 🟢 VALIDADA | Registro estructurado, inmutable y seguro de la ejecución observable de agentes y servicios autónomos (BasicMissionOrchestrator, AutonomousLoop, ContinuousMissionService) mediante pasos operacionales deterministas (START, OBSERVE, SERVICE_CALL, POLICY_EVALUATION, TOOL_CALL, PERSIST, EMIT_EVENT, COMPLETE, FAILURE), con referencias de entrada/salida, correlación causal/negocio, deduplicación e idempotencia por replay, aislamiento de fallos, durabilidad JSON con fsync, preservación de incertidumbre UNKNOWN, exclusión estricta de Chain-of-Thought / prompts privados y sanitización recursiva de credenciales. | `src/domain/agent_trace/`, `src/application/agent_trace/`, `src/infrastructure/persistence/data/json/agent_trace_repository.py`, `tests/unit/test_k2_agent_trace_unit.py` (17 passed), `tests/integration/test_k2_agent_trace_integration.py` (5 passed) |
| K.3 | Cost Tracking | 🟢 VALIDADA | Medición estructurada, trazable, inmutable y persistente de costos operacionales asociados a ejecuciones, agentes, herramientas, servicios externos y llamadas de inferencia. Responde qué costo ocurrió, quién lo causó, para qué misión/ejecución/ciclo, cuándo, cuánto y con qué fuente tarifaria en aritmética exacta `Decimal`. Semántica estricta `UNKNOWN != 0.00`, segregación multi-moneda en `CostSummary`, catálogo de tarifas desacoplado y versionado (`PricingCatalogPort`), persistencia JSON durable y atómica con checksums SHA-256, deduplicación e idempotencia ante replays, aislamiento de fallos (`isolate_failures`), sanitización recursiva de secretos y enlace no intrusivo con K.2 Agent Trace y K.1 Audit Trail. Cero optimización/caching/routing de Hito M. | `src/domain/cost/`, `src/application/cost/`, `src/infrastructure/persistence/data/json/cost_repository.py`, `tests/unit/test_k3_cost_tracking_unit.py` (29 passed), `tests/integration/test_k3_cost_tracking_integration.py` (5 passed) |
| K.4 | Evaluation Harness | 🟢 VALIDADA | Infraestructura determinista, inmutable, auditable y reproducible para evaluar comportamientos, salidas y propiedades críticas del sistema mediante casos de evaluación declarativos (`EvaluationCase`), métricas estructuradas (`EvaluationMetric`), evaluadores deterministas desacoplados (`ExactMatch`, `Structural`, `NumericTolerance`, `Status`, `Policy`, `Safety`, `Trace`, `Idempotency`, `EndToEnd`) y persistencia durable JSON con atomicity (`.tmp` + `fsync` + `os.replace`). Preservación de semánticas `UNKNOWN` y `ERROR` con aislamiento de fallos (`isolate_failures`), ejecución individual y en batch (`BatchEvaluationSummary`), deduplicación e idempotencia por replay, sanitización recursiva de secretos y vinculación no intrusiva con K.1 Audit Trail, K.2 Agent Trace y K.3 Cost Tracking. Cero dependencias LLM-as-a-judge, cero Golden Datasets (K.5) y cero Quality Gates (K.6). | `src/domain/evaluation/`, `src/application/evaluation/`, `src/infrastructure/persistence/data/json/evaluation_repository.py`, `tests/unit/test_k4_evaluation_harness_unit.py` (22 passed), `tests/integration/test_k4_evaluation_harness_integration.py` (7 passed) |
| K.5 | Golden Datasets | 🟢 VALIDADA | Conjuntos canónicos, versionados, inmutables y formalmente curados de casos de evaluación (`GoldenDataset`, `GoldenDatasetManifest`, `DatasetCaseReference`) reutilizando `EvaluationCase` de K.4 por referencia y checksum. Manifiesto determinista con hash SHA-256 canónico, identificación estructurada del curador (`GoldenDatasetCurator`), procedencia verificable (`GoldenDatasetProvenance`), ciclo de vida (`DRAFT`, `VALIDATED`, `DEPRECATED`), detección de conflictos por versión, persistencia atómica durable en JSON (`JsonGoldenDatasetRepository`) con fsync y tolerancia ante corrupción, suite canónica de datasets baseline representativos (`baseline_discovery_golden_v1`, `baseline_policy_safety_golden_v1`, `baseline_pricing_execution_golden_v1`) e integración fluida por inyección con el ejecutor por lotes de K.4 (`EvaluationHarnessService.evaluate_golden_dataset`). Cero ejecución de Quality Gates (K.6), cero bloqueo de release y cero autogeneración no curada. | `src/domain/golden_dataset/`, `src/application/golden_dataset/`, `src/infrastructure/persistence/data/json/golden_dataset_repository.py`, `tests/unit/test_k5_golden_datasets_unit.py` (13 passed), `tests/integration/test_k5_golden_datasets_integration.py` (6 passed) |
| K.6 | Quality Gates | 🟢 VALIDADA | Mecanismo formal, determinista, inmutable, versionado y auditable para decidir si un conjunto de resultados de evaluación (producidos por K.4 / K.5) satisface las condiciones contractuales requeridas para autorizar un despliegue o promoción (`QualityGateDefinition`, `QualityGateDecision`). Aristas cubiertas: verificación integral de checksums SHA-256 en definiciones y decisiones (`recompute -> compare` con detección explícita de corrupción física sin autorreparación silenciosa), persistencia inmutable crash-safe JSON (`.tmp` + `fsync` + `os.replace`), locking thread-safe (`threading.Lock`) sobre la sección crítica completa (`check -> write -> index`), recuperación de índices (`recover_index`), idempotencia fuerte mediante fingerprinting canónico de inputs materiales (`_compute_input_fingerprint`), detección de colisiones (`GateDecisionConflictError`, `GateVersionConflictError`), vinculación e integridad estricta con datasets K.5 (`target_dataset_manifest_checksum`), contrato de decisión unívoco para despliegue (`deployment_allowed` exclusivamente ante `PASS`, bloqueando ante `FAIL`, `UNKNOWN`, `ERROR` y regresión de casos críticos), preservación de semánticas no-pass (`UNKNOWN != FAIL`), deep freeze recursivo (`MappingProxyType` y `tuple`), validación estricta de rutas contra path traversal (`..`, `/`, `\`), ordenamiento SemVer canónico (`1.10.0 > 1.9.0`), y emisión real desacoplada de registro de auditoría K.1 (`AuditRecord`) ante nuevas decisiones evitando duplicados en replays. Cero ejecución de lógica de negocio, cero evaluación LLM, cero modificación de PolicyEngine y cero CI/CD. | `src/domain/quality_gate/`, `src/application/quality_gate/`, `src/infrastructure/persistence/data/json/quality_gate_repository.py`, `tests/unit/test_k6_quality_gates_unit.py` (24 passed), `tests/integration/test_k6_quality_gates_integration.py` (4 passed) |
| K.7 | Reliability | 🟢 VALIDADA | Motor de resiliencia y confiabilidad desacoplado, formal, determinista y auditable (`ReliabilityEngine`, `ReliabilityResult`, `RetryPolicy`, `CircuitBreakerPort`, `IdempotencyStorePort`). Taxonomía canónica explícita de 11 categorías de fallo (`FailureCategory`) y 4 grados de recuperabilidad (`FailureRecoverability`), preservando semántica estricta de incertidumbre `UNKNOWN != SUCCESS` y `UNKNOWN != FAILURE confirmado`. Seguridad estricta en mutaciones externas (`is_side_effect=True`): prohibición de reintento ciego ante `TIMEOUT` o `UNKNOWN` y forzado de reconciliación/verificación previa contra estado real para evitar duplicados. Control de idempotencia fuerte (`JsonIdempotencyStore`, `InMemoryIdempotencyStore`) con hashing SHA-256 de payload, almacenamiento durable con atomic writes (`.tmp` + `fsync` + `os.replace`), y detección de conflicto (`CONFLICT`) si se reutiliza una clave con payload distinto sin sobreescritura silenciosa. Circuit Breaker (`InMemoryCircuitBreaker`) con transiciones de estado `CLOSED`, `OPEN`, `HALF_OPEN` y bypass rápido ante dependencias degradadas. Simulación temporal determinista sin `sleep` real (`VirtualClock` / `ClockPort`), prevención de tormentas de reintento (`Retry-After` y backoff exponencial acotado), aislamiento de fallos no críticos (errores en Audit/Trace no abortan el resultado comercial), respeto inviolable a la gobernanza de `PolicyEngine` (sin bypass en reintentos), y emisión no intrusiva de trazas operacionales (K.2 `AgentTraceRecord`) y auditoría (K.1 `AuditRecord`). Cero herramientas de caos externas, cero dependencia de brokers externos y cero intrusión en K.8. | `src/domain/reliability/`, `src/application/reliability/`, `src/infrastructure/reliability/`, `tests/unit/test_k7_reliability_unit.py` (10 passed), `tests/integration/test_k7_reliability_integration.py` (9 passed), `tests/unit/test_k7_reliability_e2e.py` (1 passed) |
| K.8 | Security checks transversal | 🟢 VALIDADA | Validación de seguridad transversal, determinista, inmutable y desacoplada (`SecurityCheckService`, `SecurityCheckResult`, `SecurityCheckEvaluation`) sobre todas las superficies del sistema (API inputs, marketplace adapters, token storage, filesystem paths, event payloads, agent/tool boundaries). Principios y garantías: autenticación y autorización previas a side-effects, subordinación estricta a PolicyEngine (sin bypass en retries ni continuous missions), sanitización recursiva profunda de secretos (API keys, tokens OAuth, contraseñas, PAN, CVV, auth headers), exclusión inviolable de Chain-of-Thought / prompts privados / scratchpads en trazas y eventos, validación estricta contra Path Traversal (`..`, `/`, `\`, prefijos absolutos), persistencia con integridad por checksum SHA-256 y detección de corrupción, protección contra manipulación en replays de idempotencia (mismo key + payload alterado = CONFLICT), aislamiento seguro en Event Bus, semánticas explícitas de fallo no reintentable (`UNAUTHORIZED`, `INVALID_INPUT`, `INTEGRITY_ERROR`, `CONFLICT`) y registro auditable de eventos de seguridad en K.1 Audit Trail sin filtrar secretos. | `src/domain/security/`, `src/application/security/`, `tests/unit/test_k8_security_checks_unit.py` (23 passed), `tests/integration/test_k8_security_checks_integration.py` (8 passed) |

### GATE J

🟢 PASSED.

Fecha de Validación: 2026-09-02
Tests: 1158 unitarios, de integración y E2E pasando (100% pass, 1 skipped, 0 failures; 224 específicos de Hito K y Gate J)
E2E: Suite formal de validación Gate J en `tests/integration/test_gate_j_hito_k_e2e.py` completada demostrando todos los criterios clave:
- A — Cross-K Happy Path, Reliability Replay & Restart Durability (`Security -> Trace -> Reliability -> Cost -> Golden Dataset -> Evaluation Harness -> Quality Gate -> Audit Trail -> Replay/Restart durability`)
- B — Quality Gate Critical Regression Blocking (`deployment_allowed = False` y bloqueo de release ante fallos en métricas requeridas)
- C — UNKNOWN Preservation & Cost Accounting (`UNKNOWN != 0.00` y `UNKNOWN != FAIL`, sin excepciones no controladas)
- D — Checksum Tampering & Altered Replay Detection (Detección explícita de corrupción física SHA-256 y `CONFLICT` ante replay con payload inconsistente)
- E — Concurrency Protection & Exactly-Once Side Effects (10 hilos concurrentes compitiendo con la misma clave ejecutan exactamente una mutación física)

---

# 14. Transversal L — Data Quality y Governance

**Estado: 🟢 VALIDADA (Capacidades L.1 a L.8 Validadas e Integradas con E2E Gate K Pass)**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| L.1 | Source Registry | 🟢 VALIDADA | Catálogo canónico e inmutable de fuentes de datos (`RegisteredSource`, `SourceType`, `SourceStatus`) con asignación determinista de identidad canónica (`canonical_identifier`), sanitización estricta de credenciales en URLs y metadata, verificación de integridad física por checksum SHA-256 (`recompute -> compare` con detección de corrupción sin autorreparación silenciosa), persistencia JSON crash-safe (`.tmp` + `fsync` + `os.replace`), locking thread-safe (`threading.RLock`), control de idempotencia estricta, detección de colisiones de versión/identidad (`SourceVersionConflictError`, `SourceCanonicalConflictError`), prevención de path traversal en identificadores de filesystem y emisión desacoplada de eventos de auditoría K.1 (`SOURCE_REGISTERED`, `SOURCE_CONFLICT`). Cero lógica de Freshness (L.3), Confidence (L.4), Provenance (L.2), Schema Validation (L.5), Entity Resolution (L.6), Duplicate Detection (L.7) o Conflict Resolution (L.8). | `src/domain/source_registry/`, `src/application/source_registry/`, `src/infrastructure/persistence/data/json/source_registry_repository.py`, `tests/unit/test_l1_source_registry_unit.py` (24 passed), `tests/integration/test_l1_source_registry_integration.py` (9 passed) |
| L.2 | Data Provenance | 🟢 VALIDADA | Modelo inmutable y determinista de linaje de datos (`ProvenanceRecord`, `SubjectType`, `SourceLineageTrace`) con trazabilidad desde hechos atómicos y derivados (DAG libre de ciclos) hasta las fuentes raíz registradas en L.1 Source Registry. Soporte para procedencia a nivel de entidad y de campo (`field_path`), enlace con evidencias (`evidence_id`, `source_record_id`), cálculo canónico de checksum SHA-256 (`recompute -> compare` con detección de corrupción física), persistencia JSON crash-safe (`.tmp` + `fsync` + `os.replace`), locking thread-safe (`threading.RLock`), control estricto de idempotencia y rechazo explícito de conflictos ante mutaciones semánticas, sanitización recursiva de secretos y protección contra path traversal. Cero Freshness (L.3), cero Confidence (L.4), cero Schema Validation (L.5), cero Entity Resolution (L.6), cero Duplicate Detection (L.7) y cero Conflict Resolution (L.8). | `src/domain/data_provenance/`, `src/application/data_provenance/`, `src/infrastructure/persistence/data/json/data_provenance_repository.py`, `tests/unit/test_l2_data_provenance_unit.py` (20 passed), `tests/integration/test_l2_data_provenance_integration.py` (8 passed) |
| L.3 | Freshness / TTL | 🟢 VALIDADA | Modelo inmutable y determinista de frescura y políticas de caducidad temporal TTL (`FreshnessPolicy`, `FreshnessAssessment`, `FreshnessStatus`) con integración a L.1 Source Registry y L.2 Data Provenance. Evaluación explícita de frescura (`FRESH`, `STALE`, `EXPIRED`, `UNKNOWN`, `ERROR`), boundaries exactos (`age < ttl -> FRESH`, `age >= ttl -> STALE`), timestamps timezone-aware UTC, tolerancia a timestamps futuros, tratamiento seguro de timestamps ausentes (`UNKNOWN != FRESH`), regla de propagación y restricción sobre datos derivados (*most degraded parent rule* en DAGs de procedencia), cascada determinista de resolución de políticas (campo > sujeto > fuente > tipo de fuente > default), persistencia JSON atómica crash-safe (`.tmp` + `fsync` + `os.replace`), verificación SHA-256 contra corrupción física y control de idempotencia/conflictos. Cero cálculo de confianza (L.4), cero validación de esquemas (L.5), cero resolución de entidades (L.6), cero detección de duplicados (L.7) y cero resolución de conflictos (L.8). | `src/domain/freshness/`, `src/application/freshness/`, `src/infrastructure/persistence/data/json/freshness_repository.py`, `tests/unit/test_l3_freshness_ttl_unit.py` (16 passed), `tests/integration/test_l3_freshness_ttl_integration.py` (8 passed) |
| L.4 | Confidence Model | 🟢 VALIDADA | Modelo explícito, inmutable y determinista de confianza (`ConfidencePolicy`, `ConfidenceAssessment`, `ConfidenceFactor`, `ConfidenceLevel`) con scoring `Decimal` en rango `[0, 1]`, preservación diferenciada de `UNKNOWN`, `LOW` y `ERROR`, factores configurados por policy para fuente L.1, procedencia L.2, frescura L.3 y evidencia, agregación derivada explícita (`MIN`, `WEIGHTED`, `REQUIRED_ALL`), policies versionadas, checksums SHA-256 canónicos, idempotencia/conflictos, persistencia JSON atómica y sanitización K.8. Consumidores consultan assessments sin decisiones comerciales automáticas. Cero Schema Validation (L.5), Entity Resolution (L.6), Duplicate Detection (L.7) o Conflict Resolution (L.8). | `src/domain/confidence/`, `src/application/confidence/`, `src/infrastructure/persistence/data/json/confidence_repository.py`, `tests/unit/test_l4_confidence_model_unit.py` (24 passed), `tests/integration/test_l4_confidence_model_integration.py` (8 passed); regresión completa: 1275 passed, 1 skipped |
| L.5 | Schema Validation | 🟢 VALIDADA | Validación determinista de estructura, tipos y constraints de datos (`SchemaDefinition`, `SchemaValidationResult`, `ValidationStatus`: `PASS`/`FAIL`/`UNKNOWN`/`ERROR`, `FieldType`, `AdditionalFieldsPolicy`). Esquemas inmutables y versionados por SemVer con checksum SHA-256 canónico (`recompute -> compare`). Semántica estricta de `required`/`optional`/`nullable` (missing != null). Tipos estrictos sin coerción silenciosa ("10" != Decimal 10; booleans no son integers). Restricciones numéricas comerciales en `Decimal` (precio/stock negativo -> FAIL). Errores estructurados anidados con `field_path` explícito (`supplier.address.country`). Políticas explícitas de campos no declarados `ALLOW`/`IGNORE`/`FORBID`. Esquema desconocido -> `UNKNOWN` (nunca PASS). Enlace con `provenance_id` (L.2) y ortogonalidad con L.3 (frescura) y L.4 (confianza). Persistencia JSON atómica crash-safe (`.tmp` + `fsync` + `os.replace`) con detección de corrupción y conflictos de versión. Sanitización de secretos en errores/metadata (K.8). Cero Entity Resolution (L.6), Duplicate Detection (L.7) y Conflict Resolution (L.8). | `src/domain/schema_validation/`, `src/application/schema_validation/`, `src/infrastructure/persistence/data/json/schema_repository.py`, `tests/unit/test_l5_schema_validation_unit.py` (18 passed), `tests/integration/test_l5_schema_validation_integration.py` (11 passed incluido E2E L.1->L.2->L.5->L.3->L.4); regresión completa: 1304 passed, 1 skipped |
| L.6 | Entity Resolution | 🟢 VALIDADA | Resolución determinista de identidad de entidades (`CanonicalEntity`, `EntityMatchResult`, `EntityResolutionPolicy`, `MatchStatus`: `MATCH`/`NO_MATCH`/`POSSIBLE_MATCH`/`UNKNOWN`/`ERROR`). Fusión lógica inmutable de referencias mediante `canonical_entity_id`, comparación determinista por reglas de identidad exactas y difusas sin LLM/vector DB, persistencia JSON atómica crash-safe con detección de corrupción por SHA-256. | `src/domain/entity_resolution/`, `src/application/entity_resolution/`, `src/infrastructure/persistence/data/json/entity_resolution_repository.py`, `tests/unit/test_l6_entity_resolution_unit.py` (31 passed), `tests/integration/test_l6_entity_resolution_integration.py` (11 passed) |
| L.7 | Duplicate Detection | 🟢 VALIDADA | Detección determinista e inmutable de duplicados y hechos lógicos repetidos (`DuplicateCandidate`, `DuplicateDetectionResult`, `DuplicateDetectionPolicy`, `DuplicateGroup`, `DuplicateStatus`: `DUPLICATE`/`EXACT_DUPLICATE`/`REPLAY_DUPLICATE`/`POSSIBLE_DUPLICATE`/`NOT_DUPLICATE`/`UNKNOWN`/`ERROR`). Principio estricto `SAME ENTITY != DUPLICATE`, fingerprint semántico determinista SHA-256 (ordenamiento canónico, normalización Unicode NFKC, exclusión de secretos y ruido técnico), soporte de ventanas temporales (observaciones legítimas en momentos distintos) y comportamiento sensible a fuentes (evidencias independientes cross-source no colapsadas), idempotencia estricta por replay, persistencia JSON atómica crash-safe (`.tmp` + `fsync` + `os.replace`), verificación criptográfica SHA-256 contra corrupción física, locking thread-safe (`threading.RLock`) y agrupación lógica no destructiva sin borrado de registros ni selección de ganadores (L.8 boundary). | `src/domain/duplicate_detection/`, `src/application/duplicate_detection/`, `src/infrastructure/persistence/data/json/duplicate_detection_repository.py`, `tests/unit/test_l7_duplicate_detection_unit.py` (16 passed), `tests/integration/test_l7_duplicate_detection_integration.py` (10 passed); regresión completa: 1372 passed, 1 skipped |
| L.8 | Conflict Resolution | 🟢 VALIDADA | Resolución explícita, determinista, inmutable, reproducible y auditable de contradicciones entre datos válidos sobre una misma entidad canónica y campo (`ConflictCandidate`, `ConflictResolutionPolicy`, `ConflictResolutionResult`, `ConflictStatus`: `RESOLVED`/`UNRESOLVED`/`NO_CONFLICT`/`UNKNOWN`/`ERROR`, `ResolutionStrategy`: `SOURCE_PRIORITY`/`FRESHEST`/`HIGHEST_CONFIDENCE`/`CONSENSUS`/`MANUAL_REQUIRED`). Preservación incondicional de evidencia y valores originales sin sobreescritura destructiva, cero ganadores hardcodeados arbitrarios, integración sin duplicación con L.1 (Source Registry), L.2 (Data Provenance), L.3 (Freshness/TTL - descartando datos expirados y protegiendo UNKNOWN), L.4 (Confidence Model - sin asumir UNKNOWN como HIGH), L.5 (Schema Validation), L.6 (Entity Resolution) y L.7 (Duplicate Detection - protegiendo consenso contra inflación por duplicados/replays), convergencia segura a UNRESOLVED ante empates o evidencia insuficiente, persistencia atómica crash-safe JSON (`.tmp` + `fsync` + `os.replace`) con locking multihilo (`threading.RLock`), verificación criptográfica de integridad SHA-256 en lectura con detección de corrupción física y sanitización recursiva de credenciales. | `src/domain/conflict_resolution/`, `src/application/conflict_resolution/`, `src/infrastructure/persistence/data/json/conflict_resolution_repository.py`, `tests/unit/test_l8_conflict_resolution_unit.py` (18 passed), `tests/integration/test_l8_conflict_resolution_integration.py` (9 passed incluido E2E completo L.1->L.2->L.5->L.6->L.7->L.8); regresión completa: 1399 passed, 1 skipped |

### GATE K

🟢 PASSED.

Fecha de Validación: 2026-09-03
Tests: 1410 passed, 1 skipped, 0 failures (11 tests E2E específicos de Gate K en `tests/integration/test_gate_k_hito_l_e2e.py`)
Garantía Demostrada:
"Las decisiones comerciales críticas deben poder rastrearse hasta sus datos de origen."
Trazabilidad E2E Bidireccional Completa:
`COMMERCIAL DECISION → Conflict Resolution (L.8) → Duplicate / Entity Context (L.7 / L.6) → Schema Validation (L.5) → Confidence Assessment (L.4) → Freshness Assessment (L.3) → Data Provenance DAG (L.2) → Registered Root Sources (L.1)`
Demostración de No Falsa Certeza (Zero False Certainty):
- Conflictos no resueltos preservados (`UNRESOLVED` ante empates/falta de evidencia sin ganador arbitrario).
- Ambigüedad de entidades preservada (`POSSIBLE_MATCH / UNKNOWN` sin auto-merge no justificado).
- Esquemas inválidos rechazados (`FAIL / UNKNOWN` nunca admitidos como hechos comerciales).
- Falsos consensos bloqueados (múltiples replays duplicados de una misma fuente no inflan el conteo de votos independientes).
- Frescura y confianza degradadas respetadas (`STALE`, `EXPIRED`, `LOW`, `UNKNOWN` impiden fingir certeza analítica).
- Corrupción física en almacenamiento persistido detectada vía SHA-256 impidiendo falso trust.
- Persistencia atómica crash-safe durable y verificable post-reinicio.

---

# 15. Transversal M — Control de Coste e Inferencia

**Estado: 🟢 COMPLETO / VALIDADA**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| M.1 | Model Routing Strategy | 🟢 VALIDADA | Estrategia de enrutamiento determinista y estructurada (`DeterministicModelRoutingStrategy`) basada en modelos inmutables (`ModelRoute`, `RoutingRequest`, `RoutingDecision`, `RoutingPolicy`), filtrado por estado de salud (`AVAILABLE`, `DEGRADED`, `UNAVAILABLE`, `UNKNOWN`), capacidades técnicas (`TOOL_USE`, `STRUCTURED_OUTPUT`, `VISION`, `LONG_CONTEXT`, `REASONING`, `JSON_MODE`), criticidad/calidad requerida, latencia, techo de coste y desempate determinista lexicográfico por `route_id`. Integración no destructiva con `OmniRouteDecisionProvider`, deduplicación de razones de exclusión, preservación de incertidumbre y sanitización estricta de secretos/CoT. | `src/domain/model_routing/`, `src/application/model_routing/`, `src/infrastructure/llm/omniroute_decision_provider.py`, `tests/unit/test_m1_model_routing_strategy_unit.py` (14 passed), `tests/integration/test_m1_model_routing_strategy_integration.py` (8 passed) |
| M.2 | Context Budgeting | 🟢 VALIDADA | Presupuesto y evaluación determinista de capacidad de contexto (`ContextBudgetService`, `DeterministicTokenEstimator`) sobre modelos inmutables (`ContextBudgetRequest`, `ContextBudgetDecision`, `ContextBudgetPolicy`, `InputTokensBreakdown`), integración con rutas M.1 (`context_window`), aritmética canónica entera (`available_input = context_window - reserved_output - safety_margin`), protección estricta de reserva de salida y margen de seguridad, preservación de `UNKNOWN` ante capacidades no declaradas, desborde explícito (`OVER_BUDGET`) sin truncamiento ni compresión silenciosa, y sanitización estricta de secretos. | `src/domain/context_budget/`, `src/application/context_budget/`, `tests/unit/test_m2_context_budgeting_unit.py` (14 passed), `tests/integration/test_m2_context_budgeting_integration.py` (7 passed) |
| M.3 | Prompt Compression | 🟢 VALIDADA | Compresión determinista de prompt y contexto (`DeterministicPromptCompressor`) basada en prioridades inmutables (`PROTECTED`, `HIGH_PRIORITY`, `NORMAL`, `LOW_PRIORITY`, `REMOVABLE`), preservación estricta de componentes críticos (instrucciones del sistema, entrada de usuario y esquemas de tools), pipeline secuencial determinista (deduplicación exacta de contenido, compactación estructurada de JSON, poda de historial más antiguo, limitación de evidencia opcional), reporte auditable de tokens ahorrados y breakdown, checksum SHA-256 canónico y retorno explícito de `CANNOT_COMPRESS` sin truncamiento destructivo. | `src/domain/prompt_compression/`, `src/application/prompt_compression/`, `tests/unit/test_m3_prompt_compression_unit.py` (11 passed), `tests/integration/test_m3_prompt_compression_integration.py` (4 passed) |
| M.4 | Caching | 🟢 VALIDADA | Sistema determinista de caché de inferencias (`InferenceCacheService`, `InMemoryCacheRepository`, `JsonCacheRepository`) con generación de clave canónica `CacheKey` (SHA-256 de payload normalizado, route/model_id, tool schemas, params y versionado de política), seguridad semántica sin *false HITs*, expiración estricta por TTL desacoplada vía `ClockPort` (K.7), bloqueo total de almacenamiento para estados `ERROR`, `UNKNOWN`, denegaciones de política o efectos secundarios (`has_side_effects`), sanitización recursiva de secretos y aislamiento multi-tenant por `security_context_id`, persistencia atómica crash-safe con verificación de integridad por checksum SHA-256 y concurrencia segura con `threading.RLock`. | `src/domain/caching/`, `src/application/caching/`, `src/infrastructure/persistence/data/in_memory/cache_repository.py`, `src/infrastructure/persistence/data/json/cache_repository.py`, `tests/unit/test_m4_caching_unit.py` (14 passed), `tests/integration/test_m4_caching_integration.py` (9 passed) |
| M.5 | Model Selection by Task | 🟢 VALIDADA | Selección y parametrización declarativa de requerimientos de modelo por tipo de tarea (`ModelSelectionByTaskService`, `TaskModelProfile`, `TaskSelectionRequirements`, `TaskSelectionPolicy`, `StandardTaskType`) transformando el tipo de tarea y contexto en requerimientos técnicos para M.1 (`RoutingRequest`). Taxonomía basada en misiones reales del sistema (`MARKET_DISCOVERY`, `SUPPLIER_SEARCH`, `PROFIT_EVALUATION`, `CAPITAL_ALLOCATION`, `COMMERCIAL_REASONING`, `VISION_ANALYSIS`, `TOOL_EXECUTION_PLANNING`, etc.), mapeo determinista de complejidad (`LOW`, `MEDIUM`, `HIGH`, `UNKNOWN`), criticidad, capacidades técnicas (`TOOL_USE`, `STRUCTURED_OUTPUT`, `VISION`, etc.), calidad mínima y latencia, preservación estricta de incertidumbre (`UNKNOWN_TASK` / `NO_PROFILE` sin asignar modelo arbitrario por defecto), sin optimización económica invasiva (reserva para M.6), sanitización estricta de credenciales/secretos y exclusión de CoT, integración fluida en pipeline M.5 → M.1 → M.2 → M.3 → M.4. | `src/domain/model_selection/`, `src/application/model_selection/`, `tests/unit/test_m5_model_selection_by_task_unit.py` (15 passed), `tests/integration/test_m5_model_selection_by_task_integration.py` (8 passed); regresión completa: 1514 passed, 1 skipped |
| M.6 | Cost-aware Decision Policy | 🟢 VALIDADA | Política de decisión de inferencia consciente de costes (`CostAwareDecisionService`, `CostAwarePolicy`, `CostAwareRequest`, `CostAwareDecision`) respondiendo a: "¿Entre opciones técnicamente válidas, qué decisión cumple la política de coste sin violar calidad, capacidad o criticidad?". Principio *Quality First* estricto (capacidades y calidad mínima antes de coste), precisión monetaria absoluta con `Decimal` (cero floats), semántica rigurosa de incertidumbre (`UNKNOWN != FREE`), integración de omisión de inferencia por Cache HIT (M.4) con coste incremental 0.00, cálculo de coste sobre tokens finales post-compresión (M.3) y contexto M.2, vinculación no destructiva con el catálogo y registros de costes medidos en K.3 (`CostRecord`), desempate determinista por `route_id`, inmutabilidad estricta y sanitización de credenciales/secretos excluyendo CoT. | `src/domain/cost_aware_policy/`, `src/application/cost_aware_policy/`, `tests/unit/test_m6_cost_aware_decision_policy_unit.py` (15 passed), `tests/integration/test_m6_cost_aware_decision_policy_integration.py` (7 passed); regresión completa: 1536 passed, 1 skipped |

### GATE L

🟢 PASSED — Validación formal E2E ejecutada: misión comercial completa con selección M.5/M.1, presupuesto M.2, compresión M.3, caché M.4, política económica M.6 y coste real K.3 trazable. Evidencia: `tests/integration/test_gate_l_hito_m_e2e.py` (7 passed), regresión M.1–M.6 + K.3 (160 passed), regresión completa (1543 passed, 1 skipped), `GATE_L_HITO_M_VALIDATION_REPORT.md`.

---

# 16. Transversal N — Security, Governance y Safety

**Estado: 🟢 COMPLETO / VALIDADA**

| ID | Task | Estado | Criterio de validación | Evidencia / Tests |
|---|---|---|---|---|
| N.1 | Identity | 🟢 VALIDADA | Modelo canónico inmutable de identidad (`Identity`, `IdentityType`, `IdentityStatus`, `IdentityReference`, `PrincipalIdentity`) respondiendo a: "¿Quién o qué actor está realizando esta operación dentro del sistema?". Taxonomía canónica de actores reales (`USER`, `AGENT`, `SYSTEM`, `SERVICE`, `SCHEDULER`, `MARKETPLACE`, `EXTERNAL_TOOL`, `UNKNOWN`), identificador canónico determinista (`build_canonical_identifier`) y checksum criptográfico SHA-256 (`compute_identity_checksum`). Frontera estricta de seguridad: separación total de autenticación (N.2), autorización (N.3) y permisos RBAC (N.4). Mapeo determinista de sujetos externos/OAuth (MercadoLibre) sin almacenar credenciales, access/refresh tokens ni CoT. Adaptadores y mappers interoperables con `AuditActor` (K.1) y `AgentTraceRecord` (K.2). Persistencia JSON atómica crash-safe (`.tmp` + `fsync` + `os.replace`), detección de colisiones/conflictos semánticos, prevención de path traversal y detección de corrupción física. | `src/domain/identity/`, `src/application/identity/`, `src/infrastructure/persistence/data/json/identity_repository.py`, `tests/unit/test_n1_identity_unit.py` (14 passed), `tests/integration/test_n1_identity_integration.py` (10 passed); regresión completa: 1567 passed, 1 skipped, 0 failures |
| N.2 | Authentication | 🟢 VALIDADA | Modelos canónicos inmutables (`AuthenticationMethod`, `AuthenticationStatus`, `AuthenticationRequest`, `AuthenticationResult`, `PrincipalContext`) y `AuthenticationService` respondiendo a: "¿Puede este actor demostrar de forma válida que es la identidad que declara?". Estados obligatorios (`AUTHENTICATED`, `UNAUTHENTICATED`, `EXPIRED`, `INVALID`, `UNKNOWN`, `ERROR`), resultado explícito con `identity_id/principal`, `method`, `provider`, `status`, `authenticated_at`, `expires_at`, `reason_codes`, `correlation_id`, metadata sanitizada y checksum SHA-256 determinista. Cero secretos en resultados/logs/persistencia (K.8). Reuso de OAuth/MercadoLibre (Hito E) e IdentityService N.1: mismo subject => misma `identity_id` aunque el token rote (token != identity). UNKNOWN nunca se promueve a AUTHENTICATED; expirado => EXPIRED, inválido => INVALID (no fallback permisivo). Actores internos requieren contrato reconocido explícito (`trusted_internal_tokens`) - no se asume "interno = autenticado". Auditoría/trazabilidad segura K.1/K.2 (`AUTHENTICATION_EVALUATED`) sin duplicados en replay. Frontera estricta: NO evalúa permisos (N.3) ni RBAC (N.4), NO llama a PolicyEngine, NO gestiona secretos (N.5). | `src/domain/authentication/`, `src/application/authentication/`, `src/domain/audit/models.py` (EXTEND mínimo `AuditRecordType.AUTHENTICATION_EVALUATED`), `tests/unit/test_n2_authentication_unit.py` (16 passed), `tests/integration/test_n2_authentication_integration.py` (10 passed incluido E2E N.2 A-H); regresión relevante N.1/K.1/K.2/K.8/OAuth (122 passed); regresión completa: 1593 passed, 1 skipped, 0 failures |
| N.3 | Authorization | 🟢 VALIDADA | Mecanismo de autorización desacoplado, formal, determinista e inmutable (`AuthorizationRequest`, `AuthorizationDecision`, `AuthorizationStatus`: `ALLOW`/`DENY`/`UNKNOWN`/`ERROR`, `AuthorizationReasonCode`, `AuthorizationService`, `AuthorizationGuardedActionExecutor`) respondiendo a: "¿Esta identidad autenticada puede realizar esta acción sobre este recurso/contexto?". Precondición estricta de autenticación N.2 (`is_authenticated=True` obligatorio; `UNAUTHENTICATED`, `EXPIRED`, `INVALID`, `UNKNOWN` impiden `ALLOW`). Default DENY ante ausencia de política o falta de matching (`UNKNOWN`/`DENY`, nunca default `ALLOW`). Reutilización directa del `PolicyEngine` (Hito E.3) para evaluación de reglas de negocio/gobernanza y `PolicyEvaluationContext`. Sensibilidad al contexto de recurso (`ResourceReference`, `target_resource`), segregación de acciones y determinismo absoluto con checksum criptográfico SHA-256 (`compute_authorization_checksum`). Intercepción obligatoria previa a la frontera de ejecución externa (`AuthorizationGuardedActionExecutor`) bloqueando mutaciones físicas si la decisión no es `ALLOW`. Registro seguro de auditoría en K.1 (`AuditRecordType.AUTHORIZATION_EVALUATED`) y pasos de traza en K.2 (`StepType.POLICY_EVALUATION`) con sanitización recursiva profunda K.8 (cero tokens, secretos ni CoT). Cero implementación de RBAC o catálogos de roles (N.4 boundary), cero flujos de aprobación humana (N.6) y cero límites financieros (N.7). | `src/domain/authorization/`, `src/application/authorization/`, `src/domain/audit/models.py` (`AuditRecordType.AUTHORIZATION_EVALUATED`), `tests/unit/test_n3_authorization_unit.py` (15 passed), `tests/integration/test_n3_authorization_integration.py` (9 passed incluido E2E N.3 A–I); regresión completa: 1617 passed, 1 skipped, 0 failures |
| N.4 | RBAC / Permissions | 🟢 VALIDADA | Mecanismo formal, determinista e inmutable de control de acceso basado en roles (`Permission`, `Role`, `RoleAssignment`, `PermissionSet`, `RbacEvaluationResult`, `RBACService`, `JsonRoleRepository`, `JsonRoleAssignmentRepository`) respondiendo a: "¿Qué roles y permisos tiene asignados esta identidad y qué capacidades concretas representan?". Semántica canónica explícita y granular de permisos de comercio (`LISTING_READ`, `LISTING_PUBLISH`, `PRICE_UPDATE`, `INVENTORY_UPDATE`, `ORDER_READ`, `ORDER_MANAGE`, `RETURN_MANAGE`, `EXTERNAL_ACTION_EXECUTE`) con normalización canónica determinista. Roles de dominio (`VIEWER`, `OPERATOR`, `AGENT`, `SERVICE`, `ADMIN`) y resolución de capacidades efectivas mediante unión de asignaciones. Default DENY estricto: identidades desconocidas, roles inactivos (`DISABLED`, `DEPRECATED`), asignaciones ausentes o registros corruptos no otorgan permisos (cero rol privilegiado por defecto y cero escalada de privilegios). Aislamiento y validación de scopes (`account_id`, `marketplace_id`, `resource_scope`). Soporte de asignaciones temporales con expiración precisa gobernada por `ClockPort` (K.7). Persistencia crash-safe basada en JSON atómico (`.tmp` + `fsync` + `os.replace`), detección de colisiones, checksums criptográficos SHA-256 (`compute_permission_checksum`, `compute_role_checksum`, `compute_role_assignment_checksum`, `compute_evaluation_checksum`) y thread-safety (`RLock`). Integración downstream limpia con `AuthorizationService` y `AuthorizationGuardedActionExecutor` (N.3) vía `allowed_actions_override`. Registro seguro de auditoría en K.1 (`ROLE_ASSIGNED`, `ROLE_REVOKED`, `RBAC_EVALUATED`) y pasos en K.2 con sanitización K.8 (`sanitize_security_data`). Cero ejecución de acciones, cero approval policies (N.6) y cero límites financieros (N.7). | `src/domain/rbac/`, `src/application/rbac/`, `src/infrastructure/persistence/data/json/rbac_repository.py`, `src/domain/audit/models.py` (`ROLE_ASSIGNED`, `ROLE_REVOKED`, `RBAC_EVALUATED`), `tests/unit/test_n4_rbac_permissions_unit.py` (16 passed), `tests/integration/test_n4_rbac_permissions_integration.py` (9 passed incluido E2E N.4 A–I); regresión relevante N.1/N.2/N.3/K.1/K.2/K.7/K.8 (143 passed); regresión completa: 1642 passed, 1 skipped, 0 failures |
| N.5 | Secret Management | 🟢 VALIDADA | Mecanismo canónico, seguro, inmutable y desacoplado de gestión de secretos (`SecretValue`, `SecretReference`, `SecretMetadata`, `SecretResolutionResult`, `SecretService`, `JsonSecretMetadataRepository`, `EnvSecretProvider`, `InjectedSecretProvider`, `OAuthSecretProviderBridge`) respondiendo a: "¿Cómo obtiene, almacena, referencia, rota y usa el sistema credenciales/secretos sin exponerlos ni convertirlos en datos de dominio?". Aislamiento estricto de material confidencial en memoria volátil mediante wrapper seguro (`SecretValue`) con `__repr__` y `__str__` redactados (`[REDACTED]`) y exclusión explícita de serialización JSON/iteración accidental. Taxonomía canónica mínima (`API_KEY`, `CLIENT_SECRET`, `ACCESS_TOKEN`, `REFRESH_TOKEN`, `WEBHOOK_SECRET`, `INTERNAL_CREDENTIAL`, `UNKNOWN`). Preservación estricta de la frontera de dominio: modelos de negocio e identidades referencian credenciales únicamente mediante `SecretReference`/`SecretMetadata` inmutables sin almacenar cadenas en claro. Persistencia estructurada exclusiva de metadatos no sensibles (`JsonSecretMetadataRepository`) con verificación de integridad por checksum SHA-256 (`compute_secret_metadata_checksum`), soporte crash-safe atómico y prevención de path traversal. Puente seguro desacoplado con tokens OAuth (`OAuthSecretProviderBridge`) reusando `OAuthConnectionRepository` sin duplicación de almacenamiento. Soporte determinista de rotación y versionado de secretos sin invalidar identidades N.1 ni requerir mutación de permisos N.4. Auditoría y trazas K.1/K.2 seguras (`SECRET_RESOLVED`, `SECRET_ROTATED`) sin fuga de valor secreto ni presencia en claves de caché M.4. Cero implementación de N.6–N.11. | `src/domain/secrets/`, `src/application/secrets/`, `src/infrastructure/secrets/`, `src/infrastructure/persistence/data/json/secret_metadata_repository.py`, `src/domain/audit/models.py` (`SECRET_RESOLVED`, `SECRET_ROTATED`), `tests/unit/test_n5_secret_management_unit.py` (16 passed), `tests/integration/test_n5_secret_management_integration.py` (9 passed incluido E2E N.5 A–I); regresión relevante N.1/N.2/N.3/N.4/K.1/K.2/K.8/OAuth (124 passed); regresión completa: 1667 passed, 1 skipped, 0 failures |
| N.6 | Approval Policies | 🟢 VALIDADA | Mecanismo determinista, formal e inmutable de políticas y evidencias de aprobación (`ApprovalPolicy`, `ApprovalRequest`, `ApprovalDecision`, `ApprovalEvidence`, `ApprovalStatus`, `ApprovalPolicyService`, `JsonApprovalEvidenceRepository`) respondiendo a: "¿Esta acción requiere aprobación explícita antes de ejecutarse y, si la requiere, existe una aprobación válida?". Distinción semántica estricta entre N.3 Autorización y N.6 Aprobación (N.3 ALLOW no satisface aprobación por sí mismo; Approval nunca sobreescribe un DENY de N.3). Vinculación inmutable de evidencias (`ApprovalEvidence`) a `action + resource + requester + context + policy/version` con prevención de replay cruzado. Control de separación de funciones (Anti-Self-Approval: `requesting_identity_id == approver_identity_id` bloqueado cuando la política requiere separación). Expiración temporal determinista gobernada por `ClockPort` (K.7). Persistencia atómica JSON crash-safe (`.tmp` + `fsync` + `os.replace`), detección de manipulación mediante checksums SHA-256 (`compute_approval_checksum`), prevención de path traversal y thread-safety (`RLock`). Integración como barrera de seguridad en `AuthorizationGuardedActionExecutor` bloqueando mutaciones físicas (0 llamadas) ante estados `APPROVAL_REQUIRED`, `REJECTED`, `EXPIRED`, `UNKNOWN` o `ERROR`. Auditoría y trazas K.1/K.2 seguras (`APPROVAL_EVALUATED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`) con sanitización recursiva K.8. Cero implementación de límites financieros numéricos N.7 (`max_amount`, `daily_limit`, etc.), cero N.8-N.11. | `src/domain/approval/`, `src/application/approval/`, `src/infrastructure/persistence/data/json/approval_evidence_repository.py`, `src/application/authorization/authorization_guarded_action_executor.py`, `src/domain/audit/models.py` (`APPROVAL_EVALUATED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`), `tests/unit/test_n6_approval_policies_unit.py` (16 passed), `tests/integration/test_n6_approval_policies_integration.py` (11 passed incluido E2E N.6 A–K); regresión relevante N.1/N.2/N.3/N.4/N.5/N.6 (104 passed); regresión completa: 1694 passed, 1 skipped, 0 failures |
| N.7 | Financial Limits | 🟢 VALIDADA | Mecanismo formal, determinista e inmutable de límites financieros y riesgo económico comercial (`FinancialLimitPolicy`, `FinancialLimitRequest`, `FinancialLimitDecision`, `FinancialLimitRule`, `FinancialLimitService`, `JsonFinancialLimitRepository`) respondiendo a: "¿Esta operación financiera/comercial está dentro de los límites económicos permitidos?". Distinción estricta frente a M.6 (M.6 controla inferencia/tokens; N.7 gobierna transacciones, reembolsos, cambios de precio, precios máx/mín y exposición comercial). Taxonomía de límites (`MAX_TRANSACTION_AMOUNT`, `MAX_REFUND_AMOUNT`, `MAX_PRICE_CHANGE`, `MIN_ALLOWED_PRICE`, `MAX_ALLOWED_PRICE`, `MAX_ORDER_VALUE`, `MAX_EXPOSURE`, `DAILY_SPEND_LIMIT`) con precisión monetaria estricta en `Decimal` y moneda explícita reutilizando el modelo canónico inmutable `Money` (cero `float`, cero coerción silenciosa o conversión FX arbitraria). Estados canónicos (`WITHIN_LIMIT`, `LIMIT_EXCEEDED`, `APPROVAL_REQUIRED`, `REJECTED`, `UNKNOWN`, `ERROR`). Política fail-safe por defecto (ausencia de política/regla => `UNKNOWN`/`REJECTED`, jamás ilimitado). Integración como barrera de seguridad en `AuthorizationGuardedActionExecutor` respetando la precedencia de seguridad: `N.1 Identity -> N.2 Auth -> N.4 RBAC -> N.3 Authorization -> N.7 Financial Limits -> N.6 Approval -> Boundary`. N.7 no puede sobreescribir `N.3 DENY` ni inventar evidencias de aprobación N.6. Persistencia JSON crash-safe (`.tmp` + `fsync` + `os.replace`), detección de manipulación mediante checksums SHA-256 (`compute_financial_policy_checksum`, `compute_financial_decision_checksum`), prevención de path traversal y thread-safety (`RLock`). Registro auditable en K.1 (`FINANCIAL_LIMIT_EVALUATED`, `LIMIT_EXCEEDED`) y pasos K.2 sin filtrar secretos N.5 ni CoT. Cero implementación de N.8–N.11. | `src/domain/financial_limit/`, `src/application/financial_limit/`, `src/infrastructure/persistence/data/json/financial_limit_repository.py`, `src/application/authorization/authorization_guarded_action_executor.py`, `src/domain/audit/models.py` (`FINANCIAL_LIMIT_EVALUATED`, `LIMIT_EXCEEDED`), `tests/unit/test_n7_financial_limits_unit.py` (16 passed), `tests/integration/test_n7_financial_limits_integration.py` (11 passed incluido E2E N.7 A–K); regresión relevante N.1/N.2/N.3/N.4/N.5/N.6/N.7 (178 passed); regresión completa: 1721 passed, 1 skipped, 0 failures |
| N.8 | Tool Allowlist / Denylist | 🟢 VALIDADA | Gobernanza determinista y fail-secure sobre herramientas, proveedores y operaciones externas (`ToolPolicy`, `ToolPolicyRule`, `ToolReference`, `ToolAccessRequest`, `ToolAccessDecision`, `ToolAccessPolicyService`, `JsonToolPolicyRepository`) respondiendo a: "¿Está permitido invocar esta herramienta/operación concreta dentro del contexto actual?". Semántica de políticas estricta con precedencia de resolución (`Explicit DENY` > `Explicit Scoped ALLOW` > `Default DENY`). Normalización determinista anti-bypass para herramientas y proveedores (eliminación de espacios, minúsculas canónicas, identificadores canónicos `provider:tool_id`). Control granular de efectos colaterales (`READ_ONLY`, `ANALYSIS`, `WRITE`, `EXTERNAL_SIDE_EFFECT`, `IRREVERSIBLE`) con bloqueo estricto ante escalada de permisos. Restricciones por rol, scopes contextuales, cuenta y misión. Inmutabilidad estricta y checksums SHA-256 canónicos. Persistencia JSON crash-safe atómica (`.tmp` + `fsync` + `os.replace`) con detección de manipulación física/corrupción. Integración en la barrera de ejecución `AuthorizationGuardedActionExecutor` garantizando 0 ejecuciones físicas ante `DENY`, `UNKNOWN` o `ERROR`. Registro auditable seguro en K.1 (`TOOL_ACCESS_EVALUATED`, `TOOL_ACCESS_DENIED`) con sanitización recursiva profunda K.8 (cero secretos N.5, sin CoT). Cero implementación de N.9–N.11. | `src/domain/tool_policy/`, `src/application/tool_policy/`, `src/infrastructure/persistence/data/json/tool_policy_repository.py`, `src/application/authorization/authorization_guarded_action_executor.py`, `src/domain/audit/models.py` (`TOOL_ACCESS_EVALUATED`, `TOOL_ACCESS_DENIED`), `tests/unit/test_n8_tool_allowlist_denylist_unit.py` (16 passed), `tests/integration/test_n8_tool_allowlist_denylist_integration.py` (11 passed incluido E2E N.8 A–K); regresión completa: 1748 passed, 1 skipped, 0 failures |
| N.9 | Sensitive Data Handling | 🟢 VALIDADA | Clasificación, minimización, enmascaramiento, protección y control determinista e inmutable de datos sensibles (`SensitiveDataClassification`, `SensitiveFieldDescriptor`, `DataHandlingPolicy`, `DataHandlingRequest`, `DataHandlingDecision`, `RedactionResult`, `SensitiveDataHandlingService`, `JsonDataHandlingPolicyRepository`) respondiendo a: "¿Cómo clasifica, minimiza, protege, redacta y controla el sistema los datos sensibles que atraviesan dominio, memoria, prompts, logs, auditoría, trazas, cachés y persistencia?". Taxonomía canónica de clasificación (`PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `SENSITIVE`, `RESTRICTED`, `UNKNOWN`) y categorías reales (`PERSONAL_DATA`, `CONTACT_DATA`, `ADDRESS_DATA`, `FINANCIAL_DATA`, `ORDER_DATA`, `SUPPLIER_CONFIDENTIAL`, `MARKETPLACE_ACCOUNT_DATA`, `PRIVATE_PROMPT_CONTEXT`, `BUSINESS_CONFIDENTIAL`, `TECHNICAL_SECRET`, `UNKNOWN`). Principio fail-secure estricto (`UNKNOWN != PUBLIC`), enmascaramiento determinista no destructivo de PII (email, teléfono, DNI/RUT/Pasaporte, tarjeta), eliminación/redacción profunda de credenciales accidentales y CoT/razonamiento privado, minimización estricta basada en propósito (`INFERENCE`, `AUDIT`, `MARKETPLACE_OPERATION`, `ORDER_FULFILLMENT`, `SUPPLIER_CONTACT`, `CACHE`, `LOGGING`, `STORAGE`, `GENERAL`), generación de fingerprints deterministas seguros para claves de caché M.4 (`compute_deterministic_fingerprint`) sin persistir PII en claro. Persistencia atómica crash-safe en JSON con checksums SHA-256 e integridad criptográfica. Integración transparente como filtro de sanitización y minimización en `AuthorizationGuardedActionExecutor` previa a la ejecución física del delegate. Registro auditable seguro en K.1 (`AuditRecordType.POLICY_EVALUATED`) sin payload sensible en texto claro. Distinción estricta de N.5 (N.5 gobierna el material confidencial técnico; N.9 gobierna PII y datos de negocio sensibles). Cero implementación de N.10 (Audit/Compliance) ni N.11 (Emergency Stop). | `src/domain/security/sensitive_data_models.py`, `src/domain/security/sensitive_data_ports.py`, `src/domain/security/sensitive_data_engine.py`, `src/application/security/sensitive_data_handling_service.py`, `src/infrastructure/persistence/data/json/sensitive_data_policy_repository.py`, `src/application/authorization/authorization_guarded_action_executor.py`, `tests/unit/test_n9_sensitive_data_handling_unit.py` (16 passed), `tests/integration/test_n9_sensitive_data_handling_integration.py` (11 passed incluido E2E N.9 A–J + multicapa); regresión completa: 1775 passed, 1 skipped, 0 failures |
| N.10 | Audit / Compliance | 🟢 VALIDADA | Evaluación determinista, estructurada y verificable de cumplimiento y gobernanza (`ComplianceRequirement`, `CompliancePolicy`, `ComplianceCheck`, `ComplianceFinding`, `ComplianceAssessment`, `ComplianceEvidenceReference`, `ComplianceReport`, `ComplianceAssessmentService`, `DefaultComplianceEvidenceCollector`, `InMemoryCompliancePolicyRepository`) respondiendo a: "¿Podemos demostrar de forma verificable que una operación cumplió —o violó— las políticas de seguridad y gobernanza aplicables?". Reconstrucción retrospectiva e inmutable de las 9 dimensiones operacionales (N.1 Identidad, N.2 Autenticación, N.4 RBAC, N.3 Autorización, N.8 Tool Policy, N.9 Protección de Datos Sensibles, N.7 Límites Financieros, N.6 Aprobaciones, y Ejecución/Bloqueo en frontera). Principio fail-secure estricto (`UNKNOWN != COMPLIANT`, evidencia faltante => `INCOMPLETE`/`NON_COMPLIANT`, integridad alterada => `AUDIT_INTEGRITY_FAILURE`), regla de policy chain condicional ("no exigir pasos que no aplican a la operación"), evaluación determinista de bloqueos seguros (`COMPLIANT_BLOCKED_ACTION`), aislamiento estricto por correlación/misión con rechazo de evidencia cruzada (`CROSS_CORRELATION_EVIDENCE_REJECTED`), sanitización y minimización profunda respetando N.9/N.5 (cero PII en claro, cero secretos, cero CoT en reportes), inmutabilidad de políticas mediante versionado explícito y checksums criptográficos SHA-256 (`compute_compliance_policy_checksum`, `compute_assessment_checksum`, `compute_finding_checksum`, `compute_report_checksum`). Cero duplicación de almacenamiento de K.1 Audit Trail o K.2 Agent Trace (evaluación desacoplada de solo lectura). Cero implementación de N.11 Emergency Stop ni Gate M. | `src/domain/compliance/`, `src/application/compliance/`, `src/infrastructure/persistence/data/in_memory/compliance_policy_repository.py`, `tests/unit/test_n10_audit_compliance_unit.py` (16 passed), `tests/integration/test_n10_audit_compliance_integration.py` (11 passed incluido E2E N.10 A–D); regresión relevante N.1–N.10/K.1/K.2/K.8/PolicyEngine/ActionExecutor (234 passed); regresión completa: 1802 passed, 1 skipped, 0 failures |
| N.11 | Emergency Stop | 🟢 VALIDADA | Control superior de gobernanza y parada de emergencia determinista e inmutable (`EmergencyStopRecord`, `EmergencyStopScope`, `EmergencyStopState`, `EmergencyStopDecisionStatus`, `EmergencyStopReasonCode`, `EmergencyStopEvaluationContext`, `EmergencyStopDecision`, `EmergencyStopService`, `JsonEmergencyStopRepository`) respondiendo a: "¿Puede el sistema bloquear inmediatamente nuevas acciones sensibles/externas cuando una condición de emergencia exige detener la autonomía?". Jerarquía determinista de scopes con precedencia estricta (`GLOBAL` > `MARKETPLACE` > `ACCOUNT` > `MISSION` > `TOOL` > `ACTION_TYPE`), control granular de efectos colaterales (permite `allow_read_only=True` mientras bloquea `is_external_side_effect=True`), seguridad en activación y desactivación reutilizando N.1–N.4 con permisos explícitos (`EMERGENCY_STOP_ACTIVATE`, `EMERGENCY_STOP_DEACTIVATE`), fail-secure estricto ante estados `UNKNOWN` o corrupción del repositorio (`FAIL_SAFE_STORE_CORRUPTION`), expiración determinista gobernada por `ClockPort` (K.7), persistencia crash-safe atómica en JSON (`.tmp` + `fsync` + `os.replace`) con checksum SHA-256 canónico y thread-safety (`RLock`). Integración en la frontera superior de ejecución `AuthorizationGuardedActionExecutor` garantizando 0 llamadas físicas cuando el stop aplica, integración con `AutonomousLoop` para detener mutaciones autónomas de fondo sin destruir entidades ni historiales (non-destructive safety), registro auditable en K.1 (`EMERGENCY_STOP_ACTIVATED`, `EMERGENCY_STOP_DEACTIVATED`, `EMERGENCY_STOP_EVALUATED`, `EXECUTION_BLOCKED_BY_EMERGENCY_STOP`) y verificación de cumplimiento formal en N.10 (`ComplianceAssessmentService` con `REQ_EMERGENCY_STOP`). | `src/domain/emergency_stop/`, `src/application/emergency_stop/`, `src/infrastructure/persistence/data/json/emergency_stop_repository.py`, `src/application/authorization/authorization_guarded_action_executor.py`, `src/domain/audit/models.py` (`EMERGENCY_STOP_ACTIVATED`, `EMERGENCY_STOP_DEACTIVATED`, `EMERGENCY_STOP_EVALUATED`, `EXECUTION_BLOCKED_BY_EMERGENCY_STOP`), `tests/unit/test_n11_emergency_stop_unit.py` (12 passed), `tests/integration/test_n11_emergency_stop_integration.py` (9 passed incluido E2E N.11 A–K); regresión relevante N.1–N.11/K.1/K.2/K.7/K.8/AutonomousLoop/ActionExecutor/Compliance (280 passed); regresión completa: 1823 passed, 1 skipped, 0 failures |

### GATE M

🟢 PASSED — Validación formal E2E ejecutada: Cadena completa de gobernanza N.1 a N.11 demostrada de extremo a extremo sin interrupciones. Ninguna acción financiera o externa de alto impacto puede ejecutarse sin cumplir la política correspondiente. Validación de 14 escenarios canónicos: Happy path, Fallo Auth, Denegación RBAC/N.3, Denegación N.8 Tool Policy, Redacción/Minimización N.9, Bloqueo de Límite Financiero N.7, Override Válido N.6/N.7, Aprobación Expirada/Invalida N.6, Bloqueo Seguro por Secreto Faltante N.5, Bloqueo por Parada de Emergencia N.11, Bloqueo Legítimo COMPLIANT N.10, Bypass Detectado NON_COMPLIANT N.10, Aislamiento por Misión / Anti Cross-Correlation, y Fail-Safe ante Registros Corruptos/Tampering. Cero llamadas físicas en escenarios bloqueados. Evidencia: `tests/integration/test_gate_m_hito_n_e2e.py` (14 passed), regresión N.1–N.11 (169 passed), Full Regression (1837 passed, 1 skipped, 0 failures), `GATE_M_HITO_N_VALIDATION_REPORT.md`.

---

# 17. Hito O — SaaS / Platformization

**Estado: 🟢 VALIDADA (2026-09-10 — Gate N PASSED)**

| ID | Task | Estado | Evidencia |
|---|---|---|---|
| O.1 | Tenant Isolation | 🟢 VALIDADA | `O1_TENANT_ISOLATION_EXECUTION_REPORT.md` |
| O.2 | Organizations / Users | 🟢 VALIDADA | `O2_ORGANIZATIONS_USERS_EXECUTION_REPORT.md` |
| O.3 | Authentication | 🟢 VALIDADA | `O3_AUTHENTICATION_EXECUTION_REPORT.md` |
| O.4 | Authorization | 🟢 VALIDADA | `O4_AUTHORIZATION_RBAC_EXECUTION_REPORT.md` |
| O.5 | Model Gateway | 🟢 VALIDADA | `O5_MODEL_GATEWAY_EXECUTION_REPORT.md` |
| O.6 | Usage Metering | 🟢 VALIDADA | `O6_USAGE_METERING_EXECUTION_REPORT.md` |
| O.7 | Quota Management | 🟢 VALIDADA | `O7_QUOTA_MANAGEMENT_EXECUTION_REPORT.md` |
| O.8 | Plans | 🟢 VALIDADA | `O8_PLANS_ENTITLEMENTS_EXECUTION_REPORT.md` |
| O.9 | Billing | 🟢 VALIDADA | `O9_BILLING_EXECUTION_REPORT.md` |
| O.10 | Admin Console | 🟢 VALIDADA | `O10_ADMIN_CONSOLE_EXECUTION_REPORT.md` |
| O.11 | Tenant-level Configuration | 🟢 VALIDADA | `O11_TENANT_CONFIGURATION_EXECUTION_REPORT.md` |
| O.12 | Observability | 🟢 VALIDADA | `O12_OBSERVABILITY_EXECUTION_REPORT.md` |
| O.13 | Deployment Automation | 🟢 VALIDADA | `O13_DEPLOYMENT_AUTOMATION_EXECUTION_REPORT.md` |

### GATE N

🟢 PASSED — Validación formal E2E ejecutada: 16 escenarios canónicos de aislamiento multi-tenant demostrados (`test_gate_n_hito_o_e2e.py`), 357 tests O.1–O.13 (integración + unit) passed, 191 transversales (N.1–N.11/M.1–M.6/K/L.5) passed, baseline completa 2215 passed 1 skipped 0 failures, deploy_validate.py 5/5 checks PASSED, NO commit, NO push. `GATE_N_HITO_O_VALIDATION_REPORT.md`.

---

# 18. Hito P — Production / Operations

**Estado: 🟢 VALIDADA**

| ID | Task | Estado | Reporte / Evidencia |
|---|---|---|---|
| P.1 | CI/CD | 🟢 VALIDADA | `P1_CI_CD_EXECUTION_REPORT.md` |
| P.2 | Environment Separation | 🟢 VALIDADA | `P2_ENVIRONMENT_SEPARATION_EXECUTION_REPORT.md` |
| P.3 | Database Migrations | 🟢 VALIDADA | `P3_DATABASE_MIGRATIONS_EXECUTION_REPORT.md` |
| P.4 | Backups | 🟢 VALIDADA | `P4_BACKUPS_EXECUTION_REPORT.md` |
| P.5 | Disaster Recovery | 🟢 VALIDADA | `P5_DISASTER_RECOVERY_EXECUTION_REPORT.md` |
| P.6 | Health Checks | 🟢 VALIDADA | `P6_HEALTH_CHECKS_EXECUTION_REPORT.md` |
| P.7 | Monitoring | 🟢 VALIDADA | `P7_MONITORING_EXECUTION_REPORT.md` |
| P.8 | Alerting | 🟢 VALIDADA | `P8_ALERTING_EXECUTION_REPORT.md` |
| P.9 | Log Retention | 🟢 VALIDADA | `P9_LOG_RETENTION_EXECUTION_REPORT.md` |
| P.10 | Capacity Planning | 🟢 VALIDADA | `P10_CAPACITY_PLANNING_EXECUTION_REPORT.md` |
| P.11 | Rate-limit Management | 🟢 VALIDADA | `P11_RATE_LIMIT_MANAGEMENT_EXECUTION_REPORT.md` |

### GATE O

🟢 PASSED — Validación formal E2E ejecutada: 16 escenarios canónicos de producción/operaciones demostrados (`tests/integration/test_gate_o_hito_p_e2e.py`), 240 tests P.1–P.11 (integración + unit) passed, 181 transversales (O.1, O.5, O.7, O.12, N.5, N.9, K.1) passed, baseline completa 2471 passed 2 skipped 0 failures, deploy_validate.py 5/5 checks PASSED, db_migrate check UP TO DATE (revision 001_initial_saas_schema), backup real validado con SHA-256 streaming, disaster recovery simulation validado con RPO/RTO compliant, NO commit, NO push. Reporte formal: `GATE_O_HITO_P_VALIDATION_REPORT.md`.

---

# 19. Hito Q — Business Intelligence

**Estado: 🟢 VALIDADA**

| ID | Task | Estado | Reporte / Evidencia |
|---|---|---|---|
| Q.1 | Opportunity Dashboard | 🟢 VALIDADA | `Q1_OPPORTUNITY_DASHBOARD_EXECUTION_REPORT.md` |
| Q.2 | Supplier Dashboard | 🟢 VALIDADA | `Q2_SUPPLIER_DASHBOARD_EXECUTION_REPORT.md` |
| Q.3 | Profit Dashboard | 🟢 VALIDADA | `Q3_PROFIT_DASHBOARD_EXECUTION_REPORT.md` |
| Q.4 | Mission Dashboard | 🟢 VALIDADA | `Q4_MISSION_DASHBOARD_EXECUTION_REPORT.md` |
| Q.5 | Agent Cost Dashboard | 🟢 VALIDADA | `Q5_AGENT_COST_DASHBOARD_EXECUTION_REPORT.md` |
| Q.6 | Business KPIs | 🟢 VALIDADA | `Q6_BUSINESS_KPIS_EXECUTION_REPORT.md` |

### GATE P

🟢 PASSED — Formal Hito Q Validation. La plataforma transforma los hechos reales producidos por el sistema autónomo en Business Intelligence confiable, trazable, multi-tenant y accionable, sin inventar datos ni duplicar motores de dominio. Validado en `GATE_P_HITO_Q_VALIDATION_REPORT.md` con suite E2E (`tests/integration/test_gate_p_hito_q_e2e.py`), 131 tests en suite BI y 2602 tests pasando en regresión completa.

---

# 20. Hito R — Advanced Autonomy

**Estado: 🟢 VALIDADA**

| ID | Task | Estado | Reporte / Evidencia |
|---|---|---|---|
| R.1 | Multi-step Planning | 🟢 VALIDADA | `R1_MULTI_STEP_PLANNING_EXECUTION_REPORT.md` |
| R.2 | Sub-missions | 🟢 VALIDADA | `R2_SUB_MISSIONS_EXECUTION_REPORT.md` |
| R.3 | Specialist Agents | 🟢 VALIDADA | `R3_SPECIALIST_AGENTS_EXECUTION_REPORT.md` |
| R.4 | Agent Coordination | 🟢 VALIDADA | `R4_AGENT_COORDINATION_EXECUTION_REPORT.md` |
| R.5 | Dynamic Delegation | 🟢 VALIDADA | `R5_DYNAMIC_DELEGATION_EXECUTION_REPORT.md` |
| R.6 | Long-running Missions | 🟢 VALIDADA | `R6_LONG_RUNNING_MISSIONS_EXECUTION_REPORT.md` |
| R.7 | Self-monitoring | 🟢 VALIDADA | `R7_SELF_MONITORING_EXECUTION_REPORT.md` |

### GATE P

🟢 PASSED — Formal Hito R Validation. R.1–R.7 funcionan de extremo a extremo como un sistema autónomo coherente, seguro, trazable y multi-tenant. Validado en `GATE_P_HITO_R_ADVANCED_AUTONOMY_VALIDATION_REPORT.md` con 16 escenarios canónicos E2E, 239 tests R.1–R.7, 84 tests transversales y regresión completa de 2831 passed, 18 skipped, 0 failures, 0 errors; `deploy_validate.py` 5/5.

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
| 2026-09-07 | — | Gate M / Hito N (Transversal N) | 🟢 | 1837 tests pass, 1 skipped, 0 failures + E2E Gate M Validation (14 escenarios canónicos) |
| 2026-09-10 | — | Gate N / Hito O (SaaS/Platformization) | 🟢 | 2215 tests pass, 1 skipped, 0 failures + E2E Gate N Validation (16 escenarios canónicos) + deploy_validate 5/5 |

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
| G | Business Memory | 🟢 PASSED | 2026-08-31 | E2E Business Memory Integration (`test_hito_h_business_memory_e2e.py`, 650 tests pass) |
| H | Learning Loop | ⚪ | | |
| I | Continuous Autonomy | ⚪ | | |
| J | Observability/Reliability | ⚪ | | |
| K | Data Quality/Governance | ⚪ | | |
| L | Cost/Inference | ⚪ | | |
| M | Security/Safety | 🟢 PASSED | 2026-09-07 | E2E Gate M Validation (`test_gate_m_hito_n_e2e.py` - 14 escenarios canónicos PASSED, 1837 passed, 1 skipped, 0 failures) |
| N | SaaS | 🟢 PASSED | 2026-09-10 | E2E Gate N Validation (`test_gate_n_hito_o_e2e.py` - 16 escenarios, 2215 passed, 1 skipped, 0 failures, deploy_validate 5/5) |
| O | Production | ⚪ | | |
| P | Advanced Autonomy | 🟢 PASSED | 2026-09-17 | Gate P E2E (16 escenarios), 239 tests R.1–R.7, 84 transversales, 2831 passed / 18 skipped / 0 failures / 0 errors, deploy validation 5/5 |

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
| 2026-08-31 | Hito H (H.1–H.7) | Business Memory Complete (Persistencia Hexagonal JSON durable, inmutable y segura de Missions, Decisions, Actions, Results, Product Memory, Supplier Memory y Temporal State Snapshots con reconstrucción temporal y simulada de reinicio de servicios) | 650 passed, 1 skipped (35 específicos de Hito H + E2E integration) | E2E Business Memory Integration (`MISSION -> DECISION -> ACTION -> RESULT -> PRODUCT -> SUPPLIER -> TEMPORAL STATE` + Disk Recovery) | 🟢 VALIDADA | `tests/integration/test_hito_h_business_memory_e2e.py` |
| 2026-08-31 | I.1 | Outcome Tracking (Captura y persistencia Hexagonal JSON durable e inmutable de Outcomes observados en el negocio post-acción con trazabilidad causal completa `MISSION -> DECISION -> ACTION -> RESULT -> OUTCOME`, sanitización PII/credenciales e idempotencia estricta) | 657 passed, 1 skipped (7 específicos I.1) | Outcome Tracking Integration E2E (`tests/integration/test_i1_outcome_tracking_integration.py`) | 🟢 VALIDADA | `src/domain/outcome/`, `src/infrastructure/persistence/data/json/outcome_repository.py`, `src/application/outcome/outcome_service.py` |
| 2026-08-31 | I.2 | Prediction vs Actual (Registro de predicciones previo a outcomes, contraste determinista de métricas numéricas y cualitativas `MATCH/MISS/UNKNOWN`, cálculo de delta, desacoplamiento hexagonal JSON, preservación de temporalidad, provenance, confidence e idempotencia estricta) | 666 passed, 1 skipped (9 específicos I.2) | Prediction vs Actual Integration E2E (`tests/integration/test_i2_prediction_vs_actual_integration.py`) | 🟢 VALIDADA | `src/domain/prediction/`, `src/infrastructure/persistence/data/json/prediction_repository.py`, `src/application/prediction/prediction_comparison_service.py` |
| 2026-09-01 | J.1 | Scheduler (Capacidad programable y temporal desacoplada para iniciar misiones existentes mediante `Clock`/`DeterministicClock`, persistencia Hexagonal JSON durable, deduplicación e idempotencia estricta por ocurrencia, preservación de `UNKNOWN`, manejo seguro de fallos y soporte de restart/reload sin implementar lógica de negocio) | 736 passed, 1 skipped (27 específicos: 20 unit + 7 integ/E2E) | Scheduler Integration & E2E Scenarios A-F (`tests/integration/test_j1_scheduler_integration.py`) | 🟢 VALIDADA | `src/domain/scheduling/`, `src/application/scheduling/`, `src/infrastructure/persistence/data/json/schedule_repository.py`, `tests/unit/test_scheduler_service.py`, `tests/integration/test_j1_scheduler_integration.py` |
| 2026-09-04 | N.2 | Authentication (Modelos canónicos inmutables `AuthenticationMethod/Status/Request/Result/PrincipalContext`, `AuthenticationService` con validación de presencia/expiración/proveedor/binding de sujeto, resolución a Identidad N.1 estable reutilizando OAuth/MercadoLibre e IdentityService, estados `AUTHENTICATED/UNAUTHENTICATED/EXPIRED/INVALID/UNKNOWN/ERROR`, checksum SHA-256 determinista, cero secretos en results/logs/persistencia, actores internos con contrato explícito, auditoría/traza segura K.1/K.2 `AUTHENTICATION_EVALUATED`, frontera estricta sin autorización N.3/RBAC N.4/PolicyEngine) | 1593 passed, 1 skipped, 0 failures (26 específicos N.2: 16 unit + 10 integ/E2E; regresión relevante N.1/K.1/K.2/K.8/OAuth: 122 passed) | N.2 E2E Scenarios A-H (`tests/integration/test_n2_authentication_integration.py`) | 🟢 VALIDADA | `src/domain/authentication/`, `src/application/authentication/`, `src/domain/audit/models.py` (EXTEND `AUTHENTICATION_EVALUATED`), `tests/unit/test_n2_authentication_unit.py`, `tests/integration/test_n2_authentication_integration.py`, `N2_AUTHENTICATION_EXECUTION_REPORT.md` |
| 2026-09-07 | Gate M / Hito N | Validación E2E Formal y Cierre de Hito N (Transversal N — Security, Governance & Safety) demostrando la cadena ininterrumpida N.1–N.11 en 14 escenarios canónicos: Happy Path, Auth Deny, RBAC/Authz Deny, Tool Deny, Sensitive Data Redaction, Financial Limits, Valid Approval Exception, Missing/Expired Approval, Missing Secret, Emergency Stop Precedence, Compliant Enforcement, Non-compliant Forced Bypass Detection, Cross-correlation Evidence Isolation y Tampering/Corruption Fail-Safe. Cero llamadas físicas en bloqueos, cero leaks de secretos/PII y checksums SHA-256 | 1837 passed, 1 skipped, 0 failures (14 específicos Gate M, 169 unit N.1-N.11) | Gate M E2E Validation (`test_gate_m_hito_n_e2e.py` - 14 escenarios canónicos) | 🟢 VALIDADA | `tests/integration/test_gate_m_hito_n_e2e.py`, `GATE_M_HITO_N_VALIDATION_REPORT.md` |
| 2026-09-10 | Gate N / Hito O | Validación E2E Formal y Cierre de Hito O (SaaS / Platformization) con aislamiento físico multi-tenant O.1, organizaciones O.2, AuthN/AuthZ O.3/O.4, RBAC SaaS, Billing O.7, Monitoring O.9, Admin Console O.10 y Deployment Automation O.13 | 2215 passed, 1 skipped, 0 failures | Gate N E2E Validation (`test_gate_n_hito_o_e2e.py` - 16 escenarios) | 🟢 VALIDADA | `tests/integration/test_gate_n_hito_o_e2e.py`, `GATE_N_HITO_O_VALIDATION_REPORT.md` |
| 2026-09-12 | Gate O / Hito P | Validación E2E Formal y Cierre de Hito P (Production Readiness & Resilience) con CI/CD P.1, Environment Separation P.2, Database Migrations P.3 (PostgreSQL), Backups P.4, Disaster Recovery P.5, Health Checks P.6, Monitoring P.7, Log Retention P.8, Rate Limiting P.9, Capacity Planning P.10 | 2507 passed, 2 skipped, 0 failures | Gate O E2E Validation (`test_gate_o_hito_p_e2e.py` - 10 escenarios canónicos) | 🟢 VALIDADA | `tests/integration/test_gate_o_hito_p_e2e.py`, `GATE_O_HITO_P_VALIDATION_REPORT.md` |
| 2026-09-13 | Q.1 | Opportunity Dashboard (Business Intelligence / Visualización consultiva de oportunidades de mercado, ranking multi-criterio, filtros avanzados, vistas resumen/detalle, ordenación determinista, paginación, preservación de UNKNOWN e integración con O.1/O.4/O.10) | 18 passed (11 unit + 7 integ) | Opportunity Dashboard Integration & REST/HTML (`test_q1_opportunity_dashboard_integration.py`) | 🟢 VALIDADA | `tests/unit/test_q1_opportunity_dashboard_unit.py`, `tests/integration/test_q1_opportunity_dashboard_integration.py`, `Q1_OPPORTUNITY_DASHBOARD_EXECUTION_REPORT.md` |
| 2026-09-13 | Q.2 | Supplier Dashboard (Business Intelligence / Visualización consultiva y comparativa de proveedores validados, filtros multicriterio, enmascaramiento PII N.9, aislamiento multi-tenant O.1, RBAC O.4, Admin Console O.10, preservación de UNKNOWN != 0 y ordenación determinista) | 18 passed (10 unit + 8 integ) | Supplier Dashboard Integration & REST/HTML (`test_q2_supplier_dashboard_integration.py`) | 🟢 VALIDADA | `tests/unit/test_q2_supplier_dashboard_unit.py`, `tests/integration/test_q2_supplier_dashboard_integration.py`, `Q2_SUPPLIER_DASHBOARD_EXECUTION_REPORT.md` |
| 2026-09-13 | Q.3 | Profit Dashboard (Business Intelligence / Unit Economics, Margen y Rentabilidad, visualización consultiva de facts financieros reales, semántica honesta UNKNOWN != 0 / UNKNOWN != FREE, aritmética Decimal estricta, aislamiento multi-tenant O.1, RBAC O.4, Admin Console O.10, comparación de variantes y desglose explicable) | 26 passed (15 unit + 11 integ) | Profit Dashboard Integration & REST/HTML (`test_q3_profit_dashboard_integration.py`) | 🟢 VALIDADA | `tests/unit/test_q3_profit_dashboard_unit.py`, `tests/integration/test_q3_profit_dashboard_integration.py`, `Q3_PROFIT_DASHBOARD_EXECUTION_REPORT.md` |
| 2026-09-14 | R.1 | Multi-step Planning (Advanced Autonomy / Descomposición jerárquica de objetivos en sub-objetivos y steps canónicos, grafo acíclico dirigido DAG con detección determinista de ciclos, ordenación topológica reproducible con tie-breaking alfabético de Kahn, cálculo estricto de Step Readiness, validación de capacidades registradas, control estricto de budgets con preservación de UNKNOWN != 0, replanificación acotada de subgrafos afectados por fallos técnicos preservando pasos completados, regla Anti-Policy Bypass que bloquea replanes evasivos ante POLICY_DENIED / Emergency Stop N.11, persistencia hexagonal JSON durable multi-tenant con CrossTenantGuard y emisión de auditoría K.1 y trazas K.2 seguras sin Chain-of-Thought) | 2616 passed, 18 skipped, 0 failures (30 específicos R.1: 20 unit + 10 integ/E2E) | Multi-step Planning Integration & E2E Scenarios A-J (`tests/integration/test_r1_multi_step_planning_integration.py`) | 🟢 VALIDADA | `src/domain/planning/`, `src/application/planning/`, `src/infrastructure/persistence/data/json/execution_plan_repository.py`, `tests/unit/test_r1_multi_step_planning_unit.py`, `tests/integration/test_r1_multi_step_planning_integration.py`, `R1_MULTI_STEP_PLANNING_EXECUTION_REPORT.md` |
| 2026-09-17 | R.7 | Self-monitoring (Advanced Autonomy / Observación y evaluación de salud operacional en tiempo real, detección determinista de latidos vencidos, stall temporal con ClockPort, fallos técnicos, anomalías de coste en Decimal y paridad monetaria, y despacho acotado de remediaciones seguras R.1/R.5/R.6 con adapter K.1/K.2/P.8) | 2831 passed, 18 skipped, 0 failures (41 específicos R.7: 28 unit + 13 integ; regresión R.1-R.7 integrada en Gate P) | R.7 Integration Scenarios A-M (`tests/integration/test_r7_self_monitoring_integration.py`) | 🟢 VALIDADA | `src/domain/self_monitoring/`, `src/application/self_monitoring/`, `tests/unit/test_r7_self_monitoring_unit.py`, `tests/integration/test_r7_self_monitoring_integration.py`, `R7_SELF_MONITORING_EXECUTION_REPORT.md` |
| 2026-09-17 | Gate P / Hito R | Validación E2E formal y cierre de Hito R (Advanced Autonomy) integrando R.1 Multi-step Planning, R.2 Sub-missions, R.3 Specialist Agents, R.4 Agent Coordination, R.5 Dynamic Delegation, R.6 Long-running Missions y R.7 Self-monitoring, con aislamiento multi-tenant, Zero-CoT, Anti-Policy Bypass y precedencia de Emergency Stop | 2831 passed, 18 skipped, 0 failures, 0 errors (16 Gate P, 239 R.1–R.7, 84 transversales) | Gate P E2E (`test_gate_p_hito_r_advanced_autonomy_e2e.py` - 16 escenarios canónicos) + deploy validation 5/5 | 🟢 VALIDADA | `tests/integration/test_gate_p_hito_r_advanced_autonomy_e2e.py`, `GATE_P_HITO_R_ADVANCED_AUTONOMY_VALIDATION_REPORT.md` |
| 2026-09-15 | R.3 | Specialist Agents (Advanced Autonomy / modelo formal de agentes y capabilities, selección determinista capability-first, contratos I/O, disponibilidad y coste fail-safe, allowlists, ejecución protegida, idempotencia concurrente tenant-scoped, sanitización y auditoría/traza, integración R.1/R.2) | 2701 passed, 2 skipped, 0 failures (36 específicos R.3; 63 de regresión R.1/R.2) | Specialist Agents Integration Scenarios A-J con mocks/fakes, sin side effects externos | 🟢 VALIDADA | `src/domain/specialist_agent/`, `src/application/specialist_agent/`, `tests/unit/test_r3_specialist_agents_unit.py`, `tests/integration/test_r3_specialist_agents_integration.py`, `R3_SPECIALIST_AGENTS_EXECUTION_REPORT.md` |


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
- **Hito H — Business Memory (🟢 VALIDADA / GATE G PASSED - H.1 a H.7 + Business Memory E2E Integration)**
- **Hito I.1 — Outcome Tracking (🟢 VALIDADA)**
- **Hito I.2 — Prediction vs Actual (🟢 VALIDADA)**
- **Hito N — Transversal N: Security, Governance & Safety (🟢 VALIDADA / GATE M PASSED - N.1 a N.11 + Gate M E2E Validation)**
- **Hito O — SaaS / Platformization (🟢 VALIDADA / GATE N PASSED - O.1 a O.13 + Gate N E2E Validation)**
- **Hito P — Production Readiness & Resilience (🟢 VALIDADA / GATE O PASSED - P.1 a P.10 + Gate O E2E Validation)**
- **Hito Q — Business Intelligence (🟢 VALIDADA / GATE P PASSED - Q.1 a Q.6 + Gate P E2E Validation)**
- **Hito R.1 — Multi-step Planning (🟢 VALIDADA)**
- **Hito R.2 — Sub-missions (🟢 VALIDADA)**
- **Hito R.3 — Specialist Agents (🟢 VALIDADA)**
- **Hito R.4 — Agent Coordination (🟢 VALIDADA)**
- **Hito R.5 — Dynamic Delegation (🟢 VALIDADA)**
- **Hito R.6 — Long-running Missions (🟢 VALIDADA)**
- **Hito R.7 — Self-monitoring (🟢 VALIDADA)**
- **Hito R — Advanced Autonomy (🟢 VALIDADA / GATE P PASSED - R.1 a R.7 + 16 escenarios Gate P E2E)**

**Fases en progreso activo:**
- Ninguna iniciada tras el cierre de Hito R.

**Próxima acción:**
- **Hito S — Self-Improving Commerce** permanece ⚪ PENDIENTE. NO ejecutar sin autorización explícita.

---

# 28. North Star

El proyecto termina cuando puede ejecutar:

`DESCUBRIR → INVESTIGAR → EVALUAR → ENCONTRAR ABASTECIMIENTO → CALCULAR ECONOMÍA Y RIESGO → DECIDIR → EJECUTAR ACCIONES AUTORIZADAS → COMUNICAR → OBSERVAR → APRENDER → VOLVER A DECIDIR`

de forma autónoma, explicable, segura, auditable y económicamente viable.

La Gantt debe reflejar el estado REAL del sistema en todo momento.
