import asyncio
from types import SimpleNamespace

import httpx

import app.walmart as walmart_module
from app.models import InventoryItem, WalmartItemOverride
from app.walmart import (
    WalmartMarketplaceClient,
    build_inventory_feed,
    build_offer_match_preview,
    build_walmart_catalog_query,
    build_walmart_draft,
    select_verified_catalog_match,
)


def test_offer_match_preview_maps_ebay_fields():
    item = InventoryItem(
        sku="EBAY-123",
        title="Samsung Galaxy S25 128GB",
        condition="Open box",
        price=525,
        quantity=2,
        image_url="https://i.ebayimg.com/images/g/demo/s-l1600.jpg",
        item_specifics={"UPC": "887276900123", "Shipping Weight": "24 oz"},
        source="ebay-browse-api",
    )

    preview = build_offer_match_preview([item])

    assert preview["ready"] == 1
    assert preview["blocked"] == 0
    offer = preview["payload"]["MPItem"][0]["Item"]
    assert offer["sku"] == "EBAY-123"
    assert offer["productIdentifiers"] == {"productIdType": "UPC", "productId": "887276900123"}
    assert offer["ShippingWeight"] == 1.5
    assert offer["condition"] == "Open Box"
    assert offer["price"] == 577.5


def test_offer_match_preview_blocks_missing_identifier_and_weight():
    item = InventoryItem(
        sku="EBAY-123",
        title="Phone without Walmart requirements",
        condition="Open box",
        price=100,
        quantity=1,
        source="ebay-store-page",
    )

    preview = build_offer_match_preview([item])

    assert preview["ready"] == 0
    assert preview["blocked"] == 1
    assert preview["payload"]["MPItem"] == []
    assert "Missing a UPC, GTIN, EAN, or ISBN product identifier." in preview["items"][0]["errors"]
    assert "Missing Shipping Weight in pounds." in preview["items"][0]["errors"]


def test_offer_match_preview_accepts_per_sku_overrides():
    item = InventoryItem(
        sku="EBAY-123",
        title="Apple iPhone",
        condition="Used",
        price=350,
        quantity=1,
        image_url="https://i.ebayimg.com/images/g/demo/s-l1600.jpg",
    )
    override = WalmartItemOverride(
        product_id_type="GTIN",
        product_id="00000000000123",
        shipping_weight_lbs=1.25,
        condition="Pre-Owned: Good",
    )

    preview = build_offer_match_preview([item], {item.sku: override})

    assert preview["ready"] == 1
    assert preview["items"][0]["resolved"]["condition"] == "Pre-Owned: Good"
    assert preview["payload"]["MPItem"][0]["Item"]["mainImageUrl"] == item.image_url


def test_offer_match_preview_blocks_required_image_over_url_limit():
    item = InventoryItem(
        sku="EBAY-123",
        title="Pre-owned phone",
        condition="Used - Good",
        price=250,
        quantity=1,
        image_url="https://example.com/" + ("x" * 190) + ".jpg",
        item_specifics={"UPC": "887276900123", "Shipping Weight": "1 lb"},
    )

    preview = build_offer_match_preview([item])

    assert preview["ready"] == 0
    assert "Main image URL exceeds the MP_ITEM_MATCH v4.2 limit" in preview["items"][0]["errors"][0]


def test_inventory_feed_includes_zero_quantity_for_ended_listings():
    payload = build_inventory_feed(
        [
            InventoryItem(sku="EBAY-LIVE", title="Live", quantity=2),
            InventoryItem(sku="EBAY-ENDED", title="Ended", quantity=0, listing_status="ENDED"),
        ]
    )

    assert payload["InventoryHeader"] == {"version": "1.4"}
    assert [row["quantity"]["amount"] for row in payload["Inventory"]] == [2, 0]


def test_walmart_draft_preserves_ebay_data_without_inventing_identifier():
    item = InventoryItem(
        sku="EBAY-123-GRAY",
        ebay_item_id="123",
        title="Samsung Galaxy Z Flip5 512GB Gray Unlocked",
        description="Open-box phone with original packaging.",
        condition="Open box",
        price=449,
        quantity=5,
        image_url="https://i.ebayimg.com/images/g/demo/s-l1600.jpg",
        category="Cell Phones & Accessories:Cell Phones & Smartphones",
        item_specifics={
            "Brand": "Samsung",
            "Model": "Samsung Galaxy Z Flip5",
            "Storage": "512 GB",
            "Color": "Gray",
            "Shipping Weight": "0.5 lb",
        },
        source="ebay-trading-api",
    )
    catalog = {
        "status": "candidates_found",
        "query": build_walmart_catalog_query(item),
        "candidates": [{"walmart_item_id": "987", "title": item.title, "identifiers": {}}],
    }

    draft = build_walmart_draft(item, catalog)

    assert draft["status"] == "draft_needs_review"
    assert draft["prepared_listing"]["shipping_weight_lbs"] == 0.5
    assert draft["prepared_listing"]["condition"] == "Open Box"
    assert draft["prepared_listing"]["price"] == 493.9
    assert draft["prepared_listing"]["source_price"] == 449
    assert draft["prepared_listing"]["price_markup_percent"] == 10.0
    assert draft["prepared_listing"]["product_identifier"] is None
    assert draft["catalog_candidates"][0]["walmart_item_id"] == "987"
    assert "product_identifier" in draft["missing_fields"]


def test_verified_catalog_match_requires_unique_exact_variant():
    item = InventoryItem(
        sku="EBAY-123-GRAY",
        title="Samsung Galaxy Z Flip5 512GB Gray Unlocked",
        category="Cell Phones & Accessories:Cell Phones & Smartphones",
        item_specifics={
            "Brand": "Samsung",
            "Model": "Samsung Galaxy Z Flip5",
            "Storage": "512 GB",
            "Color": "Gray",
        },
    )
    candidates = [
        {
            "walmart_item_id": "987",
            "title": "Samsung Galaxy Z Flip5 512GB Gray Factory Unlocked",
            "brand": "Samsung",
            "identifiers": {"GTIN": "00887276900123"},
        },
        {
            "walmart_item_id": "654",
            "title": "Samsung Galaxy Z Flip5 256GB Gray Factory Unlocked",
            "brand": "Samsung",
            "identifiers": {"GTIN": "00887276900456"},
        },
    ]

    match, reason = select_verified_catalog_match(item, candidates)

    assert match is not None
    assert match["walmart_item_id"] == "987"
    assert match["product_id"] == "00887276900123"
    assert "Exactly one" in reason


def test_verified_catalog_match_rejects_ambiguous_results():
    item = InventoryItem(
        sku="EBAY-123",
        title="Apple Watch Series 11 42mm Black",
        category="Cell Phones & Accessories:Smart Watches",
        item_specifics={
            "Brand": "Apple",
            "Model": "Apple Watch Series 11",
            "Size": "42mm",
            "Color": "Black",
        },
    )
    candidate = {
        "title": "Apple Watch Series 11 42mm Black GPS + Cellular",
        "brand": "Apple",
        "identifiers": {"GTIN": "00000000000123"},
    }

    match, reason = select_verified_catalog_match(item, [candidate, dict(candidate)])

    assert match is None
    assert reason.startswith("2 catalog candidates")


class FakeAsyncClient:
    def __init__(self, handler, *args, **kwargs):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, path, data=None, headers=None):
        return self.handler("POST", path, data=data, headers=headers or {})

    async def request(self, method, path, headers=None, **kwargs):
        return self.handler(method, path, headers=headers or {}, **kwargs)


def _settings():
    return SimpleNamespace(
        walmart_client_id="client-id",
        walmart_client_secret="client-secret",
        walmart_api_base_url="https://marketplace.walmartapis.com",
        walmart_service_name="Walmart Marketplace",
        walmart_market="us",
        walmart_channel_type=None,
    )


def test_walmart_client_authenticates_and_submits_match_feed(monkeypatch):
    requests = []

    def handler(method, path, headers, **kwargs):
        requests.append((method, path, headers, kwargs))
        request = httpx.Request(method, f"https://marketplace.walmartapis.com{path}", headers=headers)
        if path == "/v3/token":
            assert headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 900}, request=request)
        if path == "/v3/feeds":
            assert headers["WM_SEC.ACCESS_TOKEN"] == "access-token"
            assert kwargs["params"] == {"feedType": "MP_ITEM_MATCH"}
            return httpx.Response(200, json={"feedId": "FEED@123"}, request=request)
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )
    client = WalmartMarketplaceClient(_settings())

    result = asyncio.run(
        client.submit_offer_match_feed(
            {
                "MPItemFeedHeader": {"version": "4.2"},
                "MPItem": [{"Item": {"sku": "EBAY-123"}}],
            }
        )
    )

    assert result["feed_id"] == "FEED@123"
    assert [request[1] for request in requests] == ["/v3/token", "/v3/feeds"]


def test_walmart_catalog_search_reports_match(monkeypatch):
    def handler(method, path, headers, **kwargs):
        request = httpx.Request(method, f"https://marketplace.walmartapis.com{path}", headers=headers)
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/items/walmart/search":
            assert kwargs["params"] == {"upc": "887276900123", "responseFormat": "SPEC"}
            return httpx.Response(
                200,
                json={"items": [{"feedType": "MP_ITEM_MATCH", "version": "4.2"}]},
                request=request,
            )
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )

    result = asyncio.run(WalmartMarketplaceClient(_settings()).search_catalog("UPC", "887276900123"))

    assert result["matched"] is True
    assert result["feed_type"] == "MP_ITEM_MATCH"


def test_walmart_catalog_keyword_search_returns_sanitized_candidates(monkeypatch):
    def handler(method, path, headers, **kwargs):
        request = httpx.Request(method, f"https://marketplace.walmartapis.com{path}", headers=headers)
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/items/walmart/search":
            assert kwargs["params"] == {
                "query": "Samsung Galaxy Z Flip5 512 GB Gray",
                "responseFormat": "DEFAULT",
            }
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "itemId": "987",
                            "productName": "Samsung Galaxy Z Flip5 512GB",
                            "brand": "Samsung",
                            "productType": "Cell Phones",
                            "gtin": "00887276900123",
                            "irrelevantInternalField": "not persisted",
                        }
                    ]
                },
                request=request,
            )
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )

    result = asyncio.run(
        WalmartMarketplaceClient(_settings()).search_catalog_by_query(
            "Samsung Galaxy Z Flip5 512 GB Gray"
        )
    )

    assert result["status"] == "candidates_found"
    assert result["candidates"] == [
        {
            "walmart_item_id": "987",
            "title": "Samsung Galaxy Z Flip5 512GB",
            "brand": "Samsung",
            "product_type": "Cell Phones",
            "category_path": None,
            "image_url": None,
            "identifiers": {"GTIN": "00887276900123"},
        }
    ]


def test_walmart_client_reads_published_items_and_inventory(monkeypatch):
    def handler(method, path, headers, **kwargs):
        request = httpx.Request(method, f"https://marketplace.walmartapis.com{path}", headers=headers)
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/items":
            assert kwargs["params"] == {
                "publishedStatus": "PUBLISHED",
                "lifecycleStatus": "ACTIVE",
                "limit": 1000,
            }
            return httpx.Response(
                200,
                json={
                    "ItemResponse": [
                        {
                            "sku": "PHONE-1",
                            "publishedStatus": "PUBLISHED",
                            "lifecycleStatus": "ACTIVE",
                        }
                    ]
                },
                request=request,
            )
        if path == "/v3/inventory":
            assert kwargs["params"] == {"sku": "PHONE-1"}
            return httpx.Response(
                200,
                json={"sku": "PHONE-1", "quantity": {"unit": "EACH", "amount": 4}},
                request=request,
            )
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )
    client = WalmartMarketplaceClient(_settings())

    items = asyncio.run(client.list_published_items())
    quantity = asyncio.run(client.get_inventory_quantity("PHONE-1"))

    assert items == [
        {
            "sku": "PHONE-1",
            "published_status": "PUBLISHED",
            "lifecycle_status": "ACTIVE",
        }
    ]
    assert quantity == 4
