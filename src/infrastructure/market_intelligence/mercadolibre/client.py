import json
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional

class MercadoLibreClientError(Exception):
    """Base exception for Mercado Libre Client errors."""
    pass

class MercadoLibreClient:
    """
    HTTP Client for Mercado Libre API.
    """
    BASE_URL = "https://api.mercadolibre.com"

    def __init__(self, access_token: Optional[str] = None):
        self.access_token = access_token

    def search(self, q: str, category: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        params = {"q": q, "limit": limit}
        if category:
            params["category"] = category
            
        # Default site MLC (Chile), target marketplace
        url = f"{self.BASE_URL}/sites/MLC/search?{urllib.parse.urlencode(params)}"
        
        headers = {
            "Accept": "application/json",
            "User-Agent": "AI-Autonomous-Commerce-Lab/0.1.0"
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as response:
                if response.status != 200:
                    raise MercadoLibreClientError(f"API returned status {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except Exception as e:
            raise MercadoLibreClientError(f"Failed to fetch data from Mercado Libre: {e}")
