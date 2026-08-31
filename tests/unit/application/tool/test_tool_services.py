import pytest
from types import MappingProxyType
from src.domain.tool.models import (
    ToolDescriptor,
    ToolVersion,
    ToolContract,
    ToolSchemaField,
    ToolSideEffectLevel,
    ToolExecutionChannel,
    ToolLifecycleStatus,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from src.domain.tool.ports import ToolInvokerPort
from src.domain.tool.registry import ToolRegistry
from src.application.tool.tool_discovery_service import ToolDiscoveryService
from src.application.tool.tool_invocation_service import ToolInvocationService
from src.application.tool.catalog import register_standard_commerce_tools
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class MockToolInvoker(ToolInvokerPort):
    def __init__(self):
        self.invocations = []
        self.return_success = True
        self.output_payload = {"results": ["item_1", "item_2"], "count": 2}

    def invoke(self, request: ToolInvocationRequest, descriptor: ToolDescriptor) -> ToolInvocationResult:
        self.invocations.append((request, descriptor))
        return ToolInvocationResult(
            tool_id=descriptor.tool_id,
            version=descriptor.version.version_str,
            success=self.return_success,
            output_payload=self.output_payload,
            correlation_id=request.correlation_id,
            provenance=descriptor.provenance,
        )


class TestToolApplicationServices:

    def test_standard_catalog_registration_and_discovery(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        discovery_service = ToolDiscoveryService(registry)

        # 1. Catálogo completo
        catalog = discovery_service.get_tool_catalog()
        assert len(catalog) >= 6

        # 2. Descubrir por capacidades
        market_tools = discovery_service.discover_tools_for_capability("MARKET_DISCOVERY")
        assert len(market_tools) == 1
        assert market_tools[0].tool_id == "market_search"

        supplier_tools = discovery_service.discover_tools_for_capability("SUPPLIER_DISCOVERY")
        assert len(supplier_tools) == 1
        assert supplier_tools[0].tool_id == "supplier_search"

        pub_tools = discovery_service.discover_tools_for_capability("COMMERCIAL_PUBLICATION")
        assert len(pub_tools) == 1
        assert pub_tools[0].tool_id == "publish_listing"

    def test_tool_invocation_happy_path(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        invoker = MockToolInvoker()
        service = ToolInvocationService(invoker=invoker)

        desc = registry.get("market_search")
        req = ToolInvocationRequest(
            tool_id="market_search",
            version="v1",
            input_payload={"query": "laptop", "limit": 10},
            correlation_id="corr-123",
        )

        invoker.output_payload = {"listings": [{"id": "ML1"}], "total_found": 1}
        result = service.invoke_tool(request=req, descriptor=desc)

        assert result.success is True
        assert result.tool_id == "market_search"
        assert result.correlation_id == "corr-123"
        assert len(invoker.invocations) == 1

    def test_tool_invocation_blocked_on_invalid_input(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        invoker = MockToolInvoker()
        service = ToolInvocationService(invoker=invoker)

        desc = registry.get("market_search")
        # Missing required 'query' field
        req = ToolInvocationRequest(
            tool_id="market_search",
            version="v1",
            input_payload={"limit": 10},
            correlation_id="corr-123",
        )

        result = service.invoke_tool(request=req, descriptor=desc)

        assert result.success is False
        assert result.error_code == "INVALID_INPUT_CONTRACT"
        assert "Missing required field: 'query'" in result.error_message
        assert len(invoker.invocations) == 0  # No debe invocar al invoker

    def test_tool_invocation_blocked_on_unknown_status(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)
        registry.update_status("market_search", "v1", ToolLifecycleStatus.UNKNOWN)

        invoker = MockToolInvoker()
        service = ToolInvocationService(invoker=invoker)

        desc = registry.get("market_search")
        req = ToolInvocationRequest(
            tool_id="market_search",
            version="v1",
            input_payload={"query": "laptop"},
        )

        result = service.invoke_tool(request=req, descriptor=desc)
        assert result.success is False
        assert result.error_code == "UNKNOWN_TOOL_STATUS"
        assert len(invoker.invocations) == 0

    def test_tool_invocation_blocked_on_disabled_status(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)
        registry.update_status("market_search", "v1", ToolLifecycleStatus.DISABLED)

        invoker = MockToolInvoker()
        service = ToolInvocationService(invoker=invoker)

        desc = registry.get("market_search")
        req = ToolInvocationRequest(
            tool_id="market_search",
            version="v1",
            input_payload={"query": "laptop"},
        )

        result = service.invoke_tool(request=req, descriptor=desc)
        assert result.success is False
        assert result.error_code == "TOOL_NOT_AVAILABLE"
        assert len(invoker.invocations) == 0

    def test_tool_invocation_blocked_by_policy_when_approval_required(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        invoker = MockToolInvoker()
        service = ToolInvocationService(invoker=invoker)

        # publish_listing es EXTERNAL_SIDE_EFFECT y requiere aprobación
        desc = registry.get("publish_listing")
        req = ToolInvocationRequest(
            tool_id="publish_listing",
            version="v1",
            input_payload={
                "title": "Smart Watch",
                "price": 49990.0,
                "category_id": "MLC1234",
                "available_quantity": 10,
            },
            correlation_id="corr-publish-1",
            idempotency_key="idemp-publish-1",
        )

        # Sin human_approved -> PolicyEngine bloquea con REQUIRE_APPROVAL
        result = service.invoke_tool(request=req, descriptor=desc, human_approved=False)
        assert result.success is False
        assert result.error_code == "REQUIRE_APPROVAL"
        assert len(invoker.invocations) == 0

        # Con human_approved=True -> PolicyEngine permite y procede la invocación
        invoker.output_payload = {
            "listing_id": "MLC99999",
            "permalink": "https://articulo.mercadolibre.cl/MLC-99999",
            "status": "active",
        }
        approved_result = service.invoke_tool(request=req, descriptor=desc, human_approved=True)
        assert approved_result.success is True
        assert approved_result.tool_id == "publish_listing"
        assert len(invoker.invocations) == 1

    def test_output_contract_validation_catches_invalid_output(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        invoker = MockToolInvoker()
        service = ToolInvocationService(invoker=invoker)

        desc = registry.get("market_search")
        req = ToolInvocationRequest(
            tool_id="market_search",
            version="v1",
            input_payload={"query": "laptop"},
        )

        # Invoker devuelve output inválido (falta 'total_found')
        invoker.output_payload = {"listings": [{"id": "ML1"}]}

        result = service.invoke_tool(request=req, descriptor=desc)
        assert result.success is False
        assert result.error_code == "INVALID_OUTPUT_CONTRACT"
        assert "Missing required field: 'total_found'" in result.error_message
