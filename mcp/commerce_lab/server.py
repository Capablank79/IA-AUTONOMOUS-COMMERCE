import json
from pathlib import Path

from mcp.server import MCPServer
from src.application.use_cases.analyze_profit import AnalyzeProfitUseCase
from src.application.use_cases.discover_market_opportunities import DiscoverMarketOpportunitiesUseCase
from src.domain.market_intelligence.models import SearchCriteria, Marketplace
from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionResult
from src.domain.market_intelligence.services import MarketAnalysisService
from src.domain.market_intelligence.ports import MarketplaceDataSource, ProductCatalogDataSource
from src.domain.profit.engine import ProfitEngine
from src.domain.opportunity.engine import OpportunityEngine
from src.application.mission.orchestrator import BasicMissionOrchestrator
from src.infrastructure.mission.repository import InMemoryMissionRepository
from src.application.market_intelligence.traffic_intelligence_service import TrafficIntelligenceService
from src.application.market_intelligence.product_family_intelligence_service import ProductFamilyIntelligenceService
from src.application.market_intelligence.review_intelligence_service import ReviewIntelligenceService
from src.application.market_intelligence.trend_intelligence_service import TrendIntelligenceService
from src.application.market_intelligence.catalog_listing_bridge_service import CatalogListingBridgeService
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.marketplace_data_source import MercadoLibreMarketplaceDataSource
from src.infrastructure.mercadolibre.product_catalog_data_source import MercadoLibreProductCatalogDataSource
from src.application.oauth.dependencies import oauth_service, product_hunter_service
from src.infrastructure.market_intelligence.mercadolibre.client import MercadoLibreClient
from src.infrastructure.market_intelligence.mercadolibre.adapter import MercadoLibreAdapter
from src.infrastructure.persistence.data.json.profit_repository import JsonProfitDataRepository
from src.infrastructure.persistence.data.json.market_snapshot_repository import JsonMarketSnapshotRepository
from src.infrastructure.suppliers.json_supplier_data_source import JsonSupplierDataSource
from src.domain.profit.models import ProfitAnalysis, FinancialData, DecisionRules

mcp = MCPServer("commerce_lab")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "experiments"
SUPPLIER_DIR = Path(__file__).resolve().parents[2] / "data" / "suppliers"
SNAPSHOTS_DIR = Path(__file__).resolve().parents[2] / "data" / "market_snapshots"

# Dependency Injection Manual
profit_repository = JsonProfitDataRepository(DATA_DIR)
supplier_data_source = JsonSupplierDataSource(SUPPLIER_DIR)
analyze_profit_use_case = AnalyzeProfitUseCase(profit_repository, supplier_data_source)

# New Services for MCP Bridge
mission_repository = InMemoryMissionRepository()
profit_engine = ProfitEngine()
opportunity_engine = OpportunityEngine()
traffic_intelligence_service = TrafficIntelligenceService(oauth_service=oauth_service)
review_intelligence_service = ReviewIntelligenceService(oauth_service=oauth_service)
trend_intelligence_service = TrendIntelligenceService(oauth_service=oauth_service)
catalog_listing_bridge_service = CatalogListingBridgeService(oauth_service=oauth_service)

class UserScopedMarketplaceDataSource(MarketplaceDataSource):
    """Bridge adapter to provide authenticated MarketplaceDataSource for a specific user."""
    def __init__(self, oauth_service, user_id):
        self.oauth_service = oauth_service
        self.user_id = user_id

    def fetch_snapshot(self, criteria: SearchCriteria):
        connection = self.oauth_service.get_valid_connection("mercadolibre", self.user_id)
        api_client = MercadoLibreApiClient(connection.access_token)
        return MercadoLibreMarketplaceDataSource(api_client).fetch_snapshot(criteria)

class UserScopedProductCatalogDataSource(ProductCatalogDataSource):
    """Bridge adapter to provide authenticated ProductCatalogDataSource for a specific user."""
    def __init__(self, oauth_service, user_id):
        self.oauth_service = oauth_service
        self.user_id = user_id

    def search_products(self, query: str, marketplace: Marketplace, limit: int | None = None):
        connection = self.oauth_service.get_valid_connection("mercadolibre", self.user_id)
        api_client = MercadoLibreApiClient(connection.access_token)
        return MercadoLibreProductCatalogDataSource(api_client).search_products(query, marketplace, limit)

    def get_product(self, product_id: str):
        connection = self.oauth_service.get_valid_connection("mercadolibre", self.user_id)
        api_client = MercadoLibreApiClient(connection.access_token)
        return MercadoLibreProductCatalogDataSource(api_client).get_product(product_id)

    def get_product_items(self, product_id: str):
        connection = self.oauth_service.get_valid_connection("mercadolibre", self.user_id)
        api_client = MercadoLibreApiClient(connection.access_token)
        return MercadoLibreProductCatalogDataSource(api_client).get_product_items(product_id)

ml_client = MercadoLibreClient()
ml_adapter = MercadoLibreAdapter(ml_client)
market_snapshot_repository = JsonMarketSnapshotRepository(SNAPSHOTS_DIR)
market_analysis_service = MarketAnalysisService()
discover_products_use_case = DiscoverMarketOpportunitiesUseCase(
    data_source=ml_adapter,
    repository=market_snapshot_repository,
    analysis_service=market_analysis_service
)


def load_experiment(experiment_file: str) -> dict:
    """Carga un experimento desde el directorio de datos."""

    file_path = DATA_DIR / experiment_file

    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def load_supplier(supplier_id: str) -> dict:
    """Carga un proveedor desde el directorio de datos."""

    numeric_id = supplier_id.split("-")[-1]
    file_path = SUPPLIER_DIR / f"supplier_{numeric_id}.json"

    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


@mcp.tool()
def get_project_status() -> str:
    """Devuelve el estado actual del laboratorio AI Autonomous Commerce."""

    return """
AI AUTONOMOUS COMMERCE LAB

Fase: MVP-0

Product Hunter: INICIADO
Supplier Hunter: INICIADO
Profit Engine: INICIADO
Mercado Libre: PENDIENTE
Email Agent: PENDIENTE
Listing Agent: PENDIENTE
Fulfillment Agent: PENDIENTE

Estado general: LABORATORIO EN CONSTRUCCIÓN
"""


@mcp.tool()
def get_experiment_status() -> str:
    """Devuelve el estado del experimento SSD SATA 480GB."""

    data = load_experiment("ssd_sata_480.json")

    product = data["product"]
    market = data["market"]
    suppliers = data["suppliers"]
    supply = data["supply"]
    publication = data["publication"]

    return f"""
EXPERIMENTO {data["experiment_id"]} — {product["name"]}

Producto: {product["brand"]} {product["model"]} {product["sku"]}

DEMANDA: {market["demand_status"].upper()}
Señal: {market["demand_signal"]}
Ventas visibles: +{market["visible_sales"]:,}
Precio de mercado observado: ${market["market_price_clp"]:,}
Precio mínimo observado: ${market["minimum_price_clp"]:,}

PROVEEDORES:
Candidatos encontrados: {suppliers["candidates"]}
Prioridad A: {suppliers["priority_a"]}
Cotizaciones comerciales: {suppliers["commercial_quotes"]}

ABASTECIMIENTO: {supply["status"].upper()}

Stock confirmado: {supply["stock_confirmed"]}
Precio mayorista confirmado: {supply["wholesale_price_confirmed"]}
Despacho confirmado: {supply["shipping_confirmed"]}
Garantía confirmada: {supply["warranty_confirmed"]}
Método de orden confirmado: {supply["ordering_method_confirmed"]}

PUBLICACIÓN: {publication["status"].upper()}

Motivo:
{publication["reason"]}

Estado: {data["experiment_status"]}
"""


from src.application.mappers.supplier_financial_mapper import SupplierFinancialMapper

@mcp.tool()
def calculate_profit() -> str:
    """Calcula la utilidad, margen y decisión comercial del experimento iterando sobre los candidatos."""
    experiment_id = "EXP-001"

    experiment_data = load_experiment("ssd_sata_480.json")
    supplier_candidates = experiment_data.get("supplier_candidates", [])
    sku = experiment_data.get("product", {}).get("sku")

    base_financial_data = profit_repository.get_financial_data(experiment_id)
    decision_rules = profit_repository.get_decision_rules(experiment_id)

    reports = []

    for supplier_id in supplier_candidates:
        try:
            analysis = analyze_profit_use_case.execute(experiment_id, supplier_id, sku)
            evidence = supplier_data_source.get_supplier_evidence(supplier_id, sku)
            if evidence:
                report_data = SupplierFinancialMapper.map_evidence_to_financial_data(base_financial_data, evidence)
            else:
                report_data = base_financial_data
            reports.append(format_profit_analysis_report(experiment_id, report_data, decision_rules, analysis, supplier_id))
        except ValueError as e:
            reports.append(f"PROFIT ENGINE — {experiment_id} — {supplier_id}\nError: {e}")

    if not reports:
        analysis = analyze_profit_use_case.execute(experiment_id)
        reports.append(format_profit_analysis_report(experiment_id, base_financial_data, decision_rules, analysis))

    return "\n\n".join(reports)


def format_profit_analysis_report(experiment_id: str, data: FinancialData, rules: DecisionRules, analysis: ProfitAnalysis, supplier_id: str | None = None) -> str:
    """Transforma los modelos de dominio al formato de reporte original."""
    title = f"PROFIT ENGINE — {experiment_id}"
    if supplier_id:
        title += f" — {supplier_id}"

    return f"""
{title}

Precio de venta: ${data.price.amount:,.0f}
Costo proveedor: ${data.supplier_price.amount:,.0f}

Comisión marketplace ({data.commission_pct}%): ${analysis.commission.amount:,.0f}
Despacho: ${data.shipping.amount:,.0f}
Otros costos: ${data.other_costs.amount:,.0f}

UTILIDAD NETA: ${analysis.net_profit.amount:,.0f}
MARGEN NETO: {analysis.net_margin_pct:.2f}%

DEMANDA:
Ventas visibles: {data.visible_sales:,}
Mínimo requerido: {rules.minimum_sales:,}
Demanda suficiente: {analysis.market_demand_ok}

REGLAS:
Margen mínimo: {rules.minimum_margin_pct:.2f}%
Margen excelente: {rules.excellent_margin_pct:.2f}%

DECISIÓN: {analysis.decision.value}
"""

@mcp.tool()
def get_supplier(supplier_id: str) -> str:
    """Devuelve la información comercial y de verificación de un proveedor."""

    supplier = load_supplier(supplier_id)

    company = supplier["company"]
    product = supplier["product"]
    pricing = supplier["pricing"]
    stock = supplier["stock"]
    logistics = supplier["logistics"]
    warranty = supplier["warranty"]
    verification = supplier["verification"]

    return f"""
SUPPLIER PROFILE — {supplier["supplier_id"]}

EMPRESA
Nombre: {company["name"]}
País: {company["country"]}
Tipo: {company["business_type"]}
Web: {company["website"] or "No registrada"}

PRODUCTO
Categoría: {product["category"]}
Marca: {product["brand"]}
Modelo: {product["model"]}
SKU: {product["sku"]}
Capacidad: {product["capacity_gb"]} GB

PRECIO
Precio mayorista: ${pricing["wholesale_price_clp"]:,} {pricing["currency"]}
Vigencia: {pricing["price_valid_until"] or "No confirmada"}

STOCK
Disponible: {stock["available"]}
Cantidad: {stock["quantity"]}
Última comprobación: {stock["last_checked"] or "No comprobado"}

LOGÍSTICA
Despacha desde: {logistics["ships_from"]}
Costo despacho: {logistics["shipping_cost_clp"] or "No confirmado"}
Tiempo entrega: {logistics["delivery_time_days"] or "No confirmado"}

GARANTÍA
Disponible: {warranty["available"]}
Periodo: {warranty["period_months"] or "No confirmado"} meses
RMA: {warranty["rma_available"]}

VERIFICACIÓN
Estado: {verification["status"]}
Puntuación: {verification["verification_score"]}

ESTADO PROVEEDOR: {supplier["supplier_status"]}
"""


@mcp.tool()
def submit_discovery_mission(query: str, user_id: str, marketplace: str = "MERCADO_LIBRE", limit: int = 10) -> str:
    """Inicia MARKET_DISCOVERY mediante BasicMissionOrchestrator."""
    try:
        mission = Mission.create(
            MissionType.MARKET_DISCOVERY,
            {"query": query, "user_id": user_id, "marketplace": marketplace, "limit": limit}
        )

        # Instantiate a dynamic data source for this mission
        user_data_source = UserScopedMarketplaceDataSource(oauth_service, user_id)

        orchestrator = BasicMissionOrchestrator(
            repository=mission_repository,
            product_hunter=product_hunter_service,
            market_data_source=user_data_source,
            traffic_intelligence=traffic_intelligence_service,
            supplier_source=supplier_data_source,
            profit_repository=profit_repository,
            profit_engine=profit_engine,
            opportunity_engine=opportunity_engine
        )

        orchestrator.submit(mission)
        return f"Misión iniciada exitosamente. ID: {mission.mission_id} | Estado: {mission.status}"
    except Exception as e:
        return f"Error al iniciar la misión: {e}"


@mcp.tool()
def get_mission_status(mission_id: str) -> str:
    """Recupera MissionResult, status, trace, evidences y blocks."""
    mission = mission_repository.get_by_id(mission_id)
    if not mission:
        return f"Misión no encontrada: {mission_id}"

    result = mission_repository.get_result(mission_id)

    report = [
        f"ESTADO DE MISIÓN — {mission_id}",
        f"Tipo: {mission.type}",
        f"Estado Actual: {mission.status}",
        f"Creada: {mission.created_at}",
        f"Actualizada: {mission.updated_at}",
        "-" * 30
    ]

    if result:
        report.append(f"Estado Final: {result.status}")
        report.append(f"Finalizada: {result.finished_at}")

        if result.blocks:
            report.append("\nBLOQUEOS DETECTADOS:")
            for block in result.blocks:
                report.append(f"- {block.get('step')}: {block.get('reason')}")

        if result.errors:
            report.append("\nERRORES:")
            for error in result.errors:
                report.append(f"- {error}")

        if result.trace:
            report.append("\nTRAZA DE EJECUCIÓN:")
            for entry in result.trace:
                # Truncate metadata for readability
                meta = str(entry.metadata)[:100] + "..." if len(str(entry.metadata)) > 100 else str(entry.metadata)
                report.append(f"[{entry.timestamp.strftime('%H:%M:%S')}] {entry.step}: {entry.status} | {meta}")

        if result.output:
            report.append("\nRESULTADOS:")
            # Show summary of results instead of full JSON if it's too large
            res = result.output.get("results", [])
            report.append(f"Items procesados: {len(res)}")
            for item in res[:5]:  # Show first 5
                report.append(f"- {item.get('title')} | Readiness: {item.get('readiness')} | Profit: {item.get('profit_decision')}")
            if len(res) > 5:
                report.append(f"... y {len(res) - 5} más.")

    return "\n".join(report)


@mcp.tool()
def get_item_evidence(item_id: str, user_id: str) -> str:
    """Utiliza TrafficIntelligenceService existente para obtener VisitSignal real."""
    try:
        signal = traffic_intelligence_service.get_visits(user_id=user_id, item_id=item_id, window_days=30)
        return f"""
ITEM EVIDENCE — {item_id}
Total Visitas: {signal.total_visits}
Días Observados: {signal.observed_days}
Ratio de Cobertura: {signal.coverage_ratio:.2f}
Promedio Diario: {signal.daily_average:.2f}
Fuente: {signal.source}
Fecha Observación: {signal.observed_at}
"""
    except Exception as e:
        return f"Error al obtener evidencia del item: {e}"


@mcp.tool()
def get_review_intelligence(item_id: str, user_id: str, offset: int = 0, limit: int = 50) -> str:
    """
    Obtiene y estructura reviews de un item de Mercado Libre (SOCIAL-01).
    Preserva reviewable_object, secondary_key, rating y texto.
    """
    try:
        signal = review_intelligence_service.get_reviews(user_id=user_id, item_id=item_id, offset=offset, limit=limit)

        report = [
            f"REVIEW INTELLIGENCE — {item_id}",
            f"Rating Promedio: {signal.average_rating:.2f}",
            f"Total Reviews: {signal.total_reviews}",
            f"Recuperadas: {len(signal.reviews)} (offset: {offset}, limit: {limit})",
            f"Paginación: {signal.paging}",
            f"Fecha Observación: {signal.observed_at}",
            "-" * 30,
            "REVIEWS (Muestra):"
        ]

        for r in signal.reviews[:5]:
            report.append(f"- ID: {r.external_id} | Rating: {r.rating} | Fecha: {r.date.strftime('%Y-%m-%d')}")
            report.append(f"  Object: {r.reviewable_object} | Secondary: {r.secondary_key}")
            content = (r.text[:100] + "...") if len(r.text) > 100 else r.text
            report.append(f"  Texto: {content}")
            report.append("")

        if len(signal.reviews) > 5:
            report.append(f"... y {len(signal.reviews) - 5} más.")

        report.append("-" * 30)
        report.append("DATA CATEGORIZATION:")
        report.append(f"OBSERVED: API data (ratings, text, keys, paging)")
        report.append(f"DERIVED: averages, counts, mappings")
        report.append(f"ESTIMATED: none in this phase")

        return "\n".join(report)
    except Exception as e:
        return f"Error al obtener inteligencia de reviews: {e}"

@mcp.tool()
def get_catalog_listing_bridge(catalog_product_id: str, user_id: str) -> str:
    """
    Obtiene los listings/items asociados a un catalog_product_id (OPEN-DISCOVERY-02).
    Permite el flujo: catalog_product_id -> item_ids -> visitas/reviews.
    """
    try:
        bridge = catalog_listing_bridge_service.get_product_items(user_id=user_id, catalog_product_id=catalog_product_id)

        report = [
            f"CATALOG -> LISTING BRIDGE — {catalog_product_id}",
            f"Listings encontrados: {len(bridge.item_ids)}",
            f"Fecha Observación: {bridge.observed_at.strftime('%Y-%m-%d %H:%M:%S')}",
            "-" * 30,
            "ITEM IDS DETECTADOS:",
            ", ".join(bridge.item_ids) if bridge.item_ids else "Ninguno",
            "-" * 30,
            "DATA CATEGORIZATION:",
            f"OBSERVED: listings associated with catalog product via /products/{{id}}/items",
            f"DERIVED: mapping between catalog and items",
            f"ESTIMATED: none"
        ]

        return "\n".join(report)
    except Exception as e:
        return f"Error al obtener el bridge de listings: {e}"

@mcp.tool()
def get_market_trends(user_id: str) -> str:
    """
    Obtiene las tendencias actuales de búsqueda en Mercado Libre (OPEN-DISCOVERY-01).
    Devuelve una lista de palabras clave (keywords) con su ranking.
    """
    try:
        from datetime import datetime
        trends = trend_intelligence_service.get_trends(user_id=user_id)

        report = [
            f"MARKET TRENDS — Mercado Libre Chile (MLC)",
            f"Fecha Observación: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "-" * 30,
            "TENDENCIAS DETECTADAS:"
        ]

        for t in trends:
            report.append(f"- Rank {t['rank']}: {t['keyword']}")

        report.append("-" * 30)
        report.append("DATA CATEGORIZATION:")
        report.append("OBSERVED: API trend keywords and rankings")
        report.append("DERIVED: none")
        report.append("ESTIMATED: none")

        return "\n".join(report)
    except Exception as e:
        return f"Error al obtener tendencias: {e}"

@mcp.tool()
def get_product_family_intelligence(product_id: str, user_id: str) -> str:
    """
    Obtiene inteligencia completa de la familia de un producto (CATALOG-01).
    Navega Producto -> Catalog Product -> variantes -> Listings/Items context.
    """
    try:
        catalog_source = UserScopedProductCatalogDataSource(oauth_service, user_id)
        service = ProductFamilyIntelligenceService(catalog_source)
        intel = service.get_family_intelligence(product_id)

        report = [
            f"PRODUCT FAMILY INTELLIGENCE — {product_id}",
            f"Título: {intel.main_product.title}",
            f"Marca: {intel.main_product.brand} | Modelo: {intel.main_product.model}",
            f"Estado: {intel.main_product.status}",
            f"Domain: {intel.main_product.domain_id}",
            "-" * 30
        ]

        if intel.parent_product:
            report.append(f"PRODUCTO PADRE: {intel.parent_product.product_id} ({intel.parent_product.title})")

        if intel.siblings:
            report.append(f"HERMANOS / VARIACIONES ({len(intel.siblings)}):")
            for sib in intel.siblings[:5]:
                report.append(f"- {sib.product_id}: {sib.title}")
            if len(intel.siblings) > 5:
                report.append(f"... y {len(intel.siblings) - 5} más.")

        if intel.variants:
            report.append(f"VARIANTES EN PICKERS ({len(intel.variants)}):")
            for var in intel.variants[:5]:
                report.append(f"- {var.product_id}: {var.picker_label}")
            if len(intel.variants) > 5:
                report.append(f"... y {len(intel.variants) - 5} más.")

        if intel.main_product.buy_box_winner:
            winner = intel.main_product.buy_box_winner
            report.append("-" * 30)
            report.append("GANADOR DE BUY BOX (Listing Principal):")
            report.append(f"Item ID: {winner.external_id}")
            report.append(f"Precio: ${winner.price.amount:,.0f} {winner.price.currency}")
            report.append(f"Vendedor ID: {winner.seller_id}")
            report.append(f"Condición: {winner.condition}")
            report.append(f"Logística: {winner.shipping_info.get('logistic_type', 'N/A')}")

        report.append("-" * 30)
        report.append("IDS DE CATÁLOGO RELACIONADOS (para encadenar visitas/reviews/vendedor):")
        report.append(", ".join(intel.related_catalog_ids))

        return "\n".join(report)
    except Exception as e:
        return f"Error al obtener inteligencia de familia de producto: {e}"


@mcp.tool()
def discover_products(
    query: str,
    user_id: str | None = None,
    marketplace: str = "MERCADO_LIBRE",
    category: str | None = None,
    limit: int | None = 20
) -> str:
    """Busca y descubre oportunidades de mercado basadas en una intención de búsqueda. Cumple LIVE-02 (/products/search)."""

    try:
        mp = Marketplace(marketplace.upper())
    except ValueError:
        return f"Error: Marketplace no soportado: {marketplace}"

    criteria = SearchCriteria(
        query=query,
        marketplace=mp,
        category=category,
        limit=limit
    )

    try:
        # Si no hay user_id, intentamos usar el legado (que usa /sites/MLC/search)
        # Pero si hay user_id, usamos el nuevo datasource que cumple LIVE-02 (/products/search)
        if user_id:
            user_data_source = UserScopedMarketplaceDataSource(oauth_service, user_id)
            use_case = DiscoverMarketOpportunitiesUseCase(
                data_source=user_data_source,
                repository=market_snapshot_repository,
                analysis_service=market_analysis_service
            )
            opportunities = use_case.execute(criteria)
        else:
            # Fallback al legado para mantener compatibilidad si no hay token
            opportunities = discover_products_use_case.execute(criteria)

        if not opportunities:
            return f"No se encontraron oportunidades para la búsqueda: '{query}'"

        result = [
            f"PRODUCT HUNTER RESULTS: '{query}' (Marketplace: {mp.value})",
            f"Oportunidades encontradas: {len(opportunities)}",
            "-" * 50
        ]

        for idx, opp in enumerate(opportunities, 1):
            result.append(f"#{idx} [Score: {opp.opportunity_score}]")
            result.append(f"Title: {opp.listing.title}")
            result.append(f"ID: {opp.listing.external_id} | Seller: {opp.listing.seller_id}")
            result.append(f"Price: ${opp.listing.price.amount:,.0f} {opp.listing.price.currency}")
            result.append(f"Sold: {opp.listing.sold_quantity} | Available: {opp.listing.available_quantity}")
            result.append(f"Demand Signal: {opp.demand_signal.label} (Score: {opp.demand_signal.score})")
            result.append(f"Price Signal: {opp.price_signal.position} (Ratio: {opp.price_signal.ratio:.2f})")
            result.append(f"Snapshot ID: {opp.snapshot_id}")
            result.append("-" * 50)

        return "\n".join(result)
    except Exception as e:
        return f"Error al ejecutar Product Hunter: {e}"


if __name__ == "__main__":
    mcp.run()
