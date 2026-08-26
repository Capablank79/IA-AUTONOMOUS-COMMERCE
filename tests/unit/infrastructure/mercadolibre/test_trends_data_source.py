from unittest.mock import Mock

from src.infrastructure.mercadolibre.trends_data_source import (
    MercadoLibreTrendsDataSource,
)


def test_get_trends_maps_keyword_url_and_rank():
    api_client = Mock()
    api_client.get.return_value = [
        {"keyword": "aspiradora", "url": "https://example.com/aspiradora"},
        {"keyword": "linterna", "url": "https://example.com/linterna"},
    ]

    data_source = MercadoLibreTrendsDataSource(api_client)

    result = data_source.get_trends()

    assert result == [
        {
            "keyword": "aspiradora",
            "url": "https://example.com/aspiradora",
            "rank": 1,
        },
        {
            "keyword": "linterna",
            "url": "https://example.com/linterna",
            "rank": 2,
        },
    ]

    api_client.get.assert_called_once_with("/trends/MLC")
