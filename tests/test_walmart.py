import asyncio
from types import SimpleNamespace

import httpx

import app.walmart as walmart_module
from app.models import InventoryItem, WalmartItemOverride
from app.walmart import (
    WalmartMarketplaceClient,
    build_inventory_feed,
    build_item_image_maintenance_feed,
    build_offer_match_preview,
    build_walmart_catalog_query,
    build_walmart_draft,
    estimated_shipping_weight_lbs,
    parse_walmart_product_page,
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
        item_specifics={"UPC": "887276900124", "Shipping Weight": "24 oz"},
        source="ebay-browse-api",
    )

    preview = build_offer_match_preview([item])

    assert preview["ready"] == 1
    assert preview["blocked"] == 0
    offer = preview["payload"]["MPItem"][0]["Item"]
    assert offer["sku"] == "EBAY-123"
    assert offer["productIdentifiers"] == {"productIdType": "UPC", "productId": "887276900124"}
    assert offer["ShippingWeight"] == 1.5
    assert offer["condition"] == "Open Box"
    assert offer["price"] == 577.5


def test_offer_match_preview_maps_ebay_open_box_api_codes():
    for source_condition in ("NEW_OTHER", "1500"):
        item = InventoryItem(
            sku=f"EBAY-{source_condition}",
            title="Samsung Galaxy S25 128GB",
            condition=source_condition,
            price=525,
            quantity=2,
            image_url="https://i.ebayimg.com/images/g/demo/s-l1600.jpg",
            item_specifics={"UPC": "887276900124", "Shipping Weight": "24 oz"},
            source="ebay-api",
        )

        preview = build_offer_match_preview([item])

        assert preview["ready"] == 1
        assert preview["items"][0]["resolved"]["condition"] == "Open Box"


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
        item_specifics={"UPC": "887276900124", "Shipping Weight": "1 lb"},
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


def test_item_image_maintenance_feed_matches_current_walmart_schema():
    payload = build_item_image_maintenance_feed(
        [
            {
                "sku": "PHONE-1",
                "product_type": "Cell Phones",
                "product_id_type": "GTIN",
                "product_id": "00123456789012",
                "image_urls": [
                    "https://i.ebayimg.com/images/g/new-main/s-l1600.jpg",
                    "https://i.ebayimg.com/images/g/new-back/s-l1600.jpg",
                ],
            }
        ]
    )

    assert payload["MPItemFeedHeader"] == {
        "businessUnit": "WALMART_US",
        "locale": "en",
        "version": "5.0.20260608-18_15_07-api",
    }
    item = payload["MPItem"][0]
    assert item["Orderable"] == {
        "sku": "PHONE-1",
        "productIdentifiers": {
            "productIdType": "GTIN",
            "productId": "00123456789012",
        },
    }
    assert item["Visible"]["Cell Phones"]["mainImageUrl"].endswith(
        "new-main/s-l1600.jpg"
    )
    assert item["Visible"]["Cell Phones"]["productSecondaryImageURL"] == [
        "https://i.ebayimg.com/images/g/new-back/s-l1600.jpg"
    ]


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
            "identifiers": {"GTIN": "00887276900124"},
        },
        {
            "walmart_item_id": "654",
            "title": "Samsung Galaxy Z Flip5 256GB Gray Factory Unlocked",
            "brand": "Samsung",
            "identifiers": {"GTIN": "00887276900452"},
        },
    ]

    match, reason = select_verified_catalog_match(item, candidates)

    assert match is not None
    assert match["walmart_item_id"] == "987"
    assert match["product_id"] == "00887276900124"
    assert "Exactly one" in reason


def test_verified_catalog_match_collapses_duplicate_records_for_the_same_gtin():
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

    assert match is not None
    assert match["product_id"] == "00000000000123"
    assert reason.startswith("Duplicate Walmart catalog records")


def test_verified_catalog_match_rejects_multiple_distinct_gtins():
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
    candidates = [
        {
            "title": "Apple Watch Series 11 42mm Black GPS + Cellular",
            "brand": "Apple",
            "identifiers": {"GTIN": "00000000000123"},
        },
        {
            "title": "Apple Watch Series 11 42mm Black GPS + Cellular",
            "brand": "Apple",
            "identifiers": {"GTIN": "00000000000451"},
        },
    ]

    match, reason = select_verified_catalog_match(item, candidates)

    assert match is None
    assert reason.startswith("2 catalog candidates")


def test_verified_catalog_match_rejects_locked_carrier_variant_for_unlocked_phone():
    item = InventoryItem(
        sku="EBAY-UNLOCKED",
        title="Samsung Galaxy A16 128GB Unlocked",
        category="Cell Phones & Accessories:Cell Phones & Smartphones",
        item_specifics={
            "Brand": "Samsung",
            "Model": "Samsung Galaxy A16",
            "Storage": "128 GB",
            "Network": "Unlocked",
        },
    )
    candidates = [
        {
            "title": "Samsung Galaxy A16 128GB Straight Talk Smartphone",
            "brand": "Samsung",
            "identifiers": {"UPC": "123456789012"},
        }
    ]

    match, reason = select_verified_catalog_match(item, candidates)

    assert match is None
    assert "carrier" in reason


def test_parse_walmart_product_page_extracts_upc_and_shipping_weight():
    page = """
    <html><head>
      <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Samsung Phone",
         "brand":{"@type":"Brand","name":"Samsung"},"model":"SM-A166U1",
         "gtin13":"616960559061"}
      </script>
    </head><body>
      <script>window.__DATA__={"name":"Assembled product weight","value":"7.05 oz"}</script>
    </body></html>
    """

    result = parse_walmart_product_page(page)

    assert result["identifiers"] == {"UPC": "616960559061"}
    assert result["brand"] == "Samsung"
    assert result["model"] == "SM-A166U1"
    assert result["shipping_weight_lbs"] == 0.441


def test_parse_walmart_product_page_extracts_manufacturer_number_from_page_data():
    page = '<script>window.__DATA__={"manufactureNumber":"JBLFLIP6BLKAM"}</script>'

    result = parse_walmart_product_page(page)

    assert result["manufacturer_number"] == "JBLFLIP6BLKAM"


def test_estimated_shipping_weight_is_conservative_by_category():
    assert estimated_shipping_weight_lbs(
        InventoryItem(sku="PHONE", title="Apple iPhone 16", category="Smartphones")
    ) == 2.0
    assert estimated_shipping_weight_lbs(
        InventoryItem(sku="LAPTOP", title="Apple MacBook Pro", category="Computers")
    ) == 10.0


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


def test_walmart_client_submits_item_maintenance_feed(monkeypatch):
    requests = []

    def handler(method, path, headers, **kwargs):
        requests.append((method, path, headers, kwargs))
        request = httpx.Request(
            method,
            f"https://marketplace.walmartapis.com{path}",
            headers=headers,
        )
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/feeds":
            assert kwargs["params"] == {"feedType": "MP_MAINTENANCE"}
            assert kwargs["json"]["MPItem"][0]["Orderable"]["sku"] == "PHONE-1"
            return httpx.Response(200, json={"feedId": "IMAGE@123"}, request=request)
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )
    client = WalmartMarketplaceClient(_settings())

    result = asyncio.run(
        client.submit_item_maintenance_feed(
            {
                "MPItemFeedHeader": {
                    "businessUnit": "WALMART_US",
                    "locale": "en",
                    "version": "5.0.20260608-18_15_07-api",
                },
                "MPItem": [{"Orderable": {"sku": "PHONE-1"}}],
            }
        )
    )

    assert result["feed_id"] == "IMAGE@123"


def test_walmart_catalog_search_reports_match(monkeypatch):
    def handler(method, path, headers, **kwargs):
        request = httpx.Request(method, f"https://marketplace.walmartapis.com{path}", headers=headers)
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/items/walmart/search":
            assert kwargs["params"] == {"upc": "887276900124", "responseFormat": "SPEC"}
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

    result = asyncio.run(WalmartMarketplaceClient(_settings()).search_catalog("UPC", "887276900124"))

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
                            "gtin": "00887276900124",
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
            "identifiers": {"GTIN": "00887276900124"},
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


def test_walmart_client_updates_regular_price(monkeypatch):
    requests = []

    def handler(method, path, headers, **kwargs):
        requests.append((method, path, headers, kwargs))
        request = httpx.Request(
            method,
            f"https://marketplace.walmartapis.com{path}",
            headers=headers,
        )
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/price":
            assert method == "PUT"
            assert headers["Content-Type"] == "application/json"
            assert kwargs["json"] == {
                "sku": "PHONE-1",
                "pricing": [
                    {
                        "currentPriceType": "BASE",
                        "currentPrice": {"currency": "USD", "amount": 110.0},
                    }
                ],
            }
            return httpx.Response(
                200,
                json={"ItemPriceResponse": {"sku": "PHONE-1"}},
                request=request,
            )
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )

    result = asyncio.run(WalmartMarketplaceClient(_settings()).update_price("PHONE-1", 110))

    assert result["status"] == "updated"
    assert result["price"] == 110.0
    assert [request[1] for request in requests] == ["/v3/token", "/v3/price"]


def test_walmart_client_retries_http_520(monkeypatch):
    inventory_attempts = 0

    def handler(method, path, headers, **kwargs):
        nonlocal inventory_attempts
        request = httpx.Request(
            method,
            f"https://marketplace.walmartapis.com{path}",
            headers=headers,
        )
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/inventory":
            inventory_attempts += 1
            if inventory_attempts < 3:
                return httpx.Response(520, json={"message": "temporary error"}, request=request)
            return httpx.Response(
                200,
                json={"quantity": {"unit": "EACH", "amount": 4}},
                request=request,
            )
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )
    monkeypatch.setattr(walmart_module.asyncio, "sleep", no_sleep)

    quantity = asyncio.run(WalmartMarketplaceClient(_settings()).get_inventory_quantity("PHONE-1"))

    assert quantity == 4
    assert inventory_attempts == 3


def test_walmart_client_treats_not_found_items_catalog_as_empty(monkeypatch):
    def handler(method, path, headers, **kwargs):
        request = httpx.Request(method, f"https://marketplace.walmartapis.com{path}", headers=headers)
        if path == "/v3/token":
            return httpx.Response(200, json={"access_token": "access-token"}, request=request)
        if path == "/v3/items":
            return httpx.Response(404, json={"message": "No items found"}, request=request)
        raise AssertionError(f"Unexpected Walmart request: {method} {path}")

    monkeypatch.setattr(
        walmart_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler, *args, **kwargs),
    )

    items = asyncio.run(WalmartMarketplaceClient(_settings()).list_published_items())

    assert items == []
