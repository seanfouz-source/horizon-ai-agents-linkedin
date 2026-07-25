import asyncio
import json

import httpx

from app.config import Settings
from app.metricool import schedule_metricool_payloads


def test_schedule_metricool_payloads_normalizes_media_and_creates_post():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/simpleProfiles":
            return httpx.Response(
                200,
                json=[{"id": 6278196, "userId": 4838974, "label": "Horizon Wireless"}],
            )
        if request.url.path == "/api/actions/normalize/image/url":
            return httpx.Response(200, json="https://static.metricool.com/product.jpg")
        if request.url.path == "/api/v2/scheduler/posts":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": 12345,
                        "providers": body["providers"],
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://app.metricool.com",
        ) as client:
            return await schedule_metricool_payloads(
                [
                    {
                        "publication_date_time": "2026-07-25 09:00:00",
                        "post_content": "Open-box phone. Shop: https://www.ebay.com/itm/1",
                        "media_01": "https://i.ebayimg.com/product.jpg",
                        "facebook": True,
                        "instagram": True,
                        "tiktok": True,
                        "linkedin": True,
                        "product_sku": "EBAY-1",
                        "product_title": "Open Box Phone",
                        "auto_publish": True,
                        "as_draft": False,
                    }
                ],
                settings=Settings(
                    metricool_api_token="token",
                    metricool_blog_id=6278196,
                    metricool_user_id=4838974,
                ),
                client=client,
            )

    results = asyncio.run(run())

    assert results == [
        {
            "index": 0,
            "status": "scheduled",
            "metricool_post_id": 12345,
            "publication_date_time": "2026-07-25 09:00:00",
            "product_sku": "EBAY-1",
            "providers": ["facebook", "instagram", "tiktok", "linkedin"],
        }
    ]
    create_request = next(
        request for request in requests if request.url.path == "/api/v2/scheduler/posts"
    )
    create_body = json.loads(create_request.content)
    assert create_body["publicationDate"] == {
        "dateTime": "2026-07-25T09:00:00",
        "timezone": "America/Chicago",
    }
    assert create_body["media"] == ["https://static.metricool.com/product.jpg"]
    assert create_body["saveExternalMediaFiles"] is True
    assert create_body["autoPublish"] is True
    assert create_body["draft"] is False


def test_schedule_metricool_payloads_reports_item_errors_without_aborting_batch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/actions/normalize/image/url":
            return httpx.Response(400, text="bad media")
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await schedule_metricool_payloads(
                [
                    {
                        "publication_date_time": "2026-07-25 09:00:00",
                        "post_content": "Shop now",
                        "media_01": "https://example.com/bad.jpg",
                        "facebook": True,
                        "product_sku": "EBAY-1",
                    }
                ],
                settings=Settings(
                    metricool_api_token="token",
                    metricool_blog_id=6278196,
                    metricool_user_id=4838974,
                ),
                client=client,
            )

    results = asyncio.run(run())

    assert results[0]["status"] == "failed"
    assert results[0]["product_sku"] == "EBAY-1"
    assert "HTTP 400" in str(results[0]["error"])


def test_schedule_metricool_payloads_accepts_plain_text_normalized_media_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/actions/normalize/image/url":
            return httpx.Response(
                200,
                text="https://static.metricool.com/plain-text-product.jpg",
                headers={"Content-Type": "text/plain"},
            )
        if request.url.path == "/api/v2/scheduler/posts":
            body = json.loads(request.content)
            assert body["media"] == [
                "https://static.metricool.com/plain-text-product.jpg"
            ]
            return httpx.Response(
                200,
                json={"data": {"id": 67890, "providers": body["providers"]}},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await schedule_metricool_payloads(
                [
                    {
                        "publication_date_time": "2026-07-25 11:00:00",
                        "post_content": "Shop now",
                        "media_01": "https://i.ebayimg.com/product.jpg",
                        "facebook": True,
                    }
                ],
                settings=Settings(
                    metricool_api_token="token",
                    metricool_blog_id=6278196,
                    metricool_user_id=4838974,
                ),
                client=client,
            )

    results = asyncio.run(run())

    assert results[0]["status"] == "scheduled"
    assert results[0]["metricool_post_id"] == 67890
