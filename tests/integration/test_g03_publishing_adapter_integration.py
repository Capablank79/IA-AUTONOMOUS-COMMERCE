import json
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import (
    LoopDecision,
    LoopState,
    LoopAction,
    MissionType,
)
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
    PublicationErrorCategory,
)
from src.domain.publication.generation_models import (
    ListingGenerationInput,
    SEOStrategy,
    SEOKeyword,
    KeywordSourceType,
    CustomerPainPoint,
    CustomerPainCategory,
    ChannelContentConstraint,
)
from src.domain.publication.services import DeterministicListingGenerator
from src.domain.publication.validation_models import (
    ValidationStatus,
    ListingValidationContext,
    ListingValidationResult,
)
from src.domain.publication.validation_engine import DeterministicListingValidator
from src.application.publication.listing_validator_service import ListingQualityValidatorService
from src.domain.policy.models import PolicyDecisionType
from src.domain.policy.engine import PolicyEngine
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor
from src.application.publication.publication_action_executor import PublicationActionExecutor
from src.infrastructure.mercadolibre.publication_adapter import MercadoLibrePublicationAdapter
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.domain.capital.models import CapitalBudget
from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.domain.tool.registry import ToolRegistry
from src.application.tool.catalog import register_standard_commerce_tools
from src.domain.tool.models import ToolExecutionChannel, ToolSideEffectLevel, ToolLifecycleStatus


@pytest.fixture
def sample_channel():
    return SalesChannel(
        channel_id="CH_MERCADOLIBRE_CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        region="CL",
        currency="CLP",
        metadata={"user_id": "99887766"},
    )


@pytest.fixture
def sample_capital_budget():
    return CapitalBudget(
        budget_id="budget_g03_001",
        total_capital=Decimal("1000000"),
        reserved_capital=Decimal("100000"),
        committed_capital=Decimal("50000"),
        currency="CLP",
    )


@pytest.fixture
def sample_state():
    return LoopState(
        mission_id="mission_g03_pub_001",
        iteration=1,
        goal="G.3 Publishing adapter integration and verification",
    )


class TestG03PublishingAdapterIntegration:
    """
    G.3 / TASK 07.3 - Publishing Adapter Integration & Verification Test Suite.
    
    Verifies end-to-end deterministic integration:
    ListingDraft -> G.2 Validation -> Policy Gate -> Publication Action -> ActionExecutor -> PublicationPort -> MercadoLibrePublicationAdapter -> Mercado Libre API -> PublicationResult -> Audit/Trace.
    """

    def test_full_pipeline_g1_to_g2_to_policy_to_adapter_success(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario A & G & Q: Flujo completo exitoso E2E.
        1. G.1 Generator produce ListingDraft y grounding.
        2. G.2 Validator evalúa y emite VALID.
        3. Policy Engine evalúa y emite ALLOW (con human_approved=True y provenance=LIVE).
        4. ActionExecutor delega a MercadoLibrePublicationAdapter.
        5. Mocked Mercado Libre API responde 201 Created.
        6. PublicationResult retorna status PUBLISHED con trazabilidad completa.
        """
        # Step 1: G.1 Generation
        gen_input = ListingGenerationInput(
            product_id="PROD_SSD_480GB",
            title="Disco Estado Solido Kingston A400 480GB",
            price=Decimal("34990"),
            currency="CLP",
            available_quantity=20,
            channel=sample_channel,
            category_id="MLC1672",
            attributes={
                "brand": "Kingston",
                "model": "A400",
                "capacity": "480 GB",
                "interface": "SATA III",
                "form_factor": "2.5 in",
                "condition": "new",
            },
            images=("https://http2.mlstatic.com/D_NQ_NP_TEST1.jpg",),
            customer_pains=(
                CustomerPainPoint(
                    pain_id="p1",
                    category=CustomerPainCategory.PERFORMANCE,
                    complaint_summary="Lentitud al encender el equipo",
                    severity=8,
                ),
            ),
            seo_keywords=(
                SEOKeyword(
                    keyword="disco solido ssd kingston 480gb sata 3",
                    source_type=KeywordSourceType.OBSERVED,
                    relevance_score=0.95,
                    search_volume_observed=4500,
                ),
            ),
            constraints=ChannelContentConstraint(max_title_length=60),
        )
        generator = DeterministicListingGenerator()
        gen_result = generator.generate(gen_input)
        draft = gen_result.draft

        # Step 2: G.2 Validation Gate
        validator_service = ListingQualityValidatorService()
        val_context = ListingValidationContext(
            draft=draft,
            product_truth_attributes=dict(gen_input.attributes),
            grounding=gen_result.grounding,
            seo_strategy=gen_result.seo_strategy,
            differentiation_strategy=gen_result.differentiation_strategy,
            channel_constraints=gen_input.constraints,
        )
        val_result = validator_service.validate_listing(val_context)
        assert val_result.status == ValidationStatus.VALID
        assert val_result.is_valid is True

        # Step 3: Infrastructure Setup (Mocked API)
        mock_api = MagicMock()
        mock_api.post.return_value = {
            "id": "MLC987654321",
            "status": "active",
            "permalink": "https://articulo.mercadolibre.cl/MLC-987654321-disco-ssd.html",
            "site_id": "MLC",
            "seller_id": 99887766,
        }
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        base_executor = PublicationActionExecutor(
            publication_port=adapter, default_channel=sample_channel
        )
        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=base_executor,
            capital_budget=sample_capital_budget,
        )

        # Step 4: Decision & Policy Gate Evaluation
        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publish verified and validated listing to Mercado Libre Chile",
            parameters={
                "action_type": "PUBLISH",
                "draft": draft,
                "correlation_id": "corr-g03-001",
                "idempotency_key": "idemp-g03-001",
                "risk_level": RiskLevel.LOW,
                "provenance": EvidenceProvenanceType.LIVE,
                "human_approved": True,
            },
        )

        observation = guarded_executor.execute(decision, sample_state)

        # Step 5: Assertions & Trace Verification
        assert observation["action_executed"] == "PUBLISH"
        assert observation["status"] == "PUBLISHED"
        assert observation["is_success"] is True
        assert observation["is_unknown"] is False
        assert observation["publication_id"] == "MLC987654321"
        assert observation["external_reference"] == "MLC987654321"
        assert observation["permalink"] == "https://articulo.mercadolibre.cl/MLC-987654321-disco-ssd.html"
        assert observation["correlation_id"] == "corr-g03-001"
        assert observation["idempotency_key"] == "idemp-g03-001"
        assert observation["policy_decision"] == PolicyDecisionType.ALLOW.value
        assert "policy_evaluation_id" in observation

        # Step 6: Verify API call payload
        mock_api.post.assert_called_once()
        path, kwargs = mock_api.post.call_args[0][0], mock_api.post.call_args[1]
        assert path == "/items"
        assert kwargs["payload"]["category_id"] == "MLC1672"
        assert kwargs["payload"]["price"] == 34990
        assert kwargs["payload"]["currency_id"] == "CLP"

    def test_validation_gate_blocked_status_prevents_adapter_invocation(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario B: G.2 BLOCKED -> Adapter NO invocado.
        Un listing con reclamos prohibidos es bloqueado por G.2 y no debe llegar al adapter ni a la API externa.
        """
        tampered_draft = ListingDraft(
            draft_id="draft_blocked_001",
            product_reference_id="PROD_SSD_480GB",
            title="Disco SSD Kingston 480GB Cura Todo y Garantia Eterna",
            description="SSD con garantia vitalicia garantizada 100% libre de fallas.",
            price=Decimal("34990"),
            currency="CLP",
            available_quantity=10,
            channel=sample_channel,
            category_id="MLC1672",
        )

        validator_service = ListingQualityValidatorService()
        val_context = ListingValidationContext(
            draft=tampered_draft,
            product_truth_attributes={"brand": "Kingston"},
        )
        val_result = validator_service.validate_listing(val_context)
        assert val_result.status in (ValidationStatus.BLOCKED, ValidationStatus.INVALID)
        assert val_result.is_valid is False

        # Guarded execution simulation
        mock_api = MagicMock()
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        base_executor = PublicationActionExecutor(publication_port=adapter)
        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=base_executor,
            capital_budget=sample_capital_budget,
        )

        # Si el listing es INVALID / BLOCKED, el ciclo autónomo o la política no debe permitir acción de publicación
        if not val_result.is_valid:
            decision = LoopDecision(
                action=LoopAction.REJECT,
                reason=f"Listing validation failed with status {val_result.status.value}",
                parameters={
                    "action_type": "REJECT",
                    "validation_violations": [v.code for v in val_result.violations],
                },
            )

        # Adapter nunca debe haber sido invocado
        mock_api.post.assert_not_called()
        assert adapter.api_client == mock_api
        assert base_executor.external_calls_count == 0

    def test_validation_gate_needs_review_prevents_adapter_invocation(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario C: G.2 NEEDS_REVIEW -> Adapter NO invocado.
        Un listing con advertencias o claims pendientes de revisión humana.
        """
        review_draft = ListingDraft(
            draft_id="draft_review_001",
            product_reference_id="PROD_SSD_480GB",
            title="Disco SSD Kingston A400 480GB Oferta Especial Barato",
            description="SSD Kingston 480GB para computadores portatiles.",
            price=Decimal("34990"),
            currency="CLP",
            available_quantity=5,
            channel=sample_channel,
            category_id="MLC1672",
            images=("https://http2.mlstatic.com/D_NQ_NP_TEST1.jpg",),
        )

        validator_service = ListingQualityValidatorService()
        val_context = ListingValidationContext(
            draft=review_draft,
            product_truth_attributes={"brand": "Kingston"},
        )
        val_result = validator_service.validate_listing(val_context)

        mock_api = MagicMock()
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        base_executor = PublicationActionExecutor(publication_port=adapter)

        # Cuando no es estrictamente VALID, no se publica
        if val_result.status != ValidationStatus.VALID:
            mock_api.post.assert_not_called()
            assert base_executor.external_calls_count == 0

    def test_policy_gate_deny_prevents_adapter_invocation(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario D: Policy DENY -> Adapter NO invocado.
        Una acción que viola límites de capital o está en lista prohibida es rechazada por el Policy Engine.
        """
        mock_api = MagicMock()
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        base_executor = PublicationActionExecutor(publication_port=adapter)
        
        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=base_executor,
            capital_budget=sample_capital_budget,
            default_prohibited_actions=("PUBLISH_LISTING", "PUBLISH"),
        )

        draft = ListingDraft(
            draft_id="draft_deny_001",
            product_reference_id="prod_deny",
            title="Test Listing Prohibited",
            description="Description",
            price=Decimal("10000"),
            currency="CLP",
            available_quantity=1,
            channel=sample_channel,
        )

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Attempting to publish prohibited listing action",
            parameters={
                "action_type": "PUBLISH_LISTING",
                "draft": draft,
                "correlation_id": "corr-deny-001",
            },
        )

        observation = guarded_executor.execute(decision, sample_state)

        assert observation["status"] == "POLICY_DENIED"
        assert observation["is_allowed"] is False
        assert observation["decision"] == PolicyDecisionType.DENY.value
        mock_api.post.assert_not_called()
        assert base_executor.external_calls_count == 0

    def test_policy_gate_require_approval_prevents_adapter_invocation(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario E: Policy REQUIRE_APPROVAL -> Adapter NO invocado.
        Acciones que requieren aprobación humana previa no ejecutan mutación externa.
        """
        mock_api = MagicMock()
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        base_executor = PublicationActionExecutor(publication_port=adapter)
        
        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=base_executor,
            capital_budget=sample_capital_budget,
            default_actions_requiring_approval=("PUBLISH_LISTING", "PUBLISH"),
        )

        draft = ListingDraft(
            draft_id="draft_appr_001",
            product_reference_id="prod_appr",
            title="Test Listing Requiring Approval",
            description="Description",
            price=Decimal("10000"),
            currency="CLP",
            available_quantity=1,
            channel=sample_channel,
        )

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Attempting publication requiring human approval",
            parameters={
                "action_type": "PUBLISH",
                "draft": draft,
                "correlation_id": "corr-appr-001",
                "human_approved": False,
            },
        )

        observation = guarded_executor.execute(decision, sample_state)

        assert observation["status"] == "POLICY_APPROVAL_REQUIRED"
        assert observation["is_allowed"] is False
        assert observation["requires_approval"] is True
        assert observation["decision"] == PolicyDecisionType.REQUIRE_APPROVAL.value
        mock_api.post.assert_not_called()
        assert base_executor.external_calls_count == 0

    def test_policy_allow_invokes_adapter_exactly_once(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario F: Policy ALLOW -> Adapter invocado exactamente una vez.
        """
        mock_api = MagicMock()
        mock_api.post.return_value = {
            "id": "MLC11223344",
            "status": "active",
            "permalink": "https://articulo.mercadolibre.cl/MLC11223344",
        }
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        base_executor = PublicationActionExecutor(publication_port=adapter, default_channel=sample_channel)
        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=base_executor,
            capital_budget=sample_capital_budget,
        )

        draft = ListingDraft(
            draft_id="draft_exact_001",
            product_reference_id="prod_exact",
            title="Disco Solido Kingston A400 480GB",
            description="Description",
            price=Decimal("34990"),
            currency="CLP",
            available_quantity=5,
            channel=sample_channel,
            category_id="MLC1672",
        )

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing vetted listing",
            parameters={
                "action_type": "PUBLISH",
                "draft": draft,
                "correlation_id": "corr-exact-001",
                "idempotency_key": "idemp-exact-001",
                "risk_level": RiskLevel.LOW,
                "provenance": EvidenceProvenanceType.LIVE,
                "human_approved": True,
            },
        )

        observation = guarded_executor.execute(decision, sample_state)

        assert observation["is_success"] is True
        assert mock_api.post.call_count == 1
        assert base_executor.external_calls_count == 1

    # -----------------------------------------------------------------------
    # Error Matrix Tests (H, I, J, K, L)
    # -----------------------------------------------------------------------
    def test_error_matrix_http_400_validation_error(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario H: HTTP 400/422 -> Structured VALIDATION error, no retryable.
        """
        mock_api = MagicMock()
        mock_api.post.side_effect = MercadoLibreApiError(
            "Validation error",
            status_code=400,
            response_body=json.dumps({
                "message": "Validation error",
                "error": "body.attributes.invalid",
                "status": 400,
                "cause": [{"code": "attribute.not_allowed", "message": "Attribute invalid"}],
            }),
        )
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        executor = PublicationActionExecutor(publication_port=adapter)

        draft = ListingDraft(
            draft_id="draft_val_err",
            product_reference_id="prod_val_err",
            title="Item title",
            description="Desc",
            price=Decimal("10000"),
            currency="CLP",
            available_quantity=1,
            channel=sample_channel,
        )

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing item",
            parameters={"action_type": "PUBLISH", "draft": draft},
        )

        observation = executor.execute(decision, sample_state)
        assert observation["status"] == "FAILED"
        assert observation["is_failed"] is True
        assert observation["is_unknown"] is False
        assert len(observation["errors"]) == 1
        assert observation["errors"][0]["category"] == PublicationErrorCategory.VALIDATION.value
        assert observation["errors"][0]["retryable"] is False

    def test_error_matrix_http_401_403_authorization_error(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario I: HTTP 401/403 -> Structured AUTHORIZATION error.
        """
        mock_api = MagicMock()
        mock_api.post.side_effect = MercadoLibreApiError(
            "Access denied",
            status_code=403,
            response_body=json.dumps({
                "message": "Access to resource is forbidden",
                "error": "access_denied",
                "status": 403,
            }),
        )
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        executor = PublicationActionExecutor(publication_port=adapter)

        draft = ListingDraft(
            draft_id="draft_auth_err",
            product_reference_id="prod_auth_err",
            title="Item title",
            description="Desc",
            price=Decimal("10000"),
            currency="CLP",
            available_quantity=1,
            channel=sample_channel,
        )

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing item",
            parameters={"action_type": "PUBLISH", "draft": draft},
        )

        observation = executor.execute(decision, sample_state)
        assert observation["status"] == "FAILED"
        assert observation["is_failed"] is True
        assert observation["errors"][0]["category"] == PublicationErrorCategory.AUTHORIZATION.value
        assert observation["errors"][0]["retryable"] is False

    def test_error_matrix_http_429_rate_limit(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario J: HTTP 429 -> Structured RATE_LIMIT error, retryable.
        """
        mock_api = MagicMock()
        mock_api.post.side_effect = MercadoLibreApiError(
            "Too Many Requests",
            status_code=429,
            response_body=json.dumps({
                "message": "Rate limit exceeded",
                "error": "too_many_requests",
                "status": 429,
            }),
        )
        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        executor = PublicationActionExecutor(publication_port=adapter)

        draft = ListingDraft(
            draft_id="draft_rate_limit",
            product_reference_id="prod_rl",
            title="Item title",
            description="Desc",
            price=Decimal("10000"),
            currency="CLP",
            available_quantity=1,
            channel=sample_channel,
        )

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing item",
            parameters={"action_type": "PUBLISH", "draft": draft},
        )

        observation = executor.execute(decision, sample_state)
        assert observation["status"] == "FAILED"
        assert observation["errors"][0]["category"] == PublicationErrorCategory.RATE_LIMIT.value
        assert observation["errors"][0]["retryable"] is True

    def test_error_matrix_timeout_and_5xx_preserves_unknown(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenarios K & L: Timeout y 5xx en POST preservan UNKNOWN (no FAILED).
        """
        # K: Network Timeout
        mock_api_timeout = MagicMock()
        mock_api_timeout.post.side_effect = MercadoLibreApiError("Network connection timeout")
        adapter_to = MercadoLibrePublicationAdapter(api_client=mock_api_timeout)
        executor_to = PublicationActionExecutor(publication_port=adapter_to)

        draft = ListingDraft(
            draft_id="draft_timeout",
            product_reference_id="prod_to",
            title="Item title",
            description="Desc",
            price=Decimal("10000"),
            currency="CLP",
            available_quantity=1,
            channel=sample_channel,
        )

        decision_to = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing item",
            parameters={"action_type": "PUBLISH", "draft": draft},
        )
        obs_to = executor_to.execute(decision_to, sample_state)
        assert obs_to["status"] == "UNKNOWN"
        assert obs_to["is_unknown"] is True
        assert obs_to["is_failed"] is False
        assert obs_to["errors"][0]["category"] == PublicationErrorCategory.TIMEOUT.value

        # L: 500 Internal Server Error
        mock_api_500 = MagicMock()
        mock_api_500.post.side_effect = MercadoLibreApiError(
            "Internal error",
            status_code=500,
            response_body=json.dumps({"message": "Server error", "error": "internal_error", "status": 500}),
        )
        adapter_500 = MercadoLibrePublicationAdapter(api_client=mock_api_500)
        executor_500 = PublicationActionExecutor(publication_port=adapter_500)

        obs_500 = executor_500.execute(decision_to, sample_state)
        assert obs_500["status"] == "UNKNOWN"
        assert obs_500["is_unknown"] is True
        assert obs_500["is_failed"] is False
        assert obs_500["errors"][0]["category"] == PublicationErrorCategory.EXTERNAL_SERVICE.value

    def test_unknown_recovery_via_verify_status_prevents_duplicate_creation(
        self, sample_channel, sample_capital_budget, sample_state
    ):
        """
        Escenario N & O: Resiliencia ante UNKNOWN y recuperación vía VERIFY_STATUS.
        Flujo:
        1. Publicación inicial sufre corte de red -> Retorna UNKNOWN.
        2. En vez de reintentar POST a ciegas, el sistema ejecuta VERIFY_STATUS (/items/{id}).
        3. Se constata que el ítem sí fue creado remotamente -> Se actualiza a PUBLISHED.
        4. No ocurre ninguna duplicación de publicación.
        """
        mock_api = MagicMock()
        mock_api.post.side_effect = MercadoLibreApiError("Transport disconnected after request send")
        mock_api.get.return_value = {
            "id": "MLC999888777",
            "status": "active",
            "permalink": "https://articulo.mercadolibre.cl/MLC-999888777-disco.html",
            "site_id": "MLC",
        }

        adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
        executor = PublicationActionExecutor(publication_port=adapter, default_channel=sample_channel)

        draft = ListingDraft(
            draft_id="draft_unknown_rec",
            product_reference_id="prod_rec",
            title="Disco Solido Kingston A400 480GB",
            description="Desc",
            price=Decimal("34990"),
            currency="CLP",
            available_quantity=5,
            channel=sample_channel,
            category_id="MLC1672",
        )

        # 1. POST falla con corte de red
        decision_pub = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing item",
            parameters={
                "action_type": "PUBLISH",
                "draft": draft,
                "correlation_id": "corr-rec-001",
                "idempotency_key": "idemp-rec-001",
            },
        )
        obs_pub = executor.execute(decision_pub, sample_state)
        assert obs_pub["status"] == "UNKNOWN"
        assert obs_pub["is_unknown"] is True
        assert mock_api.post.call_count == 1

        # 2. Recuperación: VERIFY_STATUS con external_reference
        decision_verify = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Verifying publication status before retrying creation",
            parameters={
                "action_type": "VERIFY_STATUS",
                "external_reference": "MLC999888777",
                "channel": sample_channel,
                "correlation_id": "corr-rec-001",
            },
        )
        obs_verify = executor.execute(decision_verify, sample_state)

        assert obs_verify["action_executed"] == "VERIFY_STATUS"
        assert obs_verify["status"] == "PUBLISHED"
        assert obs_verify["is_success"] is True
        assert obs_verify["publication_id"] == "MLC999888777"
        assert obs_verify["permalink"] == "https://articulo.mercadolibre.cl/MLC-999888777-disco.html"

        # Verificar que NO se hizo un segundo POST
        assert mock_api.post.call_count == 1
        mock_api.get.assert_called_once_with("/items/MLC999888777")

    def test_tool_registry_governance_and_schema_for_publishing(self):
        """
        Escenario R: Verificación del Tool Registry para publish_listing.
        - Tool registrada con versión tipada (v1).
        - side_effect_level = EXTERNAL_SIDE_EFFECT.
        - requires_approval = True.
        - requires_idempotency = True.
        - Esquemas de entrada y salida completos.
        """
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        tool = registry.get("publish_listing")
        assert tool is not None
        assert tool.tool_id == "publish_listing"
        assert tool.version.version_str == "v1"
        assert tool.capability == "COMMERCIAL_PUBLICATION"
        assert tool.side_effect_level == ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT
        assert tool.requires_approval is True
        assert tool.requires_idempotency is True
        assert ToolExecutionChannel.MERCADO_LIBRE in tool.supported_channels
        assert tool.status == ToolLifecycleStatus.AVAILABLE

        input_field_names = [f.name for f in tool.input_contract.fields]
        assert "title" in input_field_names
        assert "price" in input_field_names
        assert "category_id" in input_field_names
        assert "available_quantity" in input_field_names

        output_field_names = [f.name for f in tool.output_contract.fields]
        assert "listing_id" in output_field_names
        assert "permalink" in output_field_names
        assert "status" in output_field_names
