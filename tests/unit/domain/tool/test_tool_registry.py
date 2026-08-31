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
    ToolEvidenceProvenance,
)
from src.domain.tool.registry import ToolRegistry
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


def create_sample_contract(name="SampleContract"):
    return ToolContract(
        schema_name=name,
        fields=(
            ToolSchemaField(name="query", type_name="str", required=True),
            ToolSchemaField(name="limit", type_name="int", required=False, default_value=10),
        ),
        description="Sample contract",
    )


def create_sample_output_contract(name="SampleOutputContract"):
    return ToolContract(
        schema_name=name,
        fields=(
            ToolSchemaField(name="results", type_name="list", required=True),
            ToolSchemaField(name="count", type_name="int", required=True),
        ),
        description="Sample output contract",
    )


def create_sample_descriptor(
    tool_id="market_search",
    version="v1",
    capability="MARKET_DISCOVERY",
    status=ToolLifecycleStatus.AVAILABLE,
    side_effect=ToolSideEffectLevel.READ_ONLY,
    channels=(ToolExecutionChannel.MERCADO_LIBRE,),
):
    return ToolDescriptor(
        tool_id=tool_id,
        name="Market Search Tool",
        version=ToolVersion(version),
        description="Searches marketplace products",
        capability=capability,
        input_contract=create_sample_contract(),
        output_contract=create_sample_output_contract(),
        side_effect_level=side_effect,
        required_permissions=("market:read",),
        supported_channels=channels,
        status=status,
        provenance=ToolEvidenceProvenance.LIVE,
        tags=("search", "catalog"),
    )


class TestToolRegistryDomain:

    def test_register_and_retrieve_tool(self):
        registry = ToolRegistry()
        desc = create_sample_descriptor()
        registry.register(desc)

        retrieved = registry.get("market_search")
        assert retrieved is not None
        assert retrieved.tool_id == "market_search"
        assert retrieved.version.version_str == "v1"
        assert retrieved.qualified_id == "market_search:v1"

    def test_duplicate_registration_raises_error(self):
        registry = ToolRegistry()
        desc = create_sample_descriptor()
        registry.register(desc)

        with pytest.raises(ValueError, match="already registered"):
            registry.register(desc)

    def test_version_handling_and_resolution(self):
        registry = ToolRegistry()
        v1 = create_sample_descriptor(version="v1")
        v2 = create_sample_descriptor(version="v2")

        registry.register(v1)
        registry.register(v2)

        # Default get returns the latest registered version
        latest = registry.get("market_search")
        assert latest.version.version_str == "v2"

        # Explicit version get
        got_v1 = registry.get("market_search", version="v1")
        assert got_v1.version.version_str == "v1"

        got_v2 = registry.get("market_search", version="v2")
        assert got_v2.version.version_str == "v2"

        # Inexistent version returns None
        assert registry.get("market_search", version="v3") is None

    def test_list_tools_filtering(self):
        registry = ToolRegistry()
        active = create_sample_descriptor(tool_id="t1", status=ToolLifecycleStatus.AVAILABLE)
        disabled = create_sample_descriptor(tool_id="t2", status=ToolLifecycleStatus.DISABLED)
        deprecated = create_sample_descriptor(tool_id="t3", status=ToolLifecycleStatus.DEPRECATED)

        registry.register(active)
        registry.register(disabled)
        registry.register(deprecated)

        default_list = registry.list_all()
        assert len(default_list) == 1
        assert default_list[0].tool_id == "t1"

        with_disabled = registry.list_all(include_disabled=True)
        assert len(with_disabled) == 2
        assert {t.tool_id for t in with_disabled} == {"t1", "t2"}

        all_tools = registry.list_all(include_disabled=True, include_deprecated=True)
        assert len(all_tools) == 3

    def test_find_by_capability_discovery(self):
        registry = ToolRegistry()
        market_tool = create_sample_descriptor(
            tool_id="market_search",
            capability="MARKET_DISCOVERY",
            channels=(ToolExecutionChannel.MERCADO_LIBRE,),
            side_effect=ToolSideEffectLevel.READ_ONLY,
        )
        supplier_tool = create_sample_descriptor(
            tool_id="supplier_search",
            capability="SUPPLIER_DISCOVERY",
            channels=(ToolExecutionChannel.SUPPLIER_DIRECTORY,),
            side_effect=ToolSideEffectLevel.READ_ONLY,
        )
        publish_tool = create_sample_descriptor(
            tool_id="publisher",
            capability="COMMERCIAL_PUBLICATION",
            channels=(ToolExecutionChannel.MERCADO_LIBRE,),
            side_effect=ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT,
        )

        registry.register(market_tool)
        registry.register(supplier_tool)
        registry.register(publish_tool)

        # 1. Búsqueda por capability
        found_market = registry.find_by_capability("MARKET_DISCOVERY")
        assert len(found_market) == 1
        assert found_market[0].tool_id == "market_search"

        # 2. Filtrado por canal
        found_ml = registry.find_by_capability("COMMERCIAL_PUBLICATION", channel=ToolExecutionChannel.MERCADO_LIBRE)
        assert len(found_ml) == 1

        found_amz = registry.find_by_capability("COMMERCIAL_PUBLICATION", channel=ToolExecutionChannel.AMAZON)
        assert len(found_amz) == 0

        # 3. Filtrado por nivel de seguridad / side_effect
        safe_tools = registry.find_by_capability("COMMERCIAL_PUBLICATION", max_side_effect=ToolSideEffectLevel.READ_ONLY)
        assert len(safe_tools) == 0  # publisher es EXTERNAL_SIDE_EFFECT

    def test_unknown_status_and_disabled_lifecycle(self):
        registry = ToolRegistry()
        unknown_desc = create_sample_descriptor(tool_id="t_unknown", status=ToolLifecycleStatus.UNKNOWN)
        registry.register(unknown_desc)

        # UNKNOWN no es ejecutable
        assert not unknown_desc.is_executable

        # No debe aparecer en find_by_capability
        found = registry.find_by_capability("MARKET_DISCOVERY")
        assert len(found) == 0

    def test_update_lifecycle_status(self):
        registry = ToolRegistry()
        desc = create_sample_descriptor(tool_id="t1", version="v1", status=ToolLifecycleStatus.AVAILABLE)
        registry.register(desc)

        success = registry.update_status("t1", "v1", ToolLifecycleStatus.DISABLED)
        assert success is True

        updated = registry.get("t1", "v1")
        assert updated.status == ToolLifecycleStatus.DISABLED
        assert not updated.is_executable

    def test_input_contract_validation(self):
        contract = ToolContract(
            schema_name="TestContract",
            fields=(
                ToolSchemaField(name="query", type_name="str", required=True),
                ToolSchemaField(name="type", type_name="str", required=False, allowed_values=("A", "B")),
            ),
            allow_extra_fields=False,
        )

        # Válido
        valid, errs = contract.validate({"query": "laptop", "type": "A"})
        assert valid is True
        assert len(errs) == 0

        # Falta campo requerido
        valid, errs = contract.validate({"type": "A"})
        assert valid is False
        assert any("Missing required field: 'query'" in e for e in errs)

        # Valor no permitido
        valid, errs = contract.validate({"query": "laptop", "type": "INVALID"})
        assert valid is False
        assert any("not in allowed values" in e for e in errs)

        # Campo extra no permitido
        valid, errs = contract.validate({"query": "laptop", "unexpected": 123})
        assert valid is False
        assert any("Extra unknown field" in e for e in errs)

    def test_immutability_and_safety_of_descriptors(self):
        desc = create_sample_descriptor()
        assert isinstance(desc.metadata, MappingProxyType)

        with pytest.raises(Exception):
            desc.metadata["new_key"] = "hack"

        with pytest.raises(Exception):
            desc.required_permissions.append("admin")
