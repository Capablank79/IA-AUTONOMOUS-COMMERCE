import json
from pathlib import Path

from mcp.server import MCPServer


mcp = MCPServer("commerce_lab")


DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "experiments"
SUPPLIER_DIR = Path(__file__).resolve().parents[2] / "data" / "suppliers"


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

    data = load_experiment("ssd_sata_480.json")

    price = data["market"]["market_price_clp"]
    commission_pct = data["market"]["marketplace_commission_pct"]
    shipping = data["market"]["shipping_cost_clp"]
    other_costs = data["market"]["other_costs_clp"]

    supplier_price = data["suppliers"]["test_supplier_price_clp"]

    rules = data["decision_rules"]

    minimum_margin = rules["minimum_net_margin_pct"]
    excellent_margin = rules["excellent_net_margin_pct"]
    minimum_sales = rules["minimum_visible_sales"]

    market_demand_ok = (
        data["market"]["visible_sales"] >= minimum_sales
    )

    commission = price * (commission_pct / 100)

    net_profit = (
        price
        - commission
        - supplier_price
        - shipping
        - other_costs
    )

    net_margin = (net_profit / price) * 100

    if net_margin >= excellent_margin and market_demand_ok:
        decision = "STRONG_BUY"
    elif net_margin >= minimum_margin and market_demand_ok:
        decision = "BUY"
    else:
        decision = "REJECT"

    return f"""
PROFIT ENGINE — {data["experiment_id"]}

Precio de venta: ${price:,.0f}
Costo proveedor: ${supplier_price:,.0f}

Comisión marketplace ({commission_pct}%): ${commission:,.0f}
Despacho: ${shipping:,.0f}
Otros costos: ${other_costs:,.0f}

UTILIDAD NETA: ${net_profit:,.0f}
MARGEN NETO: {net_margin:.2f}%

DEMANDA:
Ventas visibles: {data["market"]["visible_sales"]:,}
Mínimo requerido: {minimum_sales:,}
Demanda suficiente: {market_demand_ok}

REGLAS:
Margen mínimo: {minimum_margin:.2f}%
Margen excelente: {excellent_margin:.2f}%

DECISIÓN: {decision}
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


if __name__ == "__main__":
    mcp.run()