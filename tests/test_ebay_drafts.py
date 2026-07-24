import asyncio
import csv
import io
from types import SimpleNamespace

import httpx

import app.ebay as ebay_module
from app.ebay import EbayClient
from app.ebay_draft_batch import inventory_sheet_missing_drafts


def _settings():
    return SimpleNamespace(
        ebay_access_token="user-token",
        ebay_client_id=None,
        ebay_client_secret=None,
        ebay_refresh_token=None,
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope/sell.inventory",
        ebay_marketplace_id="EBAY_US",
    )


def _catalog_product(title: str, epid: str = "12345") -> dict:
    return {
        "epid": epid,
        "title": title,
        "image": {"imageUrl": "https://i.ebayimg.com/images/g/catalog/s-l1600.jpg"},
        "additionalImages": [
            {"imageUrl": "https://i.ebayimg.com/images/g/catalog-two/s-l1600.jpg"}
        ],
        "aspects": [],
    }


def test_inventory_sheet_batch_contains_only_the_69_missing_variants():
    drafts = inventory_sheet_missing_drafts()

    assert len(drafts) == 69
    assert sum(draft.quantity for draft in drafts) == 416
    assert {draft.sheet_row for draft in drafts} == set(range(2, 73)) - {13, 54}
    assert len({draft.sku for draft in drafts}) == 69
    assert all(len(draft.title) <= 80 for draft in drafts)


def test_seller_hub_draft_csv_uses_visible_draft_action_and_saved_images():
    drafts = inventory_sheet_missing_drafts()
    images = {
        draft.sku: [f"https://i.ebayimg.com/images/g/{draft.sheet_row}/s-l1600.jpg"]
        for draft in drafts
    }

    csv_text = EbayClient._seller_hub_draft_csv(drafts, images)
    rows = list(csv.reader(io.StringIO(csv_text)))

    assert rows[0][:3] == [
        "#INFO",
        "Version=1.0.0",
        "Template=eBay-draft-listing-template_US",
    ]
    assert rows[1][:5] == [
        "#INFO",
        "Action=Draft",
        "SiteID=US",
        "Country=US",
        "Currency=USD",
    ]
    assert rows[2] == [
        "Action",
        "Custom label (SKU)",
        "Category ID",
        "Title",
        "UPC",
        "Price",
        "Quantity",
        "Item photo URL",
        "Condition ID",
        "Description",
        "Format",
    ]
    data_rows = rows[3:]
    assert len(data_rows) == 69
    assert {row[0] for row in data_rows} == {"Draft"}
    assert all(row[1].startswith("SH-HW-WM-202607-") for row in data_rows)
    assert all(row[4] == "" for row in data_rows)
    assert all(row[7].startswith("https://i.ebayimg.com/") for row in data_rows)
    assert {row[8] for row in data_rows} == {"NEW", "USED"}
    assert {row[10] for row in data_rows} == {"FixedPrice"}


def test_manual_images_cover_all_previously_blocked_sheet_rows():
    drafts_by_row = {
        draft.sheet_row: draft for draft in inventory_sheet_missing_drafts()
    }
    manual_rows = {
        *range(2, 12),
        12,
        14,
        15,
        17,
        19,
        20,
        *range(25, 38),
        40,
        43,
        46,
        67,
        72,
    }

    assert {row for row, draft in drafts_by_row.items() if draft.manual_image_urls} == manual_rows
    assert all(
        url.startswith("https://")
        for row in manual_rows
        for url in drafts_by_row[row].manual_image_urls
    )
    assert all(drafts_by_row[row].manual_image_source for row in manual_rows)


def test_catalog_match_requires_model_storage_and_color():
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 48
    )
    exact = _catalog_product("Apple iPhone 13 Pro Max 256GB Sierra Blue Unlocked")
    wrong_storage = _catalog_product("Apple iPhone 13 Pro Max 128GB Sierra Blue Unlocked")

    assert EbayClient._catalog_match(draft, exact)["exact"] is True
    assert EbayClient._catalog_match(draft, wrong_storage)["exact"] is False
    assert EbayClient._select_catalog_product(draft, [wrong_storage, exact]) == exact


def test_catalog_match_requires_unlocked_network_and_can_use_same_variant_image_donor():
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 48
    )
    exact_unlocked = _catalog_product(
        "Apple iPhone 13 Pro Max 256GB Sierra Blue Unlocked",
        epid="UNLOCKED-EPID",
    )
    exact_unlocked["image"] = {}
    exact_unlocked["additionalImages"] = []
    tmobile_image_donor = _catalog_product(
        "Apple iPhone 13 Pro Max 256GB Sierra Blue T-Mobile",
        epid="TMOBILE-EPID",
    )

    assert EbayClient._catalog_match(draft, tmobile_image_donor)["exact"] is False
    assert EbayClient._catalog_match(draft, tmobile_image_donor)[
        "missing_network_tokens"
    ] == ["unlocked"]

    selected = EbayClient._select_catalog_product(
        draft,
        [tmobile_image_donor, exact_unlocked],
    )

    assert selected is not None
    assert selected["epid"] == "UNLOCKED-EPID"
    assert selected["imageSourceEpid"] == "TMOBILE-EPID"
    assert EbayClient._catalog_match(draft, selected)["exact"] is True
    assert EbayClient._catalog_image_urls(selected)


def test_catalog_match_rejects_scattered_model_number_and_extra_phone_variant():
    jbl_go_4 = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 32
    )
    wrong_jbl = _catalog_product(
        "JBL Go 3 4.2W Portable Waterproof Speaker - Black"
    )
    iphone_13 = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 59
    )
    wrong_iphone_mini = _catalog_product(
        "Apple iPhone 13 mini - 256 GB - Midnight (Unlocked)"
    )

    jbl_match = EbayClient._catalog_match(jbl_go_4, wrong_jbl)
    iphone_match = EbayClient._catalog_match(iphone_13, wrong_iphone_mini)

    assert jbl_match["exact"] is False
    assert "ordered_model_phrase" in jbl_match["missing_model_tokens"]
    assert iphone_match["exact"] is False
    assert "unexpected_mini" in iphone_match["missing_model_tokens"]


def test_catalog_match_uses_product_title_for_model_identity():
    iphone_12 = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 67
    )
    mislabeled = _catalog_product(
        "Apple iPhone 13 Pro Max - 128 GB - Silver (Unlocked)"
    )
    mislabeled["aspects"] = [
        {
            "localizedName": "Compatible Model",
            "localizedValues": ["Apple iPhone 12 Pro Max"],
        }
    ]

    match = EbayClient._catalog_match(iphone_12, mislabeled)

    assert match["exact"] is False
    assert "12" in match["missing_model_tokens"]


def test_browse_product_mapping_uses_only_product_stock_images():
    candidate = EbayClient._browse_product_catalog_candidate(
        {
            "itemId": "v1|123|0",
            "epid": "321",
            "image": {"imageUrl": "https://i.ebayimg.com/images/g/seller/s-l1600.jpg"},
        },
        {
            "title": "Seller listing title",
            "product": {
                "title": "Apple iPhone 13 Pro Max 256GB Sierra Blue Unlocked",
                "brand": "Apple",
                "image": {
                    "imageUrl": "https://i.ebayimg.com/images/g/product/s-l1600.jpg"
                },
                "additionalImages": [
                    {
                        "imageUrl": "https://i.ebayimg.com/images/g/product-two/s-l1600.jpg"
                    }
                ],
                "aspectGroups": [
                    {
                        "aspects": [
                            {"name": "Color", "values": ["Sierra Blue"]},
                            {"name": "Storage Capacity", "values": ["256 GB"]},
                        ]
                    }
                ],
            },
        },
    )

    assert candidate is not None
    assert candidate["imageSource"] == "EBAY_BROWSE_PRODUCT"
    assert EbayClient._catalog_image_urls(candidate) == [
        "https://i.ebayimg.com/images/g/product/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/product-two/s-l1600.jpg",
    ]
    assert "seller/s-l1600.jpg" not in str(candidate)


class FakeDraftAsyncClient:
    calls: list[tuple[str, str, object]] = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeDraftAsyncClient.calls = self.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, params))
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/commerce/catalog/v1_beta/product_summary/search":
            return httpx.Response(
                200,
                json={
                    "productSummaries": [
                        _catalog_product("Apple iPhone 13 Pro Max 256GB Sierra Blue Unlocked")
                    ]
                },
                request=request,
            )
        if path == "/sell/inventory/v1/offer":
            return httpx.Response(200, json={"offers": []}, request=request)
        raise AssertionError(f"Unexpected GET path: {path}")

    async def put(self, path, json=None, headers=None):
        self.calls.append(("PUT", path, json))
        request = httpx.Request("PUT", f"https://api.ebay.com{path}")
        return httpx.Response(204, request=request)

    async def post(self, path, data=None, content=None, json=None, headers=None):
        payload = json if json is not None else content if content is not None else data
        self.calls.append(("POST", path, payload))
        request = httpx.Request("POST", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/offer":
            return httpx.Response(201, json={"offerId": "OFFER-1"}, request=request)
        raise AssertionError(f"Unexpected POST path: {path}")


class FakeEmptyCatalogAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path, params=None, headers=None):
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/commerce/catalog/v1_beta/product_summary/search":
            return httpx.Response(200, content=b"", request=request)
        raise AssertionError(f"Unexpected GET path: {path}")


class FakeExistingDraftAsyncClient:
    calls: list[tuple[str, str, object]] = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeExistingDraftAsyncClient.calls = self.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, params))
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/offer":
            return httpx.Response(
                200,
                json={"offers": [{"offerId": "EXISTING-OFFER-1"}]},
                request=request,
            )
        if path.startswith("/sell/inventory/v1/inventory_item/"):
            return httpx.Response(
                200,
                json={
                    "product": {
                        "imageUrls": [
                            "https://i.ebayimg.com/images/g/catalog/s-l1600.jpg"
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Existing draft unexpectedly searched catalog: {path}")


class FakeManualImageAsyncClient:
    calls: list[tuple[str, str, object]] = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeManualImageAsyncClient.calls = self.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, params))
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/offer":
            return httpx.Response(200, json={"offers": []}, request=request)
        raise AssertionError(f"Manual image draft unexpectedly searched catalog: {path}")

    async def put(self, path, json=None, headers=None):
        self.calls.append(("PUT", path, json))
        request = httpx.Request("PUT", f"https://api.ebay.com{path}")
        return httpx.Response(204, request=request)

    async def post(self, path, data=None, content=None, json=None, headers=None):
        payload = json if json is not None else content if content is not None else data
        self.calls.append(("POST", path, payload))
        request = httpx.Request("POST", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/offer":
            return httpx.Response(201, json={"offerId": "MANUAL-OFFER-1"}, request=request)
        raise AssertionError(f"Unexpected POST path: {path}")


def test_manual_verified_image_creates_offer_without_catalog_id(monkeypatch):
    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", FakeManualImageAsyncClient)
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 2
    )

    results = asyncio.run(
        EbayClient(_settings()).prepare_unpublished_drafts([draft], confirm=True)
    )

    assert results[0]["status"] == "created_unpublished"
    assert results[0]["offer_id"] == "MANUAL-OFFER-1"
    assert results[0]["published"] is False
    assert results[0]["image_source"] == "APPLE_OFFICIAL"
    put_payload = next(
        payload
        for method, path, payload in FakeManualImageAsyncClient.calls
        if method == "PUT" and "/inventory_item/" in path
    )
    assert put_payload["product"]["imageUrls"] == list(draft.manual_image_urls)
    assert "epid" not in put_payload["product"]
    assert not any(
        "/commerce/catalog/" in path
        for _, path, _ in FakeManualImageAsyncClient.calls
    )
    assert not any(
        "/publish" in path for _, path, _ in FakeManualImageAsyncClient.calls
    )


def test_existing_unpublished_offer_is_verified_before_catalog_lookup(monkeypatch):
    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", FakeExistingDraftAsyncClient)
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 56
    )

    results = asyncio.run(
        EbayClient(_settings()).prepare_unpublished_drafts([draft], confirm=True)
    )

    assert results[0]["status"] == "existing_unpublished"
    assert results[0]["offer_id"] == "EXISTING-OFFER-1"
    assert results[0]["published"] is False
    assert results[0]["inventory_item_verified"] is True
    assert results[0]["image_count"] == 1
    assert FakeExistingDraftAsyncClient.calls == [
        ("GET", "/sell/inventory/v1/offer", {"sku": draft.sku, "limit": 10}),
        (
            "GET",
            f"/sell/inventory/v1/inventory_item/{draft.sku}",
            None,
        ),
    ]


def test_empty_successful_catalog_response_blocks_item_without_crashing(monkeypatch):
    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", FakeEmptyCatalogAsyncClient)
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 38
    )

    results = asyncio.run(
        EbayClient(_settings()).prepare_unpublished_drafts([draft], confirm=False)
    )

    assert results[0]["status"] == "blocked_no_catalog_image"
    assert results[0]["published"] is False


def test_confirmed_batch_creates_unpublished_offer_with_catalog_images(monkeypatch):
    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", FakeDraftAsyncClient)
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 48
    )

    results = asyncio.run(
        EbayClient(_settings()).prepare_unpublished_drafts([draft], confirm=True)
    )

    assert results[0]["status"] == "created_unpublished"
    assert results[0]["offer_id"] == "OFFER-1"
    assert results[0]["published"] is False
    put_payload = next(
        payload
        for method, path, payload in FakeDraftAsyncClient.calls
        if method == "PUT" and "/inventory_item/" in path
    )
    assert put_payload["product"]["imageUrls"] == [
        "https://i.ebayimg.com/images/g/catalog/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/catalog-two/s-l1600.jpg",
    ]
    assert not any("/publish" in path for _, path, _ in FakeDraftAsyncClient.calls)


class FakeBrowseProductAsyncClient:
    calls: list[tuple[str, str, object, str | None]] = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeBrowseProductAsyncClient.calls = self.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path, params=None, headers=None):
        authorization = (headers or {}).get("Authorization")
        self.calls.append(("GET", path, params, authorization))
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/commerce/catalog/v1_beta/product_summary/search":
            assert authorization == "Bearer user-token"
            return httpx.Response(403, json={"errors": [{"message": "Access denied"}]}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            assert authorization == "Bearer application-token"
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123|0",
                            "epid": "321",
                            "title": "Apple iPhone 13 Pro Max 256GB Sierra Blue Unlocked",
                        }
                    ]
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123|0":
            assert authorization == "Bearer application-token"
            return httpx.Response(
                200,
                json={
                    "epid": "321",
                    "product": {
                        "title": "Apple iPhone 13 Pro Max 256GB Sierra Blue Unlocked",
                        "brand": "Apple",
                        "image": {
                            "imageUrl": "https://i.ebayimg.com/images/g/product/s-l1600.jpg"
                        },
                        "aspectGroups": [
                            {
                                "aspects": [
                                    {"name": "Color", "values": ["Sierra Blue"]},
                                    {"name": "Storage Capacity", "values": ["256 GB"]},
                                ]
                            }
                        ],
                    },
                },
                request=request,
            )
        raise AssertionError(f"Unexpected GET path: {path}")

    async def post(self, path, data=None, content=None, json=None, headers=None):
        payload = json if json is not None else content if content is not None else data
        self.calls.append(("POST", path, payload, (headers or {}).get("Authorization")))
        request = httpx.Request("POST", f"https://api.ebay.com{path}")
        if path == "/identity/v1/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "application-token"},
                request=request,
            )
        raise AssertionError(f"Unexpected POST path: {path}")


def test_catalog_permission_falls_back_to_browse_product_stock_image(monkeypatch):
    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", FakeBrowseProductAsyncClient)
    settings = _settings()
    settings.ebay_client_id = "client-id"
    settings.ebay_client_secret = "client-secret"
    settings.ebay_application_oauth_scopes = "https://api.ebay.com/oauth/api_scope"
    draft = next(
        item for item in inventory_sheet_missing_drafts() if item.sheet_row == 48
    )

    results = asyncio.run(
        EbayClient(settings).prepare_unpublished_drafts([draft], confirm=False)
    )

    assert results[0]["status"] == "ready"
    assert results[0]["selected_catalog_product"]["epid"] == "321"
    assert results[0]["image_urls"] == [
        "https://i.ebayimg.com/images/g/product/s-l1600.jpg"
    ]
    assert not any("/publish" in path for _, path, _, _ in FakeBrowseProductAsyncClient.calls)
