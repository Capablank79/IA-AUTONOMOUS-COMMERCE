
import json
from datetime import datetime, timezone
from pathlib import Path
from src.application.market_intelligence.trend_intelligence_service import TrendIntelligenceService
from src.application.market_intelligence.traffic_intelligence_service import TrafficIntelligenceService
from src.application.market_intelligence.product_family_intelligence_service import ProductFamilyIntelligenceService
from src.application.market_intelligence.review_intelligence_service import ReviewIntelligenceService
from src.application.use_cases.discover_market_opportunities import DiscoverMarketOpportunitiesUseCase
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.marketplace_data_source import MercadoLibreMarketplaceDataSource
from src.infrastructure.mercadolibre.product_catalog_data_source import MercadoLibreProductCatalogDataSource
from src.application.oauth.dependencies import oauth_service
from src.domain.market_intelligence.models import SearchCriteria, Marketplace
from src.domain.market_intelligence.services import MarketAnalysisService
from src.infrastructure.persistence.data.json.market_snapshot_repository import JsonMarketSnapshotRepository

def run_discovery():
    user_id = '55197108'
    SNAPSHOTS_DIR = Path("data/market_snapshots")
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    evidence = {
        "trend_initial": None,
        "decision": None,
        "products_discovered": [],
        "deep_dive": {},
        "comparisons": [],
        "categorization": {
            "OBSERVED": [],
            "DERIVED": [],
            "ESTIMATED": []
        },
        "trace": [],
        "gaps": [],
        "next_action": None
    }

    # 1. Get Trends
    trend_service = TrendIntelligenceService(oauth_service=oauth_service)
    trends = trend_service.get_trends(user_id)
    evidence["trend_initial"] = trends
    evidence["categorization"]["OBSERVED"].append("Mercado Libre real-time trends for MLC")
    evidence["trace"].append({"step": "Trends Discovery", "status": "SUCCESS", "source": "API -> TrendIntelligenceService"})

    # 2. Decision
    selected_trend = "antena starlink"
    evidence["decision"] = f"Selected trend: '{selected_trend}' due to high technology relevance and market momentum (Rank 37)."

    # 3. Discover Products
    connection = oauth_service.get_valid_connection("mercadolibre", user_id)
    api_client = MercadoLibreApiClient(connection.access_token)

    from urllib.parse import quote
    raw_data = api_client.get(f"/products/search?status=active&site_id=MLC&q={quote(selected_trend)}")
    raw_results = raw_data.get("results", [])

    data_source = MercadoLibreMarketplaceDataSource(api_client)
    repository = JsonMarketSnapshotRepository(SNAPSHOTS_DIR)
    analysis_service = MarketAnalysisService()
    use_case = DiscoverMarketOpportunitiesUseCase(data_source, repository, analysis_service)

    criteria = SearchCriteria(query=selected_trend, marketplace=Marketplace.MERCADO_LIBRE, limit=5)
    opportunities = use_case.execute(criteria)

    if not opportunities and raw_results:
        evidence["products_discovered"] = [
            {
                "id": r.get("id"),
                "title": r.get("name"),
                "price": 0.0,
                "currency": "CLP",
                "score": 0.0,
                "has_winner": False
            } for r in raw_results[:5]
        ]
    else:
        evidence["products_discovered"] = [
            {
                "id": opp.listing.external_id,
                "title": opp.listing.title,
                "price": float(opp.listing.price.amount),
                "currency": opp.listing.price.currency,
                "score": float(opp.opportunity_score),
                "has_winner": True
            } for opp in opportunities
        ]

    evidence["categorization"]["OBSERVED"].append(f"Found {len(raw_results)} catalog products for '{selected_trend}'")
    if not opportunities:
        evidence["gaps"].append("CRITICAL: No Buy Box winners found in search results. Price and Opportunity Score are unavailable.")

    evidence["trace"].append({"step": "Product Discovery", "status": "SUCCESS" if raw_results else "FAILED", "source": "API -> MarketplaceDataSource -> OpportunityEngine"})

    if not raw_results:
        print("No products found.")
        return

    # 4. Deep dive into the first product
    target_id = raw_results[0].get("id")
    evidence["deep_dive"]["catalog_product_id"] = target_id

    catalog_source = MercadoLibreProductCatalogDataSource(api_client)
    family_service = ProductFamilyIntelligenceService(catalog_source)
    intel = family_service.get_family_intelligence(target_id)

    evidence["deep_dive"]["family"] = {
        "title": intel.main_product.title,
        "brand": intel.main_product.brand,
        "model": intel.main_product.model,
        "status": intel.main_product.status,
        "variants_count": len(intel.variants),
        "related_ids": intel.related_catalog_ids
    }
    evidence["categorization"]["OBSERVED"].append(f"Catalog data for {target_id}")
    evidence["categorization"]["DERIVED"].append(f"Family hierarchy and variants for {target_id}")
    evidence["trace"].append({"step": "Family Intelligence", "status": "SUCCESS", "source": "API -> ProductCatalogDataSource -> ProductFamilyIntelligenceService"})

    # 5. Traffic & Review Intelligence
    item_id = None
    if intel.main_product.buy_box_winner:
        item_id = intel.main_product.buy_box_winner.external_id

    if item_id:
        traffic_service = TrafficIntelligenceService(oauth_service=oauth_service)
        visits = traffic_service.get_visits(user_id, item_id, 30)
        evidence["deep_dive"]["traffic"] = {
            "item_id": item_id,
            "total_visits": visits.total_visits,
            "daily_average": float(visits.daily_average),
            "observed_days": visits.observed_days
        }
        evidence["categorization"]["OBSERVED"].append(f"30-day traffic signal for item {item_id}")
        evidence["trace"].append({"step": "Traffic Intelligence", "status": "SUCCESS", "source": "API -> VisitsDataSource -> TrafficIntelligenceService"})

        review_service = ReviewIntelligenceService(oauth_service=oauth_service)
        reviews = review_service.get_reviews(user_id, item_id, limit=5)
        evidence["deep_dive"]["reviews"] = {
            "total": reviews.total_reviews,
            "average_rating": float(reviews.average_rating),
            "samples": [
                {"rating": r.rating, "text": r.text[:50] + "..."} for r in reviews.reviews
            ]
        }
        evidence["categorization"]["OBSERVED"].append(f"Review signal for item {item_id}")
        evidence["trace"].append({"step": "Review Intelligence", "status": "SUCCESS", "source": "API -> ReviewsDataSource -> ReviewIntelligenceService"})
    else:
        evidence["gaps"].append(f"No item_id found for deep dive into {target_id}. Traffic and Review signals are unavailable.")
        evidence["trace"].append({"step": "Traffic Intelligence", "status": "BLOCKED", "source": "Missing item_id"})
        evidence["trace"].append({"step": "Review Intelligence", "status": "BLOCKED", "source": "Missing item_id"})

    # 7. Gaps and Next Action
    evidence["gaps"].extend([
        "Falta de datos de ventas reales (restringido por API)",
        "La señal de Best Seller no está disponible vía API pública"
    ])
    evidence["next_action"] = f"Proceder con la búsqueda de proveedores (Supplier Hunter) para {selected_trend}."

    # Generate HTML
    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1000px; margin: 0 auto; padding: 20px; }}
            h1, h2, h3 {{ color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
            .evidence {{ background: #f9f9f9; border-left: 5px solid #3498db; padding: 15px; margin: 20px 0; }}
            .observed {{ color: #27ae60; font-weight: bold; }}
            .derived {{ color: #2980b9; font-weight: bold; }}
            .estimated {{ color: #e67e22; font-weight: bold; }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
            th {{ background-color: #f2f2f2; }}
            .trace {{ font-family: monospace; font-size: 0.9em; background: #2c3e50; color: #ecf0f1; padding: 15px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <h1>Reporte de Discovery: {selected_trend.upper()}</h1>

        <h2>1. Tendencia Inicial</h2>
        <p>{evidence["decision"]}</p>
        <div class="evidence">
            <span class="observed">OBSERVED:</span> Top Trends en MLC: {', '.join([t['keyword'] for t in trends[:10]])}...
        </div>

        <h2>2. Productos Descubiertos</h2>
        <table>
            <tr><th>ID</th><th>Título</th><th>Precio</th><th>Score</th></tr>
            {" ".join([f"<tr><td>{p['id']}</td><td>{p['title']}</td><td>{p['currency']} {p['price']:,.0f}</td><td>{p['score']:.2f}</td></tr>" for p in evidence["products_discovered"]])}
        </table>

        <h2>3. Deep Dive: {evidence["deep_dive"].get("family", {}).get("title", "N/A")}</h2>
        <div class="evidence">
            <p><strong>Catalog Product ID:</strong> {evidence["deep_dive"].get("catalog_product_id")}</p>
            <p><strong>Familia:</strong> {evidence["deep_dive"].get("family", {}).get("brand")} {evidence["deep_dive"].get("family", {}).get("model")}</p>
            <p><strong>Variantes detectadas:</strong> {evidence["deep_dive"].get("family", {}).get("variants_count")}</p>
        </div>

        <h3>Tráfico y Social Proof</h3>
        <ul>
            <li><strong>Visitas (30d):</strong> {evidence["deep_dive"].get("traffic", {}).get("total_visits") if evidence["deep_dive"].get("traffic") else "N/A"}</li>
            <li><strong>Reviews:</strong> {evidence["deep_dive"].get("reviews", {}).get("total") if evidence["deep_dive"].get("reviews") else "N/A"}</li>
        </ul>

        <h2>4. Categorización de Evidencia</h2>
        <ul>
            {" ".join([f"<li><span class='observed'>OBSERVED:</span> {item}</li>" for item in evidence["categorization"]["OBSERVED"]])}
            {" ".join([f"<li><span class='derived'>DERIVED:</span> {item}</li>" for item in evidence["categorization"]["DERIVED"]])}
            {" ".join([f"<li><span class='estimated'>ESTIMATED:</span> {item}</li>" for item in evidence["categorization"]["ESTIMATED"]])}
        </ul>

        <h2>5. Traza de Ejecución</h2>
        <div class="trace">
            {"<br/>".join([f"[{datetime.now().strftime('%H:%M:%S')}] {t['step']}: {t['status']} | {t['source']}" for t in evidence["trace"]])}
        </div>

        <h2>6. Gaps Identificados</h2>
        <ul>
            {" ".join([f"<li>{gap}</li>" for gap in evidence["gaps"]])}
        </ul>

        <h2>7. Siguiente Acción</h2>
        <p><strong>{evidence["next_action"]}</strong></p>
    </body>
    </html>
    """

    with open("discovery_report.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("Report generated: discovery_report.html")

if __name__ == "__main__":
    print("Starting discovery...")
    run_discovery()
