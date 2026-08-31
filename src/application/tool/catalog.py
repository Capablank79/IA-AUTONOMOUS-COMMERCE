from src.domain.tool.models import (
    ToolDescriptor,
    ToolVersion,
    ToolContract,
    ToolSchemaField,
    ToolSideEffectLevel,
    ToolExecutionChannel,
    ToolLifecycleStatus,
    ToolEvidenceProvenance,
)
from src.domain.tool.ports import ToolRegistryPort


def register_standard_commerce_tools(registry: ToolRegistryPort) -> None:
    """
    Registra el catálogo inicial y estándar de capacidades comerciales en el Tool Registry.
    Permite al agente conocer las herramientas disponibles de forma tipada y estructurada.
    """

    # 1. Market Search Tool (v1)
    market_search_input = ToolContract(
        schema_name="MarketSearchInput",
        fields=(
            ToolSchemaField(name="query", type_name="str", required=True, description="Search query string"),
            ToolSchemaField(name="category", type_name="str", required=False, description="Category filter"),
            ToolSchemaField(name="limit", type_name="int", required=False, default_value=50, description="Max listings to fetch"),
        ),
        description="Input contract for market catalog discovery and query search",
    )
    market_search_output = ToolContract(
        schema_name="MarketSearchOutput",
        fields=(
            ToolSchemaField(name="listings", type_name="list", required=True, description="Found market listings"),
            ToolSchemaField(name="total_found", type_name="int", required=True, description="Total count of matching items"),
        ),
        description="Output contract containing observed market listings",
    )
    registry.register(
        ToolDescriptor(
            tool_id="market_search",
            name="Market Catalog Search",
            version=ToolVersion("v1"),
            description="Searches marketplace product catalogs for listings and opportunity candidates",
            capability="MARKET_DISCOVERY",
            input_contract=market_search_input,
            output_contract=market_search_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("market:read",),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.GENERIC),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("market", "catalog", "search", "discovery"),
        )
    )

    # 2. Trend Search Tool (v1)
    trend_search_input = ToolContract(
        schema_name="TrendSearchInput",
        fields=(
            ToolSchemaField(name="category_id", type_name="str", required=True, description="Marketplace category ID"),
        ),
        description="Input contract for retrieving trend keywords and growth indicators",
    )
    trend_search_output = ToolContract(
        schema_name="TrendSearchOutput",
        fields=(
            ToolSchemaField(name="trends", type_name="list", required=True, description="Trending keywords and rank"),
        ),
        description="Output contract containing discovered trend signals",
    )
    registry.register(
        ToolDescriptor(
            tool_id="trend_search",
            name="Marketplace Trend Search",
            version=ToolVersion("v1"),
            description="Fetches trending searches and rising demand signals in marketplace categories",
            capability="TREND_ANALYSIS",
            input_contract=trend_search_input,
            output_contract=trend_search_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("market:trends",),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE,),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("trends", "signals", "demand"),
        )
    )

    # 3. Supplier Search Tool (v1)
    supplier_search_input = ToolContract(
        schema_name="SupplierSearchInput",
        fields=(
            ToolSchemaField(name="product_title", type_name="str", required=True, description="Product name or keywords"),
            ToolSchemaField(name="min_units", type_name="int", required=False, default_value=1, description="Target purchase volume"),
        ),
        description="Input contract for discovering wholesale suppliers and price quotes",
    )
    supplier_search_output = ToolContract(
        schema_name="SupplierSearchOutput",
        fields=(
            ToolSchemaField(name="suppliers", type_name="list", required=True, description="Matching supplier candidates and quotes"),
        ),
        description="Output contract with supplier evidence and quotes",
    )
    registry.register(
        ToolDescriptor(
            tool_id="supplier_search",
            name="Supplier Directory Search",
            version=ToolVersion("v1"),
            description="Discovers wholesale suppliers, quotes, MOQs and lead times from vetted directories",
            capability="SUPPLIER_DISCOVERY",
            input_contract=supplier_search_input,
            output_contract=supplier_search_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("supplier:read",),
            supported_channels=(ToolExecutionChannel.SUPPLIER_DIRECTORY, ToolExecutionChannel.INTERNAL),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("supplier", "sourcing", "quotes", "wholesale"),
        )
    )

    # 4. Opportunity Scoring Tool (v1)
    opp_scoring_input = ToolContract(
        schema_name="OpportunityScoringInput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="External listing ID"),
            ToolSchemaField(name="signals", type_name="dict", required=False, description="Observed market signals"),
        ),
        description="Input contract for computing deterministic opportunity score",
    )
    opp_scoring_output = ToolContract(
        schema_name="OpportunityScoringOutput",
        fields=(
            ToolSchemaField(name="score", type_name="float", required=True, description="Opportunity score between 0 and 100"),
            ToolSchemaField(name="confidence", type_name="str", required=True, description="Confidence level"),
            ToolSchemaField(name="readiness", type_name="str", required=True, description="Opportunity readiness state"),
        ),
        description="Output contract with computed opportunity metrics",
    )
    registry.register(
        ToolDescriptor(
            tool_id="opportunity_scoring",
            name="Deterministic Opportunity Scorer",
            version=ToolVersion("v1"),
            description="Computes reproducible multi-factor opportunity score without LLM hallucinations",
            capability="OPPORTUNITY_EVALUATION",
            input_contract=opp_scoring_input,
            output_contract=opp_scoring_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=(),
            supported_channels=(ToolExecutionChannel.INTERNAL,),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("opportunity", "scoring", "analysis"),
        )
    )

    # 5. Landed Cost & Profit Calculation Tool (v1)
    profit_calc_input = ToolContract(
        schema_name="ProfitCalculationInput",
        fields=(
            ToolSchemaField(name="unit_cost", type_name="float", required=True, description="Base supplier unit cost"),
            ToolSchemaField(name="target_sale_price", type_name="float", required=True, description="Expected retail price"),
            ToolSchemaField(name="shipping_cost", type_name="float", required=False, default_value=0.0, description="Estimated freight cost"),
        ),
        description="Input contract for unit economics and landed cost evaluation",
    )
    profit_calc_output = ToolContract(
        schema_name="ProfitCalculationOutput",
        fields=(
            ToolSchemaField(name="gross_margin_pct", type_name="float", required=True, description="Gross profit margin percentage"),
            ToolSchemaField(name="net_margin_pct", type_name="float", required=True, description="Net profit margin percentage"),
            ToolSchemaField(name="break_even_price", type_name="float", required=True, description="Minimum price to avoid loss"),
        ),
        description="Output contract with unit economics breakdown",
    )
    registry.register(
        ToolDescriptor(
            tool_id="profit_calculation",
            name="Unit Economics & Profit Engine",
            version=ToolVersion("v1"),
            description="Evaluates landed cost, taxes, marketplace commissions and break-even thresholds",
            capability="PROFIT_EVALUATION",
            input_contract=profit_calc_input,
            output_contract=profit_calc_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=(),
            supported_channels=(ToolExecutionChannel.INTERNAL,),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("profit", "economics", "margin", "finance"),
        )
    )

    # 6. Marketplace Listing Publication Tool (v1)
    publish_listing_input = ToolContract(
        schema_name="PublishListingInput",
        fields=(
            ToolSchemaField(name="title", type_name="str", required=True, description="Listing title"),
            ToolSchemaField(name="price", type_name="float", required=True, description="Listing selling price"),
            ToolSchemaField(name="category_id", type_name="str", required=True, description="Target category identifier"),
            ToolSchemaField(name="available_quantity", type_name="int", required=True, description="Initial inventory quantity"),
        ),
        description="Input contract for creating a published listing on an external sales channel",
    )
    publish_listing_output = ToolContract(
        schema_name="PublishListingOutput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="External created listing ID"),
            ToolSchemaField(name="permalink", type_name="str", required=False, description="Public item URL"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Item initial status"),
        ),
        description="Output contract with published item identifiers",
    )
    registry.register(
        ToolDescriptor(
            tool_id="publish_listing",
            name="External Marketplace Listing Publisher",
            version=ToolVersion("v1"),
            description="Publishes new commercial product listings to external marketplace channels",
            capability="COMMERCIAL_PUBLICATION",
            input_contract=publish_listing_input,
            output_contract=publish_listing_output,
            side_effect_level=ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT,
            required_permissions=("listing:publish", "marketplace:write"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_approval=True,
            requires_idempotency=True,
            tags=("publication", "marketplace", "listing", "external_impact"),
        )
    )

    # 7. Listing Draft Generation Tool (v1 - G.1)
    generate_listing_input = ToolContract(
        schema_name="GenerateListingInput",
        fields=(
            ToolSchemaField(name="product_id", type_name="str", required=True, description="Product identifier"),
            ToolSchemaField(name="title", type_name="str", required=True, description="Base product title"),
            ToolSchemaField(name="price", type_name="float", required=True, description="Listing price"),
            ToolSchemaField(name="currency", type_name="str", required=False, default_value="CLP", description="Currency"),
            ToolSchemaField(name="available_quantity", type_name="int", required=False, default_value=1, description="Available stock"),
            ToolSchemaField(name="attributes", type_name="dict", required=False, description="Verified product attributes"),
        ),
        description="Input contract for generating a grounded, structured listing draft",
    )
    generate_listing_output = ToolContract(
        schema_name="GenerateListingOutput",
        fields=(
            ToolSchemaField(name="draft_id", type_name="str", required=True, description="Generated ListingDraft ID"),
            ToolSchemaField(name="optimized_title", type_name="str", required=True, description="SEO and constraint optimized title"),
            ToolSchemaField(name="description", type_name="str", required=True, description="Structured factual description"),
            ToolSchemaField(name="bullets", type_name="list", required=True, description="Evidence-backed bullet points"),
        ),
        description="Output contract containing structured generated listing draft and grounding summary",
    )
    registry.register(
        ToolDescriptor(
            tool_id="generate_listing_draft",
            name="Listing Draft Generator",
            version=ToolVersion("v1"),
            description="Generates grounded, evidence-backed commercial listing drafts with SEO and customer pain differentiation",
            capability="LISTING_GENERATION",
            input_contract=generate_listing_input,
            output_contract=generate_listing_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=(),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("listing", "generation", "copywriting", "seo", "grounding"),
        )
    )

    # 8. Listing Quality & Policy Validator Tool (v1 - G.2)
    validate_listing_input = ToolContract(
        schema_name="ValidateListingInput",
        fields=(
            ToolSchemaField(name="draft_id", type_name="str", required=True, description="Target ListingDraft identifier"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target marketplace channel"),
            ToolSchemaField(name="strict_mode", type_name="bool", required=False, default_value=True, description="Enforce strict product truth checking"),
        ),
        description="Input contract for validating listing quality, product truth, policies, and channel constraints",
    )
    validate_listing_output = ToolContract(
        schema_name="ValidateListingOutput",
        fields=(
            ToolSchemaField(name="status", type_name="str", required=True, description="Validation status (VALID, NEEDS_REVIEW, INVALID, BLOCKED)"),
            ToolSchemaField(name="is_valid", type_name="bool", required=True, description="Boolean flag indicating if listing is valid"),
            ToolSchemaField(name="overall_quality_score", type_name="float", required=True, description="Overall quality score (0.0 to 100.0)"),
            ToolSchemaField(name="violations_count", type_name="int", required=True, description="Total count of blocker or error violations"),
            ToolSchemaField(name="warnings_count", type_name="int", required=True, description="Total count of warning findings"),
        ),
        description="Output contract containing structured validation findings and quality score breakdown",
    )
    registry.register(
        ToolDescriptor(
            tool_id="validate_listing_quality",
            name="Listing Quality & Policy Validator",
            version=ToolVersion("v1"),
            description="Validates listing drafts against product truth, channel constraints, compliance policies, SEO, and quality rules",
            capability="LISTING_VALIDATION",
            input_contract=validate_listing_input,
            output_contract=validate_listing_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=(),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.GENERIC),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("listing", "validation", "quality", "policy", "grounding", "compliance"),
        )
    )

    # 9. Listing Price Update Tool (v1 - G.4)
    update_price_input = ToolContract(
        schema_name="UpdateListingPriceInput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="External marketplace listing ID"),
            ToolSchemaField(name="new_price", type_name="float", required=True, description="Target updated listing price"),
            ToolSchemaField(name="currency", type_name="str", required=False, default_value="CLP", description="Price currency"),
            ToolSchemaField(name="reason", type_name="str", required=False, default_value="MARGIN_OPTIMIZATION", description="Rationale/reason for price adjustment"),
            ToolSchemaField(name="minimum_allowed_price", type_name="float", required=False, description="Calculated price floor defense"),
        ),
        description="Input contract for updating listing selling price on an external marketplace channel",
    )
    update_price_output = ToolContract(
        schema_name="UpdateListingPriceOutput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="Target listing identifier"),
            ToolSchemaField(name="applied_price", type_name="float", required=True, description="Confirmed applied price"),
            ToolSchemaField(name="previous_price", type_name="float", required=False, description="Previous listing price"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Execution status (APPLIED, UNKNOWN, FAILED)"),
        ),
        description="Output contract containing confirmed price update result and status",
    )
    registry.register(
        ToolDescriptor(
            tool_id="update_listing_price",
            name="Marketplace Listing Price Updater",
            version=ToolVersion("v1"),
            description="Updates listing price on external marketplaces under deterministic policy governance and price floor defense",
            capability="PRICING_MANAGEMENT",
            input_contract=update_price_input,
            output_contract=update_price_output,
            side_effect_level=ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT,
            required_permissions=("pricing:write", "marketplace:write"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_approval=True,
            requires_idempotency=True,
            tags=("pricing", "marketplace", "price_floor", "external_impact", "governance"),
        )
    )

    # 10. Get Current Inventory Tool (v1 - G.5)
    get_inventory_input = ToolContract(
        schema_name="GetInventoryInput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="External marketplace listing ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
        ),
        description="Input contract for querying current live stock on a marketplace listing",
    )
    get_inventory_output = ToolContract(
        schema_name="GetInventoryOutput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="Listing ID"),
            ToolSchemaField(name="available_quantity", type_name="int", required=False, description="Current available quantity on channel"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Execution status (APPLIED, UNKNOWN, FAILED)"),
        ),
        description="Output contract with observed inventory level",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_inventory",
            name="Marketplace Inventory Reader",
            version=ToolVersion("v1"),
            description="Reads live stock level and status of a published marketplace listing",
            capability="INVENTORY_MANAGEMENT",
            input_contract=get_inventory_input,
            output_contract=get_inventory_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("inventory:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("inventory", "stock", "read", "marketplace"),
        )
    )

    # 11. Update Listing Inventory Tool (v1 - G.5)
    update_inventory_input = ToolContract(
        schema_name="UpdateInventoryInput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="External marketplace listing ID"),
            ToolSchemaField(name="new_quantity", type_name="int", required=True, description="Target updated stock quantity"),
            ToolSchemaField(name="reason", type_name="str", required=False, default_value="SUPPLIER_SYNC", description="Rationale/reason for stock adjustment"),
            ToolSchemaField(name="safety_buffer", type_name="int", required=False, default_value=1, description="Enforced safety buffer"),
        ),
        description="Input contract for updating listing available stock on an external marketplace channel",
    )
    update_inventory_output = ToolContract(
        schema_name="UpdateInventoryOutput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="Target listing identifier"),
            ToolSchemaField(name="applied_quantity", type_name="int", required=True, description="Confirmed applied stock quantity"),
            ToolSchemaField(name="previous_quantity", type_name="int", required=False, description="Previous stock quantity"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Execution status (APPLIED, UNKNOWN, FAILED)"),
        ),
        description="Output contract containing confirmed inventory update result and status",
    )
    registry.register(
        ToolDescriptor(
            tool_id="update_inventory",
            name="Marketplace Inventory Updater",
            version=ToolVersion("v1"),
            description="Updates listing stock on external marketplaces under strict overselling protection and safety buffer guardrails",
            capability="INVENTORY_MANAGEMENT",
            input_contract=update_inventory_input,
            output_contract=update_inventory_output,
            side_effect_level=ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT,
            required_permissions=("inventory:write", "marketplace:write"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_approval=True,
            requires_idempotency=True,
            tags=("inventory", "stock", "overselling_protection", "external_impact", "governance"),
        )
    )

    # 12. Reconcile Inventory Tool (v1 - G.5)
    reconcile_inventory_input = ToolContract(
        schema_name="ReconcileInventoryInput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="External marketplace listing ID"),
            ToolSchemaField(name="supplier_stock", type_name="int", required=True, description="Confirmed supplier stock level"),
            ToolSchemaField(name="safety_buffer", type_name="int", required=False, default_value=1, description="Safety buffer applied"),
        ),
        description="Input contract for checking discrepancy between local backed stock and marketplace published stock",
    )
    reconcile_inventory_output = ToolContract(
        schema_name="ReconcileInventoryOutput",
        fields=(
            ToolSchemaField(name="listing_id", type_name="str", required=True, description="Target listing identifier"),
            ToolSchemaField(name="is_reconciled", type_name="bool", required=True, description="Whether local calculated stock matches marketplace"),
            ToolSchemaField(name="marketplace_stock", type_name="int", required=False, description="Observed stock in marketplace"),
            ToolSchemaField(name="target_available_stock", type_name="int", required=True, description="Calculated safe available stock"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Status of reconciliation"),
        ),
        description="Output contract containing inventory reconciliation report",
    )
    registry.register(
        ToolDescriptor(
            tool_id="reconcile_inventory",
            name="Marketplace Inventory Reconciler",
            version=ToolVersion("v1"),
            description="Reconciles supplier backed inventory with marketplace published stock and identifies drift or overselling risk",
            capability="INVENTORY_MANAGEMENT",
            input_contract=reconcile_inventory_input,
            output_contract=reconcile_inventory_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=("inventory:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("inventory", "reconciliation", "sync", "audit"),
        )
    )

    # 13. Get Orders Tool (v1 - G.6)
    get_orders_input = ToolContract(
        schema_name="GetOrdersInput",
        fields=(
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
            ToolSchemaField(name="status", type_name="str", required=False, description="Filter orders by status (e.g. paid, confirmed)"),
            ToolSchemaField(name="limit", type_name="int", required=False, default_value=50, description="Max orders to fetch"),
        ),
        description="Input contract for querying recent commercial orders from sales channels",
    )
    get_orders_output = ToolContract(
        schema_name="GetOrdersOutput",
        fields=(
            ToolSchemaField(name="orders", type_name="list", required=True, description="List of normalized orders"),
            ToolSchemaField(name="total_count", type_name="int", required=True, description="Total count of matching orders"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Query execution status"),
        ),
        description="Output contract containing retrieved orders list",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_orders",
            name="Marketplace Orders Search & Polling",
            version=ToolVersion("v1"),
            description="Fetches recent marketplace orders with normalized status and minimized PII",
            capability="ORDER_MANAGEMENT",
            input_contract=get_orders_input,
            output_contract=get_orders_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("orders:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("orders", "sales", "read", "polling", "marketplace"),
        )
    )

    # 14. Get Order Detail Tool (v1 - G.6)
    get_order_input = ToolContract(
        schema_name="GetOrderInput",
        fields=(
            ToolSchemaField(name="external_order_id", type_name="str", required=True, description="External marketplace order ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for retrieving single order details",
    )
    get_order_output = ToolContract(
        schema_name="GetOrderOutput",
        fields=(
            ToolSchemaField(name="order_id", type_name="str", required=True, description="Internal normalized order ID"),
            ToolSchemaField(name="external_order_id", type_name="str", required=True, description="External order ID"),
            ToolSchemaField(name="order_status", type_name="str", required=True, description="Normalized order status"),
            ToolSchemaField(name="payment_status", type_name="str", required=True, description="Normalized payment status"),
            ToolSchemaField(name="total_amount", type_name="float", required=True, description="Total order amount"),
            ToolSchemaField(name="currency", type_name="str", required=True, description="Currency code"),
            ToolSchemaField(name="items_count", type_name="int", required=True, description="Line items count"),
        ),
        description="Output contract containing single normalized order structure",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_order",
            name="Marketplace Order Reader",
            version=ToolVersion("v1"),
            description="Retrieves complete normalized order details by external order ID with privacy protection",
            capability="ORDER_MANAGEMENT",
            input_contract=get_order_input,
            output_contract=get_order_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("orders:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("orders", "sales", "read", "detail"),
        )
    )

    # 15. Reconcile Order Tool (v1 - G.6)
    reconcile_order_input = ToolContract(
        schema_name="ReconcileOrderInput",
        fields=(
            ToolSchemaField(name="order_id", type_name="str", required=True, description="Internal or external order identifier"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
        ),
        description="Input contract for reconciling internal order state with external marketplace state",
    )
    reconcile_order_output = ToolContract(
        schema_name="ReconcileOrderOutput",
        fields=(
            ToolSchemaField(name="order_id", type_name="str", required=True, description="Order identifier"),
            ToolSchemaField(name="is_reconciled", type_name="bool", required=True, description="Whether internal state matches external state"),
            ToolSchemaField(name="internal_status", type_name="str", required=True, description="Local order status"),
            ToolSchemaField(name="external_status", type_name="str", required=True, description="Marketplace order status"),
            ToolSchemaField(name="requires_action", type_name="bool", required=True, description="Whether action is required to resolve drift"),
        ),
        description="Output contract containing order reconciliation report",
    )
    registry.register(
        ToolDescriptor(
            tool_id="reconcile_order",
            name="Marketplace Order Reconciler",
            version=ToolVersion("v1"),
            description="Reconciles internal order and payment status against external channel state without destructive overwrites",
            capability="ORDER_MANAGEMENT",
            input_contract=reconcile_order_input,
            output_contract=reconcile_order_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=("orders:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("orders", "reconciliation", "sync", "audit"),
        )
    )

    # 16. Get Shipments Tool (v1 - G.7)
    get_shipments_input = ToolContract(
        schema_name="GetShipmentsInput",
        fields=(
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
            ToolSchemaField(name="status", type_name="str", required=False, description="Filter shipments by status (e.g. ready_to_ship, shipped, delivered)"),
            ToolSchemaField(name="limit", type_name="int", required=False, default_value=50, description="Max shipments to fetch"),
        ),
        description="Input contract for querying shipments from logistics/marketplace channels",
    )
    get_shipments_output = ToolContract(
        schema_name="GetShipmentsOutput",
        fields=(
            ToolSchemaField(name="shipments", type_name="list", required=True, description="List of normalized shipments"),
            ToolSchemaField(name="total_count", type_name="int", required=True, description="Total count of matching shipments"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Query execution status"),
        ),
        description="Output contract containing retrieved shipments list",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_shipments",
            name="Marketplace Shipments Search & Polling",
            version=ToolVersion("v1"),
            description="Fetches recent marketplace shipments with normalized logistics status and carrier info",
            capability="FULFILLMENT_MANAGEMENT",
            input_contract=get_shipments_input,
            output_contract=get_shipments_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("fulfillment:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("fulfillment", "shipping", "logistics", "read", "marketplace"),
        )
    )

    # 17. Get Shipment Detail Tool (v1 - G.7)
    get_shipment_input = ToolContract(
        schema_name="GetShipmentInput",
        fields=(
            ToolSchemaField(name="external_shipment_id", type_name="str", required=True, description="External marketplace shipment ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for retrieving single shipment details and tracking state",
    )
    get_shipment_output = ToolContract(
        schema_name="GetShipmentOutput",
        fields=(
            ToolSchemaField(name="shipment_id", type_name="str", required=True, description="Internal normalized shipment ID"),
            ToolSchemaField(name="external_shipment_id", type_name="str", required=True, description="External shipment ID"),
            ToolSchemaField(name="order_id", type_name="str", required=True, description="Associated order ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Normalized shipment status"),
            ToolSchemaField(name="carrier", type_name="str", required=False, description="Assigned carrier name"),
            ToolSchemaField(name="tracking_number", type_name="str", required=False, description="Logistics tracking number"),
            ToolSchemaField(name="service_level", type_name="str", required=True, description="Shipping service level"),
        ),
        description="Output contract containing single normalized shipment structure",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_shipment",
            name="Marketplace Shipment Reader",
            version=ToolVersion("v1"),
            description="Retrieves complete normalized shipment details by external shipment ID with carrier and tracking info",
            capability="FULFILLMENT_MANAGEMENT",
            input_contract=get_shipment_input,
            output_contract=get_shipment_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("fulfillment:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("fulfillment", "shipping", "read", "detail", "tracking"),
        )
    )

    # 18. Get Tracking History Tool (v1 - G.7)
    get_tracking_input = ToolContract(
        schema_name="GetTrackingInput",
        fields=(
            ToolSchemaField(name="external_shipment_id", type_name="str", required=True, description="External shipment ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for querying the full tracking events history of a shipment",
    )
    get_tracking_output = ToolContract(
        schema_name="GetTrackingOutput",
        fields=(
            ToolSchemaField(name="shipment_id", type_name="str", required=True, description="Shipment identifier"),
            ToolSchemaField(name="events", type_name="list", required=True, description="Chronological list of tracking events"),
            ToolSchemaField(name="events_count", type_name="int", required=True, description="Number of observed tracking events"),
        ),
        description="Output contract containing tracking history events",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_tracking",
            name="Shipment Tracking History Reader",
            version=ToolVersion("v1"),
            description="Fetches chronological tracking events with provenance, timestamps and locations for a shipment",
            capability="FULFILLMENT_MANAGEMENT",
            input_contract=get_tracking_input,
            output_contract=get_tracking_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("fulfillment:read", "tracking:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.INTERNAL),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("fulfillment", "tracking", "history", "carrier", "logistics"),
        )
    )

    # 19. Reconcile Shipment Tool (v1 - G.7)
    reconcile_shipment_input = ToolContract(
        schema_name="ReconcileShipmentInput",
        fields=(
            ToolSchemaField(name="shipment_id", type_name="str", required=True, description="Internal or external shipment identifier"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
        ),
        description="Input contract for reconciling internal shipment state with external logistics state",
    )
    reconcile_shipment_output = ToolContract(
        schema_name="ReconcileShipmentOutput",
        fields=(
            ToolSchemaField(name="shipment_id", type_name="str", required=True, description="Shipment identifier"),
            ToolSchemaField(name="is_reconciled", type_name="bool", required=True, description="Whether internal state matches external state"),
            ToolSchemaField(name="internal_status", type_name="str", required=True, description="Local shipment status"),
            ToolSchemaField(name="external_status", type_name="str", required=True, description="Marketplace shipment status"),
            ToolSchemaField(name="requires_action", type_name="bool", required=True, description="Whether action is required to resolve drift"),
            ToolSchemaField(name="discrepancies", type_name="list", required=True, description="List of identified discrepancies"),
        ),
        description="Output contract containing shipment reconciliation report",
    )
    registry.register(
        ToolDescriptor(
            tool_id="reconcile_shipment",
            name="Shipment Logistics Reconciler",
            version=ToolVersion("v1"),
            description="Reconciles internal shipment status and tracking number against external channel state without destructive overwrites",
            capability="FULFILLMENT_MANAGEMENT",
            input_contract=reconcile_shipment_input,
            output_contract=reconcile_shipment_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=("fulfillment:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("fulfillment", "reconciliation", "sync", "audit", "shipping"),
        )
    )

    # 20. Prepare Fulfillment Tool (v1 - G.7)
    prepare_fulfillment_input = ToolContract(
        schema_name="PrepareFulfillmentInput",
        fields=(
            ToolSchemaField(name="order_id", type_name="str", required=True, description="Internal or external order ID to fulfill"),
            ToolSchemaField(name="service_level", type_name="str", required=False, default_value="STANDARD", description="Desired shipping service level"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for initiating fulfillment preparation for an order",
    )
    prepare_fulfillment_output = ToolContract(
        schema_name="PrepareFulfillmentOutput",
        fields=(
            ToolSchemaField(name="shipment_id", type_name="str", required=True, description="Created or retrieved shipment ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Shipment status (READY_TO_SHIP)"),
            ToolSchemaField(name="service_level", type_name="str", required=True, description="Confirmed service level"),
        ),
        description="Output contract containing prepared shipment information",
    )
    registry.register(
        ToolDescriptor(
            tool_id="prepare_fulfillment",
            name="Order Fulfillment Preparer",
            version=ToolVersion("v1"),
            description="Prepares fulfillment and generates initial shipment records for confirmed orders under policy governance",
            capability="FULFILLMENT_MANAGEMENT",
            input_contract=prepare_fulfillment_input,
            output_contract=prepare_fulfillment_output,
            side_effect_level=ToolSideEffectLevel.WRITE,
            required_permissions=("fulfillment:write", "orders:read"),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_idempotency=True,
            tags=("fulfillment", "preparation", "dispatch", "governance"),
        )
    )

    # 21. Create Shipping Label Tool (v1 - G.7)
    create_shipping_label_input = ToolContract(
        schema_name="CreateShippingLabelInput",
        fields=(
            ToolSchemaField(name="external_shipment_id", type_name="str", required=True, description="External marketplace shipment ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
            ToolSchemaField(name="format", type_name="str", required=False, default_value="PDF", description="Desired label format (PDF/ZPL)"),
        ),
        description="Input contract for requesting or retrieving shipping labels from marketplace carriers",
    )
    create_shipping_label_output = ToolContract(
        schema_name="CreateShippingLabelOutput",
        fields=(
            ToolSchemaField(name="label_id", type_name="str", required=True, description="Created or retrieved label ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Label status (READY, ERROR, NOT_SUPPORTED)"),
            ToolSchemaField(name="url", type_name="str", required=False, description="Label download URL if available"),
            ToolSchemaField(name="format", type_name="str", required=True, description="Confirmed label format"),
        ),
        description="Output contract containing shipping label reference and format",
    )
    registry.register(
        ToolDescriptor(
            tool_id="create_shipping_label",
            name="Marketplace Shipping Label Generator",
            version=ToolVersion("v1"),
            description="Generates or retrieves official shipping labels from marketplace carrier services under policy governance",
            capability="FULFILLMENT_MANAGEMENT",
            input_contract=create_shipping_label_input,
            output_contract=create_shipping_label_output,
            side_effect_level=ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT,
            required_permissions=("fulfillment:write", "marketplace:write"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE,),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_approval=False,
            requires_idempotency=True,
            tags=("fulfillment", "labels", "shipping", "me2", "governance"),
        )
    )

    # 22. Get Returns Tool (v1 - G.8)
    get_returns_input = ToolContract(
        schema_name="GetReturnsInput",
        fields=(
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
            ToolSchemaField(name="status", type_name="str", required=False, description="Filter returns by status (e.g. REQUESTED, APPROVED, IN_TRANSIT, RECEIVED, RESOLVED)"),
            ToolSchemaField(name="limit", type_name="int", required=False, default_value=50, description="Max returns to fetch"),
        ),
        description="Input contract for querying post-sale returns from marketplace channels",
    )
    get_returns_output = ToolContract(
        schema_name="GetReturnsOutput",
        fields=(
            ToolSchemaField(name="returns", type_name="list", required=True, description="List of observed returns"),
            ToolSchemaField(name="total_count", type_name="int", required=True, description="Total matching returns count"),
            ToolSchemaField(name="channel_id", type_name="str", required=True, description="Channel queried"),
        ),
        description="Output contract containing post-sale returns query results",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_returns",
            name="Marketplace Returns Reader",
            version=ToolVersion("v1"),
            description="Queries and inspects post-sale returns from marketplaces with privacy protection and status normalization",
            capability="RETURNS_MANAGEMENT",
            input_contract=get_returns_input,
            output_contract=get_returns_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("returns:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("returns", "post-sale", "read", "polling", "marketplace"),
        )
    )

    # 23. Get Single Return Tool (v1 - G.8)
    get_return_input = ToolContract(
        schema_name="GetReturnInput",
        fields=(
            ToolSchemaField(name="external_return_id", type_name="str", required=True, description="External marketplace return ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for retrieving single return details",
    )
    get_return_output = ToolContract(
        schema_name="GetReturnOutput",
        fields=(
            ToolSchemaField(name="return_id", type_name="str", required=True, description="Internal normalized return ID"),
            ToolSchemaField(name="external_return_id", type_name="str", required=True, description="External return ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Normalized return status"),
            ToolSchemaField(name="reason", type_name="str", required=True, description="Normalized return reason"),
            ToolSchemaField(name="resolution", type_name="str", required=True, description="Normalized resolution"),
            ToolSchemaField(name="refund_status", type_name="str", required=False, description="Refund status if applicable"),
        ),
        description="Output contract containing normalized single return structure",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_return",
            name="Marketplace Return Reader",
            version=ToolVersion("v1"),
            description="Retrieves complete normalized return details by external return ID with reason and refund info",
            capability="RETURNS_MANAGEMENT",
            input_contract=get_return_input,
            output_contract=get_return_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("returns:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.SHOPIFY, ToolExecutionChannel.AMAZON),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("returns", "post-sale", "read", "detail"),
        )
    )

    # 24. Get Single Claim Tool (v1 - G.8)
    get_claim_input = ToolContract(
        schema_name="GetClaimInput",
        fields=(
            ToolSchemaField(name="external_claim_id", type_name="str", required=True, description="External marketplace claim/dispute ID"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for retrieving single claim/dispute details",
    )
    get_claim_output = ToolContract(
        schema_name="GetClaimOutput",
        fields=(
            ToolSchemaField(name="claim_id", type_name="str", required=True, description="Internal normalized claim ID"),
            ToolSchemaField(name="external_claim_id", type_name="str", required=True, description="External claim ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Normalized claim status"),
            ToolSchemaField(name="stage", type_name="str", required=True, description="Claim stage (DISPUTE/CLAIM/MEDIATION)"),
            ToolSchemaField(name="reason", type_name="str", required=True, description="Normalized claim reason"),
        ),
        description="Output contract containing normalized claim/dispute structure",
    )
    registry.register(
        ToolDescriptor(
            tool_id="get_claim",
            name="Marketplace Claim Reader",
            version=ToolVersion("v1"),
            description="Retrieves normalized claim and dispute details by external claim ID",
            capability="RETURNS_MANAGEMENT",
            input_contract=get_claim_input,
            output_contract=get_claim_output,
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            required_permissions=("claims:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.INTERNAL),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            tags=("claims", "disputes", "read", "detail"),
        )
    )

    # 25. Reconcile Return Tool (v1 - G.8)
    reconcile_return_input = ToolContract(
        schema_name="ReconcileReturnInput",
        fields=(
            ToolSchemaField(name="return_id", type_name="str", required=True, description="Internal or external return identifier"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Target sales channel"),
        ),
        description="Input contract for reconciling internal return state with external marketplace state",
    )
    reconcile_return_output = ToolContract(
        schema_name="ReconcileReturnOutput",
        fields=(
            ToolSchemaField(name="return_id", type_name="str", required=True, description="Return identifier"),
            ToolSchemaField(name="is_reconciled", type_name="bool", required=True, description="Whether internal state matches external state"),
            ToolSchemaField(name="internal_status", type_name="str", required=True, description="Local return status"),
            ToolSchemaField(name="external_status", type_name="str", required=True, description="Marketplace return status"),
            ToolSchemaField(name="requires_action", type_name="bool", required=True, description="Whether action is required to resolve drift"),
            ToolSchemaField(name="discrepancies", type_name="list", required=True, description="List of identified discrepancies"),
        ),
        description="Output contract containing return reconciliation report",
    )
    registry.register(
        ToolDescriptor(
            tool_id="reconcile_return",
            name="Marketplace Return Reconciler",
            version=ToolVersion("v1"),
            description="Reconciles internal return status and refund state against external marketplace state without destructive overwrites",
            capability="RETURNS_MANAGEMENT",
            input_contract=reconcile_return_input,
            output_contract=reconcile_return_output,
            side_effect_level=ToolSideEffectLevel.ANALYSIS,
            required_permissions=("returns:read", "marketplace:read"),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.DERIVED,
            tags=("returns", "reconciliation", "sync", "audit", "post-sale"),
        )
    )

    # 26. Create Return Request Tool (v1 - G.8)
    create_return_input = ToolContract(
        schema_name="CreateReturnRequestInput",
        fields=(
            ToolSchemaField(name="order_id", type_name="str", required=True, description="Internal or external order ID to request return for"),
            ToolSchemaField(name="reason", type_name="str", required=False, default_value="DEFECTIVE", description="Reason for return"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for requesting a post-sale return",
    )
    create_return_output = ToolContract(
        schema_name="CreateReturnRequestOutput",
        fields=(
            ToolSchemaField(name="return_id", type_name="str", required=True, description="Created or retrieved return ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Return status (REQUESTED)"),
            ToolSchemaField(name="reason", type_name="str", required=True, description="Confirmed return reason"),
        ),
        description="Output contract containing requested return structure",
    )
    registry.register(
        ToolDescriptor(
            tool_id="create_return_request",
            name="Post-Sale Return Requester",
            version=ToolVersion("v1"),
            description="Initiates or records a post-sale return request under deterministic policy governance",
            capability="RETURNS_MANAGEMENT",
            input_contract=create_return_input,
            output_contract=create_return_output,
            side_effect_level=ToolSideEffectLevel.WRITE,
            required_permissions=("returns:write", "orders:read"),
            supported_channels=(ToolExecutionChannel.INTERNAL, ToolExecutionChannel.MERCADO_LIBRE),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_idempotency=True,
            tags=("returns", "requests", "post-sale", "governance"),
        )
    )

    # 27. Issue Refund Tool (v1 - G.8)
    issue_refund_input = ToolContract(
        schema_name="IssueRefundInput",
        fields=(
            ToolSchemaField(name="return_id", type_name="str", required=True, description="Return identifier to refund"),
            ToolSchemaField(name="amount", type_name="float", required=True, description="Monetary refund amount"),
            ToolSchemaField(name="currency", type_name="str", required=False, default_value="USD", description="Currency code"),
            ToolSchemaField(name="channel_id", type_name="str", required=False, default_value="MERCADO_LIBRE", description="Sales channel"),
        ),
        description="Input contract for issuing post-sale refunds under policy governance",
    )
    issue_refund_output = ToolContract(
        schema_name="IssueRefundOutput",
        fields=(
            ToolSchemaField(name="refund_id", type_name="str", required=True, description="Created refund ID"),
            ToolSchemaField(name="status", type_name="str", required=True, description="Refund status (CONFIRMED, PROCESSING, FAILED, UNKNOWN)"),
            ToolSchemaField(name="amount", type_name="float", required=True, description="Confirmed refund amount"),
        ),
        description="Output contract containing confirmed refund result",
    )
    registry.register(
        ToolDescriptor(
            tool_id="issue_refund",
            name="Post-Sale Refund Issuer",
            version=ToolVersion("v1"),
            description="Issues post-sale monetary refund with strict idempotency and policy governance controls",
            capability="RETURNS_MANAGEMENT",
            input_contract=issue_refund_input,
            output_contract=issue_refund_output,
            side_effect_level=ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT,
            required_permissions=("refunds:write", "marketplace:write"),
            supported_channels=(ToolExecutionChannel.MERCADO_LIBRE, ToolExecutionChannel.INTERNAL),
            status=ToolLifecycleStatus.AVAILABLE,
            provenance=ToolEvidenceProvenance.LIVE,
            requires_approval=True,
            requires_idempotency=True,
            tags=("refunds", "finance", "post-sale", "governance"),
        )
    )
