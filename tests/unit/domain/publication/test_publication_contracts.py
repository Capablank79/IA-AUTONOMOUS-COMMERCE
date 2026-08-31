import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    PublicationStatus,
    PublicationErrorCategory,
    PublicationError,
    ListingDraft,
    PublicationRequest,
    PublicationResult,
)
from src.domain.publication.ports import (
    PublicationPort,
    PublicationRepository,
)


class TestSalesChannel:
    def test_valid_creation(self):
        channel = SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
            region="CL",
            currency="CLP",
            metadata={"site_id": "MLC"},
        )
        assert channel.channel_id == "CH_MERCADOLIBRE_CL"
        assert channel.channel_type == SalesChannelType.MARKETPLACE
        assert channel.name == "Mercado Libre Chile"
        assert channel.region == "CL"
        assert channel.currency == "CLP"
        assert channel.is_active is True
        assert channel.metadata["site_id"] == "MLC"

    def test_empty_channel_id_raises(self):
        with pytest.raises(ValueError, match="channel_id cannot be empty"):
            SalesChannel(
                channel_id="",
                channel_type=SalesChannelType.MARKETPLACE,
                name="Test",
            )

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name cannot be empty"):
            SalesChannel(
                channel_id="CH-01",
                channel_type=SalesChannelType.DIRECT_STORE,
                name="   ",
            )

    def test_empty_currency_raises(self):
        with pytest.raises(ValueError, match="currency cannot be empty"):
            SalesChannel(
                channel_id="CH-01",
                channel_type=SalesChannelType.DIRECT_STORE,
                name="Test",
                currency="",
            )

    def test_immutability(self):
        channel = SalesChannel(
            channel_id="CH-01",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Test",
        )
        with pytest.raises(Exception):
            channel.name = "Modified"  # type: ignore


class TestPublicationError:
    def test_valid_error_creation(self):
        err = PublicationError(
            category=PublicationErrorCategory.VALIDATION,
            message="Title exceeds maximum allowed length",
            code="ERR_TITLE_TOO_LONG",
            details={"max_length": 60, "actual_length": 75},
            retryable=False,
        )
        assert err.category == PublicationErrorCategory.VALIDATION
        assert err.message == "Title exceeds maximum allowed length"
        assert err.code == "ERR_TITLE_TOO_LONG"
        assert err.retryable is False
        assert err.details["max_length"] == 60

    def test_empty_message_raises(self):
        with pytest.raises(ValueError, match="PublicationError message cannot be empty"):
            PublicationError(
                category=PublicationErrorCategory.UNKNOWN,
                message="   ",
            )

    def test_error_categories(self):
        categories = [
            PublicationErrorCategory.VALIDATION,
            PublicationErrorCategory.AUTHORIZATION,
            PublicationErrorCategory.RATE_LIMIT,
            PublicationErrorCategory.TIMEOUT,
            PublicationErrorCategory.EXTERNAL_SERVICE,
            PublicationErrorCategory.UNKNOWN,
        ]
        for cat in categories:
            err = PublicationError(category=cat, message=f"Test error for {cat.value}")
            assert err.category == cat


class TestListingDraft:
    @pytest.fixture
    def channel(self):
        return SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
        )

    def test_valid_listing_draft_creation(self, channel):
        draft = ListingDraft(
            draft_id="DRAFT-001",
            product_reference_id="PROD-TECH-100",
            title="Auriculares Inalámbricos Bluetooth Pro",
            description="Cancelación de ruido activa, batería 30h",
            price=Decimal("49990"),
            currency="CLP",
            available_quantity=15,
            channel=channel,
            images=("https://cdn.example.com/img1.jpg", "https://cdn.example.com/img2.jpg"),
            attributes={"brand": "SoundPro", "color": "Black"},
            sku="SKU-APRO-BLK",
            category_id="MLC1055",
            condition="new",
            metadata={"origin_mission_id": "MIS-001"},
        )
        assert draft.draft_id == "DRAFT-001"
        assert draft.product_reference_id == "PROD-TECH-100"
        assert draft.title == "Auriculares Inalámbricos Bluetooth Pro"
        assert draft.price == Decimal("49990")
        assert draft.available_quantity == 15
        assert len(draft.images) == 2
        assert draft.attributes["brand"] == "SoundPro"
        assert draft.sku == "SKU-APRO-BLK"
        assert draft.condition == "new"

    def test_invalid_price_raises(self, channel):
        with pytest.raises(ValueError, match="price must be greater than zero"):
            ListingDraft(
                draft_id="DRAFT-001",
                product_reference_id="PROD-1",
                title="Test",
                description="Desc",
                price=Decimal("0"),
                currency="CLP",
                available_quantity=1,
                channel=channel,
            )

        with pytest.raises(ValueError, match="price must be greater than zero"):
            ListingDraft(
                draft_id="DRAFT-001",
                product_reference_id="PROD-1",
                title="Test",
                description="Desc",
                price=Decimal("-100"),
                currency="CLP",
                available_quantity=1,
                channel=channel,
            )

    def test_negative_quantity_raises(self, channel):
        with pytest.raises(ValueError, match="available_quantity cannot be negative"):
            ListingDraft(
                draft_id="DRAFT-001",
                product_reference_id="PROD-1",
                title="Test",
                description="Desc",
                price=Decimal("1000"),
                currency="CLP",
                available_quantity=-5,
                channel=channel,
            )

    def test_empty_identifiers_raise(self, channel):
        with pytest.raises(ValueError, match="draft_id cannot be empty"):
            ListingDraft(
                draft_id="",
                product_reference_id="PROD-1",
                title="Test",
                description="Desc",
                price=Decimal("1000"),
                currency="CLP",
                available_quantity=1,
                channel=channel,
            )

        with pytest.raises(ValueError, match="product_reference_id cannot be empty"):
            ListingDraft(
                draft_id="D-1",
                product_reference_id="",
                title="Test",
                description="Desc",
                price=Decimal("1000"),
                currency="CLP",
                available_quantity=1,
                channel=channel,
            )

        with pytest.raises(ValueError, match="title cannot be empty"):
            ListingDraft(
                draft_id="D-1",
                product_reference_id="P-1",
                title="   ",
                description="Desc",
                price=Decimal("1000"),
                currency="CLP",
                available_quantity=1,
                channel=channel,
            )


class TestPublicationRequest:
    @pytest.fixture
    def channel_ml(self):
        return SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
        )

    @pytest.fixture
    def channel_amz(self):
        return SalesChannel(
            channel_id="CH_AMAZON_US",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Amazon US",
            currency="USD",
        )

    @pytest.fixture
    def draft(self, channel_ml):
        return ListingDraft(
            draft_id="DRAFT-001",
            product_reference_id="PROD-TECH-100",
            title="Auriculares Inalámbricos Bluetooth Pro",
            description="Cancelación de ruido activa",
            price=Decimal("49990"),
            currency="CLP",
            available_quantity=10,
            channel=channel_ml,
        )

    def test_valid_publication_request(self, draft, channel_ml):
        req = PublicationRequest(
            request_id="REQ-PUB-001",
            draft=draft,
            channel=channel_ml,
            idempotency_key="IDEMP-UUID-12345",
            correlation_id="CORR-MIS-999",
            metadata={"initiated_by": "AutonomousLoop"},
        )
        assert req.request_id == "REQ-PUB-001"
        assert req.draft == draft
        assert req.channel == channel_ml
        assert req.idempotency_key == "IDEMP-UUID-12345"
        assert req.correlation_id == "CORR-MIS-999"
        assert req.metadata["initiated_by"] == "AutonomousLoop"

    def test_empty_request_id_raises(self, draft, channel_ml):
        with pytest.raises(ValueError, match="request_id cannot be empty"):
            PublicationRequest(
                request_id="",
                draft=draft,
                channel=channel_ml,
            )

    def test_mismatched_channels_raises(self, draft, channel_amz):
        with pytest.raises(ValueError, match="Draft channel .* must match publication request channel"):
            PublicationRequest(
                request_id="REQ-PUB-001",
                draft=draft,
                channel=channel_amz,
            )


class TestPublicationResult:
    @pytest.fixture
    def channel(self):
        return SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
        )

    def test_published_success_result(self, channel):
        now = datetime.now(timezone.utc)
        res = PublicationResult(
            publication_id="PUB-ML-998877",
            channel=channel,
            status=PublicationStatus.PUBLISHED,
            external_reference="MLC123456789",
            permalink="https://articulo.mercadolibre.cl/MLC-123456789-auriculares",
            published_at=now,
            confidence=Confidence.HIGH,
        )
        assert res.is_success is True
        assert res.is_unknown is False
        assert res.is_failed is False
        assert res.publication_id == "PUB-ML-998877"
        assert res.external_reference == "MLC123456789"
        assert res.permalink.startswith("https://articulo.mercadolibre.cl")

    def test_published_requires_identifier(self, channel):
        with pytest.raises(ValueError, match="PUBLISHED status requires publication_id or external_reference"):
            PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.PUBLISHED,
                external_reference=None,
            )

    def test_failed_result_with_error(self, channel):
        err = PublicationError(
            category=PublicationErrorCategory.RATE_LIMIT,
            message="Rate limit reached for API call",
            code="RATE_LIMIT_EXCEEDED",
            retryable=True,
        )
        res = PublicationResult(
            publication_id=None,
            channel=channel,
            status=PublicationStatus.FAILED,
            errors=(err,),
            confidence=Confidence.HIGH,
        )
        assert res.is_failed is True
        assert res.is_success is False
        assert res.is_unknown is False
        assert len(res.errors) == 1
        assert res.errors[0].category == PublicationErrorCategory.RATE_LIMIT
        assert res.errors[0].retryable is True

    def test_failed_requires_at_least_one_error(self, channel):
        with pytest.raises(ValueError, match="FAILED status requires at least one PublicationError"):
            PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.FAILED,
                errors=(),
            )

    def test_unknown_result_critical_handling(self, channel):
        """
        UNKNOWN no debe convertirse automáticamente en FAILED.
        Representa timeout o respuesta ambigua que requiere verificación/idempotencia.
        """
        res = PublicationResult(
            publication_id=None,
            channel=channel,
            status=PublicationStatus.UNKNOWN,
            external_reference=None,
            errors=(
                PublicationError(
                    category=PublicationErrorCategory.TIMEOUT,
                    message="Network timeout waiting for marketplace acknowledgement",
                    retryable=True,
                ),
            ),
            confidence=Confidence.UNKNOWN,
        )
        assert res.is_unknown is True
        assert res.is_success is False
        assert res.is_failed is False
        assert res.status == PublicationStatus.UNKNOWN


class TestPublicationPortContract:
    def test_mock_adapter_satisfies_contract(self):
        class MockPublicationAdapter(PublicationPort):
            def __init__(self):
                self.publications = {}

            def publish(self, request: PublicationRequest) -> PublicationResult:
                if request.draft.price > Decimal("1000000"):
                    return PublicationResult(
                        publication_id=None,
                        channel=request.channel,
                        status=PublicationStatus.FAILED,
                        errors=(
                            PublicationError(
                                category=PublicationErrorCategory.VALIDATION,
                                message="Price exceeds maximum allowed limit",
                            ),
                        ),
                    )
                pub_res = PublicationResult(
                    publication_id=f"PUB-{request.request_id}",
                    channel=request.channel,
                    status=PublicationStatus.PUBLISHED,
                    external_reference=f"EXT-{request.draft.draft_id}",
                    published_at=datetime.now(timezone.utc),
                )
                self.publications[pub_res.external_reference] = pub_res
                return pub_res

            def get_status(self, channel: SalesChannel, external_reference: str) -> PublicationResult:
                if external_reference in self.publications:
                    return self.publications[external_reference]
                return PublicationResult(
                    publication_id=None,
                    channel=channel,
                    status=PublicationStatus.UNKNOWN,
                    external_reference=external_reference,
                )

        channel = SalesChannel(
            channel_id="CH_TEST",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Test Channel",
        )
        draft = ListingDraft(
            draft_id="D-01",
            product_reference_id="P-01",
            title="Item Test",
            description="Description",
            price=Decimal("15000"),
            currency="CLP",
            available_quantity=5,
            channel=channel,
        )
        req = PublicationRequest(
            request_id="REQ-01",
            draft=draft,
            channel=channel,
            idempotency_key="IDEMP-01",
        )

        adapter: PublicationPort = MockPublicationAdapter()
        res = adapter.publish(req)

        assert isinstance(res, PublicationResult)
        assert res.is_success is True
        assert res.external_reference == "EXT-D-01"

        status_res = adapter.get_status(channel, "EXT-D-01")
        assert status_res.status == PublicationStatus.PUBLISHED
