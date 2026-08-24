import json
from pathlib import Path

from mcp.server import MCPServer
from src.application.use_cases.analyze_profit import AnalyzeProfitUseCase
from src.application.use_cases.discover_market_opportunities import DiscoverMarketOpportunitiesUseCase
from src.domain.market_intelligence.models import SearchCriteria, Marketplace
from src.domain.market_intelligence.services import MarketAnalysisService
from src.infrastructure.market_intelligence.mercadolibre.client import MercadoLibreClient
from src.infrastructure.market_intelligence.mercadolibre.adapter import MercadoLibreAdapter
from src.infrastructure.persistence.data.json.profit_repository import JsonProfitDataRepository
from src.infrastructure.persistence.data.json.market_snapshot_repository import JsonMarketSnapshotRepository
from src.domain.profit.models import ProfitAnalysis, FinancialData, DecisionRules

mcp = MCPServer("commerce_lab")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "experiments"
SUPPLIER_DIR = Path(__file__).resolve().parents[2] / "data" / "suppliers"
SNAPSHOTS_DIR = Path(__file__).resolve().parents[2] / "data" / "market_snapshots"

# Dependency Injection Manual
profit_repository = JsonProfitDataRepository(DATA_DIR)
analyze_profit_use_case = AnalyzeProfitUseCase(profit_repository)

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


@mcp.tool()
def calculate_profit() -> str:
    """Calcula la utilidad, margen y decisión comercial del experimento."""
    experiment_id = "EXP-001"
    
    financial_data = profit_repository.get_financial_data(experiment_id)
    decision_rules = profit_repository.get_decision_rules(experiment_id)
    
    analysis = analyze_profit_use_case.execute(experiment_id)
    
    return format_profit_analysis_report(experiment_id, financial_data, decision_rules, analysis)


def format_profit_analysis_report(experiment_id: str, data: FinancialData, rules: DecisionRules, analysis: ProfitAnalysis) -> str:
    """Transforma los modelos de dominio al formato de reporte original."""
    return f"""
PROFIT ENGINE — {experiment_id}

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
def discover_products(
    query: str,
    marketplace: str = "MERCADO_LIBRE",
    category: str | None = None,
    limit: int | None = 20
) -> str:
    """Busca y descubre oportunidades de mercado basadas en una intención de búsqueda."""
    
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