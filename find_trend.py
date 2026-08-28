
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.application.oauth.dependencies import oauth_service
from src.application.market_intelligence.trend_intelligence_service import TrendIntelligenceService

def find():
    user_id = '55197108'
    connection = oauth_service.get_valid_connection('mercadolibre', user_id)
    api_client = MercadoLibreApiClient(connection.access_token)
    trend_service = TrendIntelligenceService(oauth_service)
    
    trends = trend_service.get_trends(user_id)
    print(f"Checking {len(trends)} trends...")
    
    for t in trends[:50]:
        keyword = t['keyword']
        from urllib.parse import quote
        try:
            data = api_client.get(f'/products/search?status=active&site_id=MLC&q={quote(keyword)}')
            results = data.get('results', [])
            winners = [i.get('buy_box_winner') is not None for i in results]
            if any(winners):
                print(f"FOUND TREND: {keyword} - Winners: {sum(winners)}/{len(results)}")
                return keyword
            else:
                print(f"No winners for {keyword}")
        except Exception as e:
            print(f"Error checking {keyword}: {e}")
            
    print("No trends with winners found in top 50.")
    return None

if __name__ == "__main__":
    find()
