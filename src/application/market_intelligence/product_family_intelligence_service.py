from dataclasses import dataclass, field
from typing import List, Optional
from src.domain.market_intelligence.models import CatalogProduct, ProductVariant
from src.domain.market_intelligence.ports import ProductCatalogDataSource

@dataclass(frozen=True)
class ProductFamilyIntelligence:
    main_product: CatalogProduct
    parent_product: Optional[CatalogProduct] = None
    siblings: List[CatalogProduct] = field(default_factory=list)
    variants: List[ProductVariant] = field(default_factory=list)
    related_catalog_ids: List[str] = field(default_factory=list)

class ProductFamilyIntelligenceService:
    def __init__(self, catalog_data_source: ProductCatalogDataSource):
        self.catalog_data_source = catalog_data_source

    def get_family_intelligence(self, product_id: str) -> ProductFamilyIntelligence:
        """
        Navigates Product -> Catalog Product -> variants -> Listings/Items context.
        Reconstructs commercial context from a catalog_product_id.
        """
        # 1. Get main product
        main_product = self.catalog_data_source.get_product(product_id)
        
        parent_product = None
        siblings = []
        related_ids = {product_id}
        
        # 2. Get parent and siblings if applicable
        if main_product.parent_id:
            related_ids.add(main_product.parent_id)
            try:
                parent_product = self.catalog_data_source.get_product(main_product.parent_id)
                for child_id in parent_product.children_ids:
                    if child_id != product_id:
                        try:
                            siblings.append(self.catalog_data_source.get_product(child_id))
                        except Exception:
                            # If a specific child fails, we continue
                            pass
                    related_ids.add(child_id)
            except Exception:
                # If parent fails, we continue with what we have
                pass
        
        # 3. Collect variants from all pickers
        all_variants = []
        for picker in main_product.pickers:
            for variant in picker.variants:
                all_variants.append(variant)
                related_ids.add(variant.product_id)
        
        # If there's a parent, also check its pickers
        if parent_product:
            for picker in parent_product.pickers:
                for variant in picker.variants:
                    if variant.product_id not in related_ids:
                        all_variants.append(variant)
                        related_ids.add(variant.product_id)

        return ProductFamilyIntelligence(
            main_product=main_product,
            parent_product=parent_product,
            siblings=siblings,
            variants=all_variants,
            related_catalog_ids=sorted(list(related_ids))
        )
