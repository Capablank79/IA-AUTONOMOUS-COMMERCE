from decimal import Decimal
from src.domain.market_intelligence.models import MarketListing, Marketplace, Money

class MercadoLibreMapper:
    @staticmethod
    def to_domain(ml_item: dict) -> MarketListing:
        # Extract basic data
        external_id = str(ml_item.get("id", ""))
        title = ml_item.get("title", "No title")
        
        # Money handling
        price_amount = Decimal(str(ml_item.get("price", "0")))
        currency = ml_item.get("currency_id", "CLP")
        
        # Quantities
        raw_sold = ml_item.get("sold_quantity")
        sold_quantity = int(raw_sold) if raw_sold is not None else None

        available_quantity = int(ml_item.get("available_quantity", 0))

        # Seller
        seller_data = ml_item.get("seller", {})
        seller_id = str(seller_data.get("id", "unknown"))
        
        # Additional info
        condition = ml_item.get("condition", "new")
        shipping_info = ml_item.get("shipping", {})
        category_id = ml_item.get("category_id", "unknown")

        return MarketListing(
            external_id=external_id,
            marketplace=Marketplace.MERCADO_LIBRE,
            title=title,
            price=Money(
                amount=price_amount,
                currency=currency
            ),
            sold_quantity=sold_quantity,
            available_quantity=available_quantity,
            seller_id=seller_id,
            condition=condition,
            shipping_info=shipping_info,
            category=category_id
        )
